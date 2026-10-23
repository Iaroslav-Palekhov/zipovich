#!/usr/bin/env python3
"""
Рекурсивно сжимает все видео из ./vid до 360p MP4 (H.264 + AAC).
2 файла параллельно, каждый ffmpeg жёстко ограничен по потокам.

Прогресс: на каждый воркер — имя файла сверху, проценты снизу.
Оригиналы удаляются ТОЛЬКО после успешной проверки результата ffprobe.

Требуются ffmpeg.exe и ffprobe.exe рядом со скриптом.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------- НАСТРОЙКИ ----------
WORKERS         = 2          # параллельных файлов
FFMPEG_THREADS  = 2          # потоков на каждый ffmpeg (декодер + энкодер)
PRESET          = 'faster'   # 'veryfast' если совсем тормозит
MAX_DIM         = 360
CRF             = 28
ABITRATE        = '64k'

TMP_SUFFIX      = '.__compressing__.mp4'

VIDEO_EXTS = {
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv',
    '.m4v', '.ts', '.mpg', '.mpeg', '.3gp', '.ogv', '.m2ts',
    '.vob', '.divx', '.rmvb', '.rm', '.asf', '.f4v', '.mts',
}

SCRIPT_DIR = Path(__file__).resolve().parent
FFMPEG  = SCRIPT_DIR / 'ffmpeg.exe'
FFPROBE = SCRIPT_DIR / 'ffprobe.exe'
SRC_DIR = SCRIPT_DIR / 'vid'
# -------------------------------


def enable_ansi():
    """Включает ANSI-escape на Windows 10+ (иначе \x1b[*] просто печатаются)."""
    if os.name == 'nt':
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


class Display:
    """N воркеров × 2 строки (имя / проценты), блок внизу экрана."""

    def __init__(self, n_slots):
        self.n_slots = n_slots
        self.n_lines = n_slots * 2
        self.lines = [''] * self.n_lines
        self.lock = threading.Lock()
        self.first = True
        enable_ansi()

    def _clear_block(self):
        if self.first:
            return
        sys.stdout.write(f'\x1b[{self.n_lines}A\x1b[J')

    def _draw_block(self):
        for line in self.lines:
            sys.stdout.write(line + '\n')
        sys.stdout.flush()
        self.first = False

    def update_slot(self, slot, name, pct=None, speed=None, note=None):
        with self.lock:
            i = slot * 2
            self.lines[i] = name
            if pct is None:
                self.lines[i + 1] = f'        {note or ""}'
            else:
                spd = f'  {speed}' if speed else ''
                self.lines[i + 1] = f'        {pct:5.1f}%{spd}'
            self._clear_block()
            self._draw_block()

    def clear_slot(self, slot):
        with self.lock:
            i = slot * 2
            self.lines[i] = ''
            self.lines[i + 1] = ''
            self._clear_block()
            self._draw_block()

    def log(self, msg):
        with self.lock:
            self._clear_block()
            sys.stdout.write(msg + '\n')
            self._draw_block()

    def shutdown(self):
        with self.lock:
            self._clear_block()
            sys.stdout.flush()


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def probe(path: Path) -> dict:
    r = run([str(FFPROBE), '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', str(path)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or 'ffprobe error')
    return json.loads(r.stdout)


def real_video_streams(info: dict) -> list:
    out = []
    for s in info.get('streams', []):
        if s.get('codec_type') != 'video':
            continue
        if (s.get('disposition') or {}).get('attached_pic'):
            continue
        out.append(s)
    return out


def display_dims(stream: dict):
    w = int(stream.get('width')  or 0)
    h = int(stream.get('height') or 0)
    rot = 0
    for sd in stream.get('side_data_list') or []:
        if 'rotation' in sd:
            try:
                rot = int(round(float(sd['rotation'])))
                break
            except (TypeError, ValueError):
                pass
    if rot == 0:
        tags = stream.get('tags') or {}
        if 'rotate' in tags:
            try:
                rot = int(tags['rotate'])
            except (TypeError, ValueError):
                pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    return w, h


def run_ffmpeg(cmd, duration_s, on_progress):
    """Запускает ffmpeg с -progress pipe:1, отдаёт проценты в on_progress."""
    err_lines = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', bufsize=1,
    )

    def read_err():
        try:
            for line in proc.stderr:
                err_lines.append(line)
        except Exception:
            pass

    t = threading.Thread(target=read_err, daemon=True)
    t.start()

    state = {}
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            state[k] = v
        if line.startswith('progress='):
            if duration_s > 0:
                us_str = (state.get('out_time_us')
                          or state.get('out_time_ms') or '0')
                try:
                    us = int(us_str)
                except ValueError:
                    us = 0
                pct = min(100.0, us / (duration_s * 1e6) * 100.0)
                on_progress(pct, state.get('speed', '').strip())
            if line == 'progress=end':
                break

    proc.wait()
    t.join(timeout=2)
    return proc.returncode, ''.join(err_lines)


_thread_slots = {}
_thread_slots_lock = threading.Lock()


def get_slot():
    tid = threading.get_ident()
    with _thread_slots_lock:
        if tid not in _thread_slots:
            _thread_slots[tid] = len(_thread_slots) % WORKERS
        return _thread_slots[tid]


def compress_one(src: Path, idx: int, total: int, display: Display) -> str:
    slot = get_slot()
    label = f'[{idx}/{total}]'
    name_line = f'[w{slot + 1}] [{idx}/{total}] {src.name}'
    tmp = src.with_name(src.name + TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    t0 = time.time()

    try:
        display.update_slot(slot, name_line, note='проверяю...')
        try:
            info = probe(src)
        except Exception as e:
            display.log(f'{label} [!] {src.name}: не читается ({e})')
            return 'error'

        vstreams = real_video_streams(info)
        if not vstreams:
            display.log(f'{label} [-] {src.name}: нет видео-потока')
            return 'skipped'

        v = vstreams[0]
        w, h = display_dims(v)
        short = h if w >= h else w
        if short <= MAX_DIM:
            display.log(f'{label} [=] {src.name}: уже {w}x{h}, пропуск')
            return 'skipped'

        vf = (f"scale=-2:'min({MAX_DIM},ih)'" if w >= h
              else f"scale='min({MAX_DIM},iw)':-2")
        try:
            dur = float(info.get('format', {}).get('duration') or 0)
        except (TypeError, ValueError):
            dur = 0.0

        display.update_slot(slot, name_line, pct=0.0)

        def on_prog(pct, speed):
            display.update_slot(slot, name_line, pct=pct, speed=speed)

        cmd = [
            str(FFMPEG), '-hide_banner', '-nostdin', '-loglevel', 'error',
            '-nostats', '-progress', 'pipe:1', '-y',
            '-filter_threads', '1',
            '-threads', str(FFMPEG_THREADS),
            '-i', str(src),
            '-map', '0:V:0', '-map', '0:a:0?',
            '-vf', vf,
            '-c:v', 'libx264', '-preset', PRESET, '-crf', str(CRF),
            '-threads', str(FFMPEG_THREADS),
            '-x264-params',
            f'threads={FFMPEG_THREADS}:lookahead_threads=1',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', ABITRATE,
            '-movflags', '+faststart',
            str(tmp),
        ]
        rc, err = run_ffmpeg(cmd, dur, on_prog)
        if rc != 0:
            display.log(f'{label} [!] ffmpeg ({src.name}): '
                        f'{err.strip()[-400:]}')
            tmp.unlink(missing_ok=True)
            return 'error'

        display.update_slot(slot, name_line, note='проверяю результат...')

        try:
            info2 = probe(tmp)
            v2    = real_video_streams(info2)
            dur2  = float(info2.get('format', {}).get('duration') or 0)
        except Exception as e:
            display.log(f'{label} [!] verify ({src.name}): {e}')
            tmp.unlink(missing_ok=True)
            return 'error'

        if not v2:
            display.log(f'{label} [!] {src.name}: в результате нет видео')
            tmp.unlink(missing_ok=True)
            return 'error'
        if dur > 0 and abs(dur2 - dur) > max(2.0, dur * 0.02):
            display.log(f'{label} [!] {src.name}: '
                        f'длительность {dur:.1f} -> {dur2:.1f}')
            tmp.unlink(missing_ok=True)
            return 'error'

        size_old = src.stat().st_size
        size_new = tmp.stat().st_size
        if size_new == 0:
            display.log(f'{label} [!] {src.name}: пустой результат')
            tmp.unlink(missing_ok=True)
            return 'error'
        if size_new >= size_old:
            display.log(f'{label} [=] {src.name}: результат не меньше '
                        f'({size_old // 1024}K -> {size_new // 1024}K), '
                        f'оставляю оригинал')
            tmp.unlink(missing_ok=True)
            return 'skipped'

        if src.suffix.lower() == '.mp4':
            try:
                os.replace(tmp, src)
            except Exception as e:
                display.log(f'{label} [!] {src.name}: не заменить ({e}); '
                            f'результат: {tmp.name}')
                return 'error'
            dest = src
        else:
            dest = src.with_suffix('.mp4')
            if dest.exists():
                dest = src.parent / (src.stem + '.compressed.mp4')
            try:
                os.replace(tmp, dest)
            except Exception as e:
                display.log(f'{label} [!] {src.name}: не переименовать ({e}); '
                            f'результат: {tmp.name}')
                return 'error'
            try:
                src.unlink()
            except Exception as e:
                display.log(f'{label} [!] {src.name}: не удалить оригинал '
                            f'({e}); сжатый рядом: {dest.name}')

        elapsed = time.time() - t0
        pct = size_new * 100 / size_old if size_old else 0
        display.log(f'{label} [+] {src.name} -> {dest.name}  '
                    f'({size_old // 1024}K -> {size_new // 1024}K, '
                    f'{pct:.0f}%) за {elapsed:.0f}с')
        return 'compressed'
    finally:
        display.clear_slot(slot)


def main():
    for exe in (FFMPEG, FFPROBE):
        if not exe.exists():
            sys.exit(f'Нет {exe.name} рядом со скриптом: {exe}')
    if not SRC_DIR.exists():
        sys.exit(f'Нет папки: {SRC_DIR}')

    for tail in SRC_DIR.rglob(f'*{TMP_SUFFIX}'):
        if tail.is_file():
            print(f'[i] чищу хвост: {tail.name}')
            tail.unlink(missing_ok=True)

    files = sorted(
        p for p in SRC_DIR.rglob('*')
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTS
        and not p.name.endswith(TMP_SUFFIX)
    )

    display = Display(WORKERS)
    display.log(f'[i] файлов: {len(files)} | параллельно: {WORKERS} | '
                f'потоков на ffmpeg: {FFMPEG_THREADS} | preset: {PRESET} | '
                f'CRF: {CRF}')

    stat = {'compressed': 0, 'skipped': 0, 'error': 0}
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {}
    try:
        for i, f in enumerate(files, 1):
            futures[ex.submit(compress_one, f, i, len(files), display)] = f
        for fut in as_completed(futures):
            try:
                stat[fut.result()] += 1
            except Exception as e:
                f = futures[fut]
                display.log(f'[!] {f.name}: исключение: {e}')
                stat['error'] += 1
    except KeyboardInterrupt:
        display.log('\n[i] Ctrl+C: отменяю очередь, дожидаюсь запущенных...')
        for fut in futures:
            fut.cancel()
    finally:
        ex.shutdown(wait=True)
        display.shutdown()

    print(f"\n===== сжато: {stat['compressed']}, "
          f"пропущено: {stat['skipped']}, "
          f"ошибок: {stat['error']} =====")


if __name__ == '__main__':
    main()
