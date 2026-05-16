import requests
import subprocess
import sys
import datetime
import hashlib
import time
import urllib.parse
import re

# ---------- настройки ----------
ROOM_SLUG = "seltin_sweety"
AUTO_REFRESH = True
AUTO_REFRESH_INTERVAL = 300   # интервал плановой проверки статуса комнаты (сек)
URL_FETCH_RETRIES = 3         # попыток получить ссылку при ошибке
FFMPEG_RESTART_DELAY = 5      # пауза перед перезапуском ffmpeg (сек)
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
        print("Usage: python record_chaturbate.py <room_slug> [--auto-refresh]")
        sys.exit(0)

if args:
    ROOM_SLUG = args[0]

# ---------- вспомогательные функции ----------
def fetch_stream_url():
    """Получить URL лучшего качества для комнаты. Возвращает (url, room_status)."""
    payload = {"room_slug": ROOM_SLUG}
    resp = requests.post(API_URL, headers=HEADERS, data=payload)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"API returned success=false: {data}")
    room_status = data.get("room_status")
    if room_status != "public":
        return None, room_status

    master_url = data.get("url")
    if not master_url:
        raise Exception("No stream URL in API response")

    # Получаем мастер-плейлист и выбираем максимальный битрейт
    master_resp = requests.get(master_url, headers={"User-Agent": HEADERS["User-Agent"]})
    master_resp.raise_for_status()
    lines = master_resp.text.splitlines()
    max_bw = 0
    best_variant = None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "BANDWIDTH=" in line:
            bw = int(re.search(r'BANDWIDTH=(\d+)', line).group(1))
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
    """Создать команду ffmpeg с правильными заголовками."""
    return [
        "ffmpeg",
        "-f", "hls",
        "-user_agent", HEADERS["User-Agent"],
        "-headers", "Referer: https://chaturbate.com/\r\n",
        "-i", stream_url,
        "-c", "copy",
        filename
    ]

# ---------- проверка ffmpeg ----------
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (subprocess.CalledProcessError, FileNotFoundError):
    print("Error: FFmpeg not found. Install it and add to PATH.")
    sys.exit(1)

# ---------- первый запуск ----------
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
    print(f"Room status is '{room_status}'. Cannot record.")
    sys.exit(1)

# ---------- генерация базового имени ----------
now = datetime.datetime.now()
date_str = now.strftime("%m.%d.%Y")
hash_str = hashlib.md5(ROOM_SLUG.encode()).hexdigest()[:8]
base_filename = f"{date_str}_{hash_str}_{ROOM_SLUG}_recording"
filename = f"{base_filename}.mp4"

# ---------- счётчик перезапусков (для уникальных имён) ----------
restart_counter = 1

def get_next_filename():
    """Формирует имя следующего файла при перезапуске."""
    global restart_counter, base_filename
    restart_counter += 1
    return f"{base_filename}_part{restart_counter}.mp4"

ffmpeg_cmd = build_ffmpeg_cmd(stream_url, filename)
print(f"Starting recording to {filename}...")
ffmpeg_process = subprocess.Popen(ffmpeg_cmd)

# ---------- главный цикл мониторинга ----------
last_url_check = time.time()

try:
    while True:
        # Проверяем, жив ли ffmpeg
        poll = ffmpeg_process.poll()
        if poll is not None:
            # ffmpeg завершился (возможно с ошибкой)
            print(f"FFmpeg exited with code {poll}. Refreshing stream URL...")
            for attempt in range(URL_FETCH_RETRIES):
                try:
                    new_url, new_status = fetch_stream_url()
                    if new_status != "public":
                        print(f"Room is no longer public (status: {new_status}). Stopping.")
                        sys.exit(0)
                    if new_url:
                        stream_url = new_url
                        break
                except Exception as e:
                    print(f"Refresh attempt {attempt+1} failed: {e}")
                    time.sleep(2)
            else:
                print("Failed to refresh stream URL. Waiting before retry...")
                time.sleep(FFMPEG_RESTART_DELAY)
                continue

            # Новое уникальное имя файла, чтобы не перезаписывать предыдущий
            new_filename = get_next_filename()
            ffmpeg_cmd = build_ffmpeg_cmd(stream_url, new_filename)
            print(f"Restarting FFmpeg to {new_filename}")
            ffmpeg_process = subprocess.Popen(ffmpeg_cmd)
            last_url_check = time.time()
        else:
            # Плановая проверка смены URL
            if AUTO_REFRESH and (time.time() - last_url_check > AUTO_REFRESH_INTERVAL):
                last_url_check = time.time()
                try:
                    new_url, new_status = fetch_stream_url()
                    if new_status != "public":
                        print("Room is not public anymore. Terminating recording.")
                        ffmpeg_process.terminate()
                        ffmpeg_process.wait()
                        break
                    if new_url and new_url != stream_url:
                        print("Stream URL changed, restarting recording...")
                        ffmpeg_process.terminate()
                        ffmpeg_process.wait()
                        stream_url = new_url
                        # При смене URL также создаём новый файл (опционально)
                        new_filename = get_next_filename()
                        ffmpeg_cmd = build_ffmpeg_cmd(stream_url, new_filename)
                        ffmpeg_process = subprocess.Popen(ffmpeg_cmd)
                except Exception as e:
                    print(f"Periodic URL check failed: {e}")
        time.sleep(1)

except KeyboardInterrupt:
    print("\nInterrupted by user. Stopping recording...")
    ffmpeg_process.terminate()
    ffmpeg_process.wait()

print("Recording finished.")