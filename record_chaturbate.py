#!/usr/bin/env python3
"""
Chaturbate Recorder Void — запись в исходном качестве со звуком.
Запуск: python recorder_void.py <room_slug> [--auto-refresh]
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
AUTO_REFRESH = False
AUTO_REFRESH_INTERVAL = 300
URL_FETCH_RETRIES = 3
FFMPEG_RESTART_DELAY = 5
OUTPUT_BASE_DIR = "recordings"
HANG_TIMEOUT = 60          # секунд без новых кадров → перезапуск
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

# ------------------------- АРГУМЕНТЫ -------------------------------
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

# --------------------------- ПАПКА КОМНАТЫ -------------------------
room_dir = os.path.join(OUTPUT_BASE_DIR, ROOM_SLUG)
os.makedirs(room_dir, exist_ok=True)

# --------------------------- ФУНКЦИИ ------------------------------
def fetch_streams():
    """
    Возвращает (video_url, audio_url, room_status).
    video_url — ссылка на видео-поток максимального качества.
    audio_url — ссылка на аудио-поток (или None, если аудио нет).
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
        return None, None, room_status

    master_url = data.get("url")
    if not master_url:
        raise Exception("No stream URL in API response")

    # Загружаем мастер-плейлист
    try:
        master_resp = requests.get(master_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        master_resp.raise_for_status()
        lines = master_resp.text.splitlines()
    except Exception as e:
        raise Exception(f"Failed to fetch master playlist: {e}")

    # Парсим варианты
    video_variants = []  # (bandwidth, url)
    audio_url = None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            # Ищем BANDWIDTH и CODECS
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            codecs_match = re.search(r'CODECS="([^"]*)"', line)
            bandwidth = int(bw_match.group(1)) if bw_match else 0
            codecs = codecs_match.group(1) if codecs_match else ""
            # Следующая строка — URL варианта
            if i + 1 < len(lines) and not lines[i+1].startswith('#'):
                variant_url = urllib.parse.urljoin(master_url, lines[i+1].strip())
                # Определяем тип по кодеку: если mp4a — аудио, иначе видео
                if "mp4a" in codecs and "avc" not in codecs:
                    if audio_url is None:  # берём первый попавшийся аудио-вариант
                        audio_url = variant_url
                else:
                    video_variants.append((bandwidth, variant_url))

    # Выбираем видео с максимальным битрейтом
    video_url = None
    if video_variants:
        video_variants.sort(key=lambda x: x[0], reverse=True)
        video_url = video_variants[0][1]

    if not video_url:
        raise Exception("No video variant found in master playlist")

    return video_url, audio_url, room_status

def build_ffmpeg_cmd(video_url, audio_url, filename):
    """Собирает команду FFmpeg с видео и (опционально) аудио."""
    cmd = [
        "ffmpeg",
        "-user_agent", HEADERS["User-Agent"],
        "-headers", "Referer: https://chaturbate.com/\r\n",
        "-timeout", "10000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", video_url
    ]
    if audio_url:
        cmd += ["-i", audio_url]
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0"]
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

# ----------------------- ПРОВЕРКА FFMPEG --------------------------
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (subprocess.CalledProcessError, FileNotFoundError):
    print("Error: FFmpeg not found. Install it and add to PATH.")
    sys.exit(1)

# --------------------- ПЕРВЫЙ ЗАПУСК -----------------------------
print(f"Fetching initial streams for '{ROOM_SLUG}'...")
video_url = audio_url = None
room_status = None
for attempt in range(URL_FETCH_RETRIES):
    try:
        video_url, audio_url, room_status = fetch_streams()
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        time.sleep(2)
else:
    print("Could not get initial streams. Exiting.")
    sys.exit(1)

if room_status != "public" or not video_url:
    print(f"Room status is '{room_status}'. Waiting for public stream...")
    while True:
        time.sleep(30)
        try:
            video_url, audio_url, room_status = fetch_streams()
            if room_status == "public" and video_url:
                break
            else:
                print(f"Room status is '{room_status}'. Retrying...")
        except Exception as e:
            print(f"Retry failed: {e}")

# --------------------- ГЕНЕРАЦИЯ ИМЕНИ --------------------------
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
print(f"Output file: {filename}")

ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, filename)
print(f"Starting recording (video: {video_url[:60]}... audio: {'yes' if audio_url else 'no'})")
ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)

def get_next_filename():
    global restart_counter, base_filename
    restart_counter += 1
    candidate = f"{base_filename}_part{restart_counter}.ts"
    while os.path.exists(candidate):
        restart_counter += 1
        candidate = f"{base_filename}_part{restart_counter}.ts"
    return candidate

# --------------------- СТОРОЖЕВОЙ ТАЙМЕР -----------------------
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

# --------------------- ГЛАВНЫЙ ЦИКЛ ----------------------------
last_url_check = time.time()

try:
    while True:
        poll = ffmpeg_process.poll()
        if poll is not None:
            print(f"FFmpeg exited with code {poll}. Restarting...")
            video_url = audio_url = None
            while True:
                for attempt in range(URL_FETCH_RETRIES):
                    try:
                        video_url, audio_url, room_status = fetch_streams()
                        if room_status == "public" and video_url:
                            break
                        else:
                            print(f"Room status '{room_status}'. Waiting 10s...")
                            time.sleep(10)
                    except Exception as e:
                        print(f"Refresh attempt {attempt+1} failed: {e}")
                        time.sleep(2)
                if video_url:
                    break
                time.sleep(30)

            new_filename = get_next_filename()
            ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, new_filename)
            print(f"Restarting FFmpeg → {new_filename}")
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
                    new_video, new_audio, new_status = fetch_streams()
                    if new_status == "public" and new_video and new_video != video_url:
                        print("Stream URL changed. Restarting...")
                        graceful_stop(ffmpeg_process)
                        video_url, audio_url = new_video, new_audio
                        new_filename = get_next_filename()
                        ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, new_filename)
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