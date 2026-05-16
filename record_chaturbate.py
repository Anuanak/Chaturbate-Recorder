#!/usr/bin/env python3
"""
Chaturbate Recorder Void — видео + аудио (исправленное связывание дорожек)
"""

import requests, subprocess, sys, datetime, time, os, signal, threading, re, urllib.parse, secrets

ROOM_SLUG = "seltin_sweety"
AUTO_REFRESH = False
AUTO_REFRESH_INTERVAL = 300
URL_FETCH_RETRIES = 3
FFMPEG_RESTART_DELAY = 5
OUTPUT_BASE_DIR = "recordings"
HANG_TIMEOUT = 60
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
    content = master_resp.text
    lines = content.splitlines()

    # Собираем аудио‑медиа: group_id -> url
    audio_groups = {}
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            group = re.search(r'GROUP-ID="([^"]*)"', line)
            uri   = re.search(r'URI="([^"]*)"', line)
            if group and uri:
                audio_groups[group.group(1)] = urllib.parse.urljoin(master_url, uri.group(1))

    # Собираем видео‑варианты: (bandwidth, url, audio_group)
    video_variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = int(re.search(r'BANDWIDTH=(\d+)', line).group(1)) if 'BANDWIDTH=' in line else 0
            audio_group = re.search(r'AUDIO="([^"]*)"', line)
            audio_group = audio_group.group(1) if audio_group else None
            if i+1 < len(lines) and not lines[i+1].startswith('#'):
                url = urllib.parse.urljoin(master_url, lines[i+1].strip())
                codecs_match = re.search(r'CODECS="([^"]*)"', line)
                codecs = codecs_match.group(1) if codecs_match else ""
                has_video = "avc" in codecs
                if has_video:
                    video_variants.append((bw, url, audio_group))

    if not video_variants:
        raise Exception("No video variants")

    # Берём видео с максимальным битрейтом
    video_variants.sort(key=lambda x: x[0], reverse=True)
    best_video = video_variants[0]
    video_url = best_video[1]
    audio_group = best_video[2]

    # Ищем аудио по group_id
    audio_url = audio_groups.get(audio_group) if audio_group else None
    return video_url, audio_url, room_status

def build_ffmpeg_cmd(video_url, audio_url, filename):
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
        cmd += ["-i", audio_url, "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0"]
    cmd += ["-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", "-y", filename]
    return cmd

def graceful_stop(process, timeout=10):
    if process.poll() is not None: return
    try:
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        process.send_signal(sig)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait()
    except:
        process.terminate()
        process.wait()

try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except:
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

now = datetime.datetime.now()
date_str = now.strftime("%m.%d.%Y")
unique_id = secrets.token_hex(4)
fname = f"{date_str}_{unique_id}_{ROOM_SLUG}_recording.ts"
fname = os.path.join(room_dir, fname)
print(f"Output: {fname}")
print(f"Audio: {'yes' if audio_url else 'no'}")

ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, fname)
ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)

def get_next_filename():
    return os.path.join(room_dir, f"{date_str}_{secrets.token_hex(4)}_{ROOM_SLUG}_recording.ts")

last_frame = time.time()
def read_stderr(pipe):
    global last_frame
    for line in iter(pipe.readline, ''):
        if 'frame=' in line: last_frame = time.time()
threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True).start()

last_url_check = time.time()
try:
    while True:
        poll = ffmpeg_process.poll()
        if poll is not None:
            print(f"FFmpeg exited with {poll}. Restarting...")
            video_url = audio_url = None
            while True:
                for _ in range(URL_FETCH_RETRIES):
                    try:
                        video_url, audio_url, room_status = fetch_best_stream()
                        if room_status == "public" and video_url: break
                        print(f"Status '{room_status}', wait 10s..."); time.sleep(10)
                    except Exception as e:
                        print(f"Refresh attempt failed: {e}"); time.sleep(2)
                if video_url: break
                time.sleep(30)
            new_fname = get_next_filename()
            ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, new_fname)
            print(f"Restarting → {new_fname} (audio: {'yes' if audio_url else 'no'})")
            ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
            last_frame = time.time()
            threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True).start()
            last_url_check = time.time()
        else:
            if time.time() - last_frame > HANG_TIMEOUT:
                print("Hung detected, restarting...")
                graceful_stop(ffmpeg_process)
                continue
            if AUTO_REFRESH and time.time() - last_url_check > AUTO_REFRESH_INTERVAL:
                last_url_check = time.time()
                try:
                    new_v, new_a, new_s = fetch_best_stream()
                    if new_s == "public" and new_v and (new_v != video_url or new_a != audio_url):
                        graceful_stop(ffmpeg_process)
                        video_url, audio_url = new_v, new_a
                        new_fname = get_next_filename()
                        ffmpeg_cmd = build_ffmpeg_cmd(video_url, audio_url, new_fname)
                        ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
                        last_frame = time.time()
                        threading.Thread(target=read_stderr, args=(ffmpeg_process.stderr,), daemon=True).start()
                except Exception as e:
                    print(f"Periodic check failed: {e}")
        time.sleep(2)
except KeyboardInterrupt:
    print("\nInterrupted, stopping...")
    graceful_stop(ffmpeg_process)
print("Finished.")