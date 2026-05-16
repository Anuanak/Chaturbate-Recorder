#!/usr/bin/env python3
"""
Chaturbate Recorder Void — запись видео+аудио в одном файле (макс. качество)
"""

import requests
import subprocess
import sys
import datetime
import hashlib
import time
import os
import signal
import threading
import re
import urllib.parse

# ----------------------------- НАСТРОЙКИ -----------------------------
ROOM_SLUG = "seltin_sweety"
AUTO_REFRESH = False                # плановая проверка URL (обычно не нужна)
AUTO_REFRESH_INTERVAL = 300
URL_FETCH_RETRIES = 3
FFMPEG_RESTART_DELAY = 5
OUTPUT_BASE_DIR = "recordings"
HANG_TIMEOUT = 60                  # секунд без новых кадров → перезапуск
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

# ---------- аргументы командной строки ----------
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

# ---------- папка для комнаты ----------
room_dir = os.path.join(OUTPUT_BASE_DIR, ROOM_SLUG)
os.makedirs(room_dir, exist_ok=True)

# ---------- функции ----------
def fetch_best_stream():
    """
    Возвращает URL потока, который содержит и видео, и аудио (если доступно).
    Приоритет: комбинированный поток с максимальным битрейтом.
    Если комбинированного нет — берём видео макс. качества + отдельное аудио.
    Возвращает (url, room_status).
    """
    payload = {"room_slug": ROOM_SLUG}
    try:
        resp = requests.post(API_URL, headers=HEADERS, data=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise Exception(f"API request failed: {e}")

    if not data.get("success"):
        raise Exception(f"API returned success=false: {data}")
    room_status = data.get("room_status")
    if room_status != "public":
        return None, room_status

    master_url = data.get("url")
    if not master_url:
        raise Exception("No stream URL in API response")

    # Получаем мастер-плейлист
    try:
        master_resp = requests.get(master_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        master_resp.raise_for_status()
        content = master_resp.text
    except Exception as e:
        raise Exception(f"Failed to fetch master playlist: {e}")

    lines = content.splitlines()
    variants = []  # (bandwidth, url, has_audio)

    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            codecs_match = re.search(r'CODECS="([^"]*)"', line)
            bandwidth = int(bw_match.group(1)) if bw_match else 0
            codecs = codecs_match.group(1) if codecs_match else ""
            has_audio = "mp4a" in codecs
            if i + 1 < len(lines) and not lines[i+1].startswith('#'):
                variant_url = urllib.parse.urljoin(master_url, lines[i+1].strip())
                variants.append((bandwidth, variant_url, has_audio))

    # 1) Ищем комбинированный поток (видео + аудио) с макс. битрейтом
    combined = [v for v in variants if v[2]]
    if combined:
        combined.sort(key=lambda x: x[0], reverse=True)
        best = combined[0]
        print(f"Selected combined stream: {best[0]} bps, {best[1][:80]}...")
        return best[1], room_status

    # 2) Если комбинированного нет, будем брать видео + аудио отдельно,
    #    но эта ситуация маловероятна при наличии аудио.
    video_variants = [v for v in variants if not v[2]]
    if not video_variants:
        raise Exception("No video variants found")
    video_variants.sort(key=lambda x: x[0], reverse=True)
    video_url = video_variants[0][1]
    # Ищем отдельную аудиодорожку из EXT-X-MEDIA
    audio_url = None
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            uri_match = re.search(r'URI="([^"]*)"', line)
            if uri_match:
                audio_url = urllib.parse.urljoin(master_url, uri_match.group(1))
                break
    if audio_url:
        # Возвращаем специальный формат: видео+аудио, склеим в ffmpeg
        return (video_url, audio_url), room_status
    else:
        return video_url, room_status

def build_ffmpeg_cmd(stream_input, filename):
    """
    stream_input может быть:
    - строкой (один URL комбинированного потока)
    - кортежем (video_url, audio_url)
    """
    cmd = [
        "ffmpeg",
        "-user_agent", HEADERS["User-Agent"],
        "-headers", "Referer: https://chaturbate.com/\r\n",
        "-timeout", "10000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]
    if isinstance(stream_input, tuple):
        video_url, audio_url = stream_input
        cmd += ["-i", video_url, "-i", audio_url]
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-i", stream_input, "-map", "0"]
    cmd += [
        "-c", "copy",
        "-bsf:v", "h264_mp4toannexb",
        "-f", "mpegts",
        "-y",
        filename
    ]
    return cmd

def graceful_stop(process, timeout=10):
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait()
    except Exception:
        process.terminate()
        process.wait()

# ---------- проверка ffmpeg ----------
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (subprocess.CalledProcessError, FileNotFoundError):
    print("Error: FFmpeg not found.")
    sys.exit(1)

# ---------- первый запуск ----------
print(f"Fetching stream for '{ROOM_SLUG}'...")
stream_input = None
room_status = None
for attempt in range(URL_FETCH_RETRIES):
    try:
        stream_input, room_status = fetch_best_stream()
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        time.sleep(2)
else:
    print("Could not get stream. Exiting.")
    sys.exit(1)

if room_status != "public" or not stream_input:
    print(f"Room status is '{room_status}'. Waiting for public...")
    while True:
        time.sleep(30)
        try:
            stream_input, room_status = fetch_best_stream()
            if room_status == "public" and stream_input:
                break
            else:
                print(f"Room status is '{room_status}'. Retrying...")
        except Exception as e:
            print(f"Retry failed: {e}")

# ---------- генерация имени ----------
now = datetime.datetime.now()
date_str = now.strftime("%m.%d.%Y")
hash_str = hashlib.md5(ROOM_SLUG.encode()).hexdigest()[:8]
base_filename = os.path.join(room_dir, f"{date_str}_{hash_str}_{ROOM_SLUG}_recording")

def find_free_filename():
    if not os.path.exists(f"{base_filename}.ts"):
        return f"{base_filename}.ts", 1
    part = 2
    while True:
        candidate = f"{base_filename}_part{part}.ts"
        if not os.path.exists(candidate):
            return candidate, part
        part += 1

filename, restart_counter = find_free_filename()
print(f"Output: {filename}")

ffmpeg_cmd = build_ffmpeg_cmd(stream_input, filename)
ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)

def get_next_filename():
    global restart_counter, base_filename
    restart_counter += 1
    candidate = f"{base_filename}_part{restart_counter}.ts"
    while os.path.exists(candidate):
        restart_counter += 1
        candidate = f"{base_filename}_part{restart_counter}.ts"
    return candidate

# ---------- сторожевой таймер ----------
last_frame_time = time.time()

def read_stderr(pipe):
    global last_frame_time
    try:
        for line in iter(pipe.readline, ''):
            if 'frame=' in line:
                last_frame_time = time.time()
    except Exception:
        pass

stderr_thread = threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True)
stderr_thread.start()

last_url_check = time.time()

# ---------- главный цикл ----------
try:
    while True:
        poll = ffmpeg_process.poll()
        if poll is not None:
            print(f"FFmpeg exited with code {poll}. Restarting...")
            stream_input = None
            while True:
                for attempt in range(URL_FETCH_RETRIES):
                    try:
                        stream_input, room_status = fetch_best_stream()
                        if room_status == "public" and stream_input:
                            break
                        else:
                            print(f"Room status '{room_status}'. Waiting 10s...")
                            time.sleep(10)
                    except Exception as e:
                        print(f"Refresh attempt {attempt+1} failed: {e}")
                        time.sleep(2)
                if stream_input:
                    break
                time.sleep(30)

            new_filename = get_next_filename()
            ffmpeg_cmd = build_ffmpeg_cmd(stream_input, new_filename)
            print(f"Restarting → {new_filename}")
            ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
            last_frame_time = time.time()
            stderr_thread = threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True)
            stderr_thread.start()
            last_url_check = time.time()
        else:
            if time.time() - last_frame_time > HANG_TIMEOUT:
                print("FFmpeg appears hung. Restarting...")
                graceful_stop(ffmpeg_process)
                continue

            if AUTO_REFRESH and (time.time() - last_url_check > AUTO_REFRESH_INTERVAL):
                last_url_check = time.time()
                try:
                    new_input, new_status = fetch_best_stream()
                    if new_status == "public" and new_input and new_input != stream_input:
                        print("Stream URL changed. Restarting...")
                        graceful_stop(ffmpeg_process)
                        stream_input = new_input
                        new_filename = get_next_filename()
                        ffmpeg_cmd = build_ffmpeg_cmd(stream_input, new_filename)
                        ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
                        last_frame_time = time.time()
                        stderr_thread = threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True)
                        stderr_thread.start()
                except Exception as e:
                    print(f"Periodic URL check failed: {e}")
        time.sleep(2)

except KeyboardInterrupt:
    print("\nInterrupted by user. Stopping...")
    graceful_stop(ffmpeg_process)

print("Recording finished.")