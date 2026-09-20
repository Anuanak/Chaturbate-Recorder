#!/usr/bin/env python3
"""
Chaturbate Recorder Void — видео + аудио + живая статистика
"""

import requests, subprocess, sys, datetime, time, os, signal, threading, re, urllib.parse, secrets, shutil
from collections import deque

ROOM_SLUG = "seltin_sweety"
AUTO_REFRESH = False
AUTO_REFRESH_INTERVAL = 300
URL_FETCH_RETRIES = 3
FFMPEG_RESTART_DELAY = 5
OUTPUT_BASE_DIR = "recordings"
HANG_TIMEOUT = 60
STATUS_INTERVAL = 1  # как часто обновлять строку статуса (сек)
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

args = sys.argv[1:]
for arg in args[:]:
    if arg in ("--auto-refresh", "-u"):
        AUTO_REFRESH = True
        args.remove(arg)
    elif arg in ("--help", "-h"):
        print("Usage: python recorder_void.py <room_slug> [--auto-refresh]")
        sys.exit(0)
if args:
    ROOM_SLUG = args[0]

room_dir = os.path.join(OUTPUT_BASE_DIR, ROOM_SLUG)
os.makedirs(room_dir, exist_ok=True)

# ---------- статистика ----------
stats_lock = threading.Lock()
print_lock = threading.Lock()
stop_event = threading.Event()
stats = {
    "size": 0,             # размер текущего файла (байт)
    "done_bytes": 0,       # размер предыдущих файлов
    "out_time": "00:00:00",
    "bitrate": "N/A",
    "speed": "N/A",
    "fps": "0",
    "dup": "0",
    "drop": "0",
    "file": "",
    "files": 0,
    "restarts": 0,
    "session_start": time.time(),
}

def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024

def fmt_speed(bps):
    return f"{fmt_size(bps)}/s ({bps * 8 / 1_000_000:.2f} Mbit/s)"

def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

def log(msg):
    """Печать сообщения, не ломая строку статуса."""
    width = shutil.get_terminal_size((100, 20)).columns - 1
    with print_lock:
        sys.stdout.write("\r" + " " * width + "\r" + msg + "\n")
        sys.stdout.flush()

def status_loop():
    prev_size, prev_t = 0, time.time()
    speeds = deque(maxlen=5)  # сглаживание по 5 секундам
    while not stop_event.is_set():
        time.sleep(STATUS_INTERVAL)
        now = time.time()
        with stats_lock:
            size = stats["size"]
            total = stats["done_bytes"] + size
            out_time = stats["out_time"]
            bitrate = stats["bitrate"]
            speed = stats["speed"]
            fps = stats["fps"]
            drop = stats["drop"]
            restarts = stats["restarts"]
            files = stats["files"]
        delta = size - prev_size
        if delta < 0:  # начался новый файл
            delta = size
        speeds.append(delta / max(now - prev_t, 0.001))
        prev_size, prev_t = size, now
        cur_speed = sum(speeds) / len(speeds)

        try:
            free = fmt_size(shutil.disk_usage(room_dir).free)
        except Exception:
            free = "?"

        line = (
            f"● REC {ROOM_SLUG} | запись {out_time} | файл {fmt_size(size)} | "
            f"↓ {fmt_speed(cur_speed)} | ffmpeg {bitrate} {speed} {fps}fps drop={drop} | "
            f"всего {fmt_size(total)} ({files} ф.) | диск {free} | рестартов {restarts} | "
            f"работает {fmt_time(now - stats['session_start'])}"
        )
        width = shutil.get_terminal_size((100, 20)).columns - 1
        with print_lock:
            sys.stdout.write("\r" + line[:width].ljust(width))
            sys.stdout.flush()

# ---------- получение потока ----------
def fetch_best_stream():
    """Возвращает (video_url, audio_url, room_status)."""
    payload = {"room_slug": ROOM_SLUG}
    resp = requests.post(API_URL, headers=HEADERS, data=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"API error: {data}")
    room_status = data.get("room_status")
    if room_status != "public":
        return None, None, room_status

    master_url = data["url"]
    master_resp = requests.get(master_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
    master_resp.raise_for_status()
    lines = master_resp.text.splitlines()

    audio_groups = {}
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            group = re.search(r'GROUP-ID="([^"]*)"', line)
            uri = re.search(r'URI="([^"]*)"', line)
            if group and uri:
                audio_groups[group.group(1)] = urllib.parse.urljoin(master_url, uri.group(1))

    video_variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = int(re.search(r'BANDWIDTH=(\d+)', line).group(1)) if 'BANDWIDTH=' in line else 0
            audio_group = re.search(r'AUDIO="([^"]*)"', line)
            audio_group = audio_group.group(1) if audio_group else None
            if i + 1 < len(lines) and not lines[i + 1].startswith('#'):
                url = urllib.parse.urljoin(master_url, lines[i + 1].strip())
                codecs_match = re.search(r'CODECS="([^"]*)"', line)
                codecs = codecs_match.group(1) if codecs_match else ""
                if "avc" in codecs:
                    video_variants.append((bw, url, audio_group))

    if not video_variants:
        raise Exception("No video variants")

    video_variants.sort(key=lambda x: x[0], reverse=True)
    best_video = video_variants[0]
    audio_url = audio_groups.get(best_video[2]) if best_video[2] else None
    return best_video[1], audio_url, room_status

def build_ffmpeg_cmd(video_url, audio_url, filename):
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",
        "-progress", "pipe:1",          # машиночитаемая статистика в stdout
        "-user_agent", HEADERS["User-Agent"],
        "-headers", "Referer: https://chaturbate.com/\r\n",
        "-timeout", "10000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", video_url
    ]
    if audio_url:
        cmd += ["-i", audio_url, "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0"]
    cmd += ["-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", "-y", filename]
    return cmd

def graceful_stop(process, timeout=10):
    if process.poll() is not None:
        return
    try:
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        process.send_signal(sig)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait()
    except Exception:
        process.terminate()
        process.wait()

# ---------- чтение вывода ffmpeg ----------
last_frame = time.time()
ffmpeg_process = None

def read_progress(pipe):
    """Парсит блоки key=value из -progress."""
    global last_frame
    for line in iter(pipe.readline, ''):
        line = line.strip()
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        with stats_lock:
            if k == "total_size" and v.isdigit():
                size = int(v)
                if size != stats["size"]:
                    last_frame = time.time()  # файл растёт → не завис
                stats["size"] = size
            elif k == "out_time" and v != "N/A":
                stats["out_time"] = v.split('.')[0]
            elif k == "bitrate":
                stats["bitrate"] = v
            elif k == "speed":
                stats["speed"] = v
            elif k == "fps":
                stats["fps"] = v
            elif k == "drop_frames":
                stats["drop"] = v

def read_stderr(pipe):
    for line in iter(pipe.readline, ''):
        line = line.strip()
        if line:
            log(f"[ffmpeg] {line}")

def start_ffmpeg(video_url, audio_url, fname):
    global ffmpeg_process, last_frame
    with stats_lock:
        stats["done_bytes"] += stats["size"]
        stats["size"] = 0
        stats["out_time"] = "00:00:00"
        stats["file"] = fname
        stats["files"] += 1
    ffmpeg_process = subprocess.Popen(
        build_ffmpeg_cmd(video_url, audio_url, fname),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, encoding="utf-8", errors="replace",
    )
    last_frame = time.time()
    threading.Thread(target=read_progress, args=(ffmpeg_process.stdout,), daemon=True).start()
    threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True).start()

def get_next_filename():
    date_str = datetime.datetime.now().strftime("%m.%d.%Y")
    return os.path.join(room_dir, f"{date_str}_{secrets.token_hex(4)}_{ROOM_SLUG}_recording.ts")

# ---------- старт ----------
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except Exception:
    print("FFmpeg not found")
    sys.exit(1)

print(f"Fetching stream for '{ROOM_SLUG}'...")
video_url = audio_url = room_status = None
for _ in range(URL_FETCH_RETRIES):
    try:
        video_url, audio_url, room_status = fetch_best_stream()
        break
    except Exception as e:
        print(f"Attempt failed: {e}")
        time.sleep(2)
if room_status != "public" or not video_url:
    print(f"Room status '{room_status}', waiting...")
    while True:
        time.sleep(30)
        try:
            video_url, audio_url, room_status = fetch_best_stream()
            if room_status == "public" and video_url:
                break
        except Exception as e:
            print(f"Retry: {e}")

fname = get_next_filename()
print(f"Output: {fname}")
print(f"Audio: {'yes' if audio_url else 'no'}")
start_ffmpeg(video_url, audio_url, fname)
threading.Thread(target=status_loop, daemon=True).start()

last_url_check = time.time()
try:
    while True:
        if ffmpeg_process.poll() is not None:
            log(f"FFmpeg exited with {ffmpeg_process.poll()}. Restarting...")
            with stats_lock:
                stats["restarts"] += 1
            video_url = audio_url = None
            while True:
                for _ in range(URL_FETCH_RETRIES):
                    try:
                        video_url, audio_url, room_status = fetch_best_stream()
                        if room_status == "public" and video_url:
                            break
                        log(f"Status '{room_status}', wait 10s...")
                        time.sleep(10)
                    except Exception as e:
                        log(f"Refresh attempt failed: {e}")
                        time.sleep(2)
                if video_url:
                    break
                time.sleep(30)
            new_fname = get_next_filename()
            log(f"Restarting → {new_fname} (audio: {'yes' if audio_url else 'no'})")
            start_ffmpeg(video_url, audio_url, new_fname)
            last_url_check = time.time()
        else:
            if time.time() - last_frame > HANG_TIMEOUT:
                log("Hung detected, restarting...")
                graceful_stop(ffmpeg_process)
                continue
            if AUTO_REFRESH and time.time() - last_url_check > AUTO_REFRESH_INTERVAL:
                last_url_check = time.time()
                try:
                    new_v, new_a, new_s = fetch_best_stream()
                    if new_s == "public" and new_v and (new_v != video_url or new_a != audio_url):
                        graceful_stop(ffmpeg_process)
                        video_url, audio_url = new_v, new_a
                        start_ffmpeg(video_url, audio_url, get_next_filename())
                except Exception as e:
                    log(f"Periodic check failed: {e}")
        time.sleep(2)
except KeyboardInterrupt:
    log("Interrupted, stopping...")
    graceful_stop(ffmpeg_process)

stop_event.set()
with stats_lock:
    total = stats["done_bytes"] + stats["size"]
    files = stats["files"]
print(f"\nFinished. Записано: {fmt_size(total)} в {files} файл(ах), "
      f"время работы {fmt_time(time.time() - stats['session_start'])}.")
