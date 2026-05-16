#!/usr/bin/env python3
"""
Chaturbate Recorder Void — непрерывная запись с мгновенным восстановлением.
Запуск: python recorder_void.py <room_slug> [--auto-refresh]
"""

import requests
import subprocess
import sys
import datetime
import hashlib
import time
import urllib.parse
import re
import os
import signal

# ----------------------------- НАСТРОЙКИ -----------------------------
ROOM_SLUG = "seltin_sweety"              # комната по умолчанию
AUTO_REFRESH = False                     # если True — раз в N секунд проверяет смену URL (редко нужно)
AUTO_REFRESH_INTERVAL = 300              # интервал проверки (только если AUTO_REFRESH = True)
URL_FETCH_RETRIES = 3                    # число попыток получить ссылку при сбое
FFMPEG_RESTART_DELAY = 5                 # пауза перед перезапуском ffmpeg
OUTPUT_BASE_DIR = "recordings"           # корневая папка для всех записей
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

# ------------------------- АРГУМЕНТЫ КОМАНДНОЙ СТРОКИ ----------------
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

# --------------------------- ПАПКА ДЛЯ КОМНАТЫ -----------------------
room_dir = os.path.join(OUTPUT_BASE_DIR, ROOM_SLUG)
os.makedirs(room_dir, exist_ok=True)

# --------------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ------------------
def fetch_stream_url():
    """
    Получает URL потока максимального качества через API.
    Возвращает (stream_url, room_status) или (None, status) при неудаче.
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

    # Загружаем мастер-плейлист и выбираем вариант с макс. битрейтом
    try:
        master_resp = requests.get(master_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        master_resp.raise_for_status()
        lines = master_resp.text.splitlines()
    except Exception as e:
        raise Exception(f"Failed to fetch master playlist: {e}")

    max_bw = 0
    best_variant = None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "BANDWIDTH=" in line:
            match = re.search(r'BANDWIDTH=(\d+)', line)
            if match:
                bw = int(match.group(1))
                if bw > max_bw:
                    max_bw = bw
                    if i + 1 < len(lines):
                        best_variant = lines[i + 1].strip()

    if best_variant:
        stream_url = urllib.parse.urljoin(master_url, best_variant)
    else:
        stream_url = master_url
    return stream_url, room_status

def build_ffmpeg_cmd(stream_url, filename):
    """
    Собирает команду ffmpeg для записи в MPEG-TS с корректным
    преобразованием H.264 из avcC в Annex B.
    """
    return [
        "ffmpeg",
        "-f", "hls",
        "-user_agent", HEADERS["User-Agent"],
        "-headers", "Referer: https://chaturbate.com/\r\n",
        "-i", stream_url,
        "-c", "copy",
        "-bsf:v", "h264_mp4toannexb",
        "-f", "mpegts",
        "-y",
        filename
    ]

def graceful_stop(process, timeout=10):
    """Мягко завершить ffmpeg, давая время на запись последних пакетов."""
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

# ----------------------------- ПРОВЕРКА FFMPEG -----------------------
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (subprocess.CalledProcessError, FileNotFoundError):
    print("Error: FFmpeg not found. Install it and add to PATH.")
    sys.exit(1)

# -------------------------- ПЕРВЫЙ ЗАПУСК ----------------------------
print(f"Fetching initial stream URL for '{ROOM_SLUG}'...")
stream_url = None
room_status = None
for attempt in range(URL_FETCH_RETRIES):
    try:
        stream_url, room_status = fetch_stream_url()
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        time.sleep(2)
else:
    print("Could not get initial stream URL. Exiting.")
    sys.exit(1)

if room_status != "public" or not stream_url:
    print(f"Room status is '{room_status}'. Waiting for public stream...")
    while True:
        time.sleep(30)
        try:
            stream_url, room_status = fetch_stream_url()
            if room_status == "public" and stream_url:
                break
            else:
                print(f"Room status is '{room_status}'. Retrying...")
        except Exception as e:
            print(f"Retry failed: {e}")

# --------------- ГЕНЕРАЦИЯ ИМЕНИ ФАЙЛА (В ПАПКЕ КОМНАТЫ) ------------
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

ffmpeg_cmd = build_ffmpeg_cmd(stream_url, filename)
print(f"Starting recording to {filename}...")
ffmpeg_process = subprocess.Popen(ffmpeg_cmd)

def get_next_filename():
    global restart_counter, base_filename
    restart_counter += 1
    candidate = f"{base_filename}_part{restart_counter}.ts"
    while os.path.exists(candidate):
        restart_counter += 1
        candidate = f"{base_filename}_part{restart_counter}.ts"
    return candidate

# ------------------- ГЛАВНЫЙ ЦИКЛ (МГНОВЕННОЕ ВОССТАНОВЛЕНИЕ) -------
last_url_check = time.time()

try:
    while True:
        poll = ffmpeg_process.poll()
        if poll is not None:
            # FFmpeg упал — сразу пытаемся перезапуститься
            print(f"FFmpeg exited with code {poll}. Attempting immediate recovery...")
            while True:
                for attempt in range(URL_FETCH_RETRIES):
                    try:
                        new_url, new_status = fetch_stream_url()
                        if new_status == "public" and new_url:
                            stream_url = new_url
                            break
                        else:
                            print(f"Room status is '{new_status}'. Waiting 10s...")
                            time.sleep(10)
                    except Exception as e:
                        print(f"Refresh attempt {attempt+1} failed: {e}")
                        time.sleep(2)
                if new_status == "public" and new_url:
                    break
                time.sleep(30)

            new_filename = get_next_filename()
            ffmpeg_cmd = build_ffmpeg_cmd(stream_url, new_filename)
            print(f"Restarting FFmpeg → {new_filename}")
            ffmpeg_process = subprocess.Popen(ffmpeg_cmd)
            last_url_check = time.time()

        # Опциональная плановая проверка (только если включена)
        if AUTO_REFRESH and (time.time() - last_url_check > AUTO_REFRESH_INTERVAL):
            last_url_check = time.time()
            try:
                check_url, check_status = fetch_stream_url()
                if check_status == "public" and check_url and check_url != stream_url:
                    print("Stream URL changed (periodic check). Restarting...")
                    graceful_stop(ffmpeg_process)
                    stream_url = check_url
                    new_filename = get_next_filename()
                    ffmpeg_cmd = build_ffmpeg_cmd(stream_url, new_filename)
                    ffmpeg_process = subprocess.Popen(ffmpeg_cmd)
            except Exception as e:
                print(f"Periodic URL check failed: {e}")

        time.sleep(1)

except KeyboardInterrupt:
    print("\nInterrupted by user. Stopping recording...")
    graceful_stop(ffmpeg_process)

print("Recording finished.")