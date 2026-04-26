import requests
import subprocess
import sys
import datetime
import hashlib
import time
import urllib.parse

# Configuration
ROOM_SLUG = "seltin_sweety"
AUTO_REFRESH = False
AUTO_REFRESH_INTERVAL = 300  # seconds
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

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

# 1. Get a fresh stream URL
headers = {"X-Requested-With": "XMLHttpRequest"}
payload = {"room_slug": ROOM_SLUG}

try:
    response = requests.post(API_URL, headers=headers, data=payload)
    response.raise_for_status()
    data = response.json()

    if data.get("success") and data.get("room_status") == "public":
        stream_url = data.get("url")
        print(f"Success! Got stream URL for {ROOM_SLUG}.")
        
        # Select the best quality stream
        try:
            playlist_response = requests.get(stream_url)
            playlist_response.raise_for_status()
            playlist = playlist_response.text
            lines = playlist.split('\n')
            max_bandwidth = 0
            best_url = None
            for i, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF'):
                    if 'BANDWIDTH=' in line:
                        bandwidth_str = line.split('BANDWIDTH=')[1].split(',')[0]
                        bandwidth = int(bandwidth_str)
                        if bandwidth > max_bandwidth:
                            max_bandwidth = bandwidth
                            best_url = lines[i+1].strip()
            if best_url:
                if not best_url.startswith('http'):
                    stream_url = urllib.parse.urljoin(stream_url, best_url)
                else:
                    stream_url = best_url
                print(f"Selected best quality stream: {stream_url}")
            else:
                print("Could not find stream variants, using default.")
        except Exception as e:
            print(f"Failed to select best quality: {e}")
        
        # Generate filename with date and hash prefix
        now = datetime.datetime.now()
        date_str = now.strftime("%m.%d.%Y")
        hash_str = hashlib.md5(ROOM_SLUG.encode()).hexdigest()[:8]
        filename = f"{date_str}_{hash_str}_{ROOM_SLUG}_recording.mp4"
    elif data.get("room_status") == "ticket":
        print("Room is in ticket mode. Cannot record without payment.")
        sys.exit(1)
    else:
        print(f"Error: Room status is '{data.get('room_status')}'. Cannot record.")
        sys.exit(1)

except Exception as e:
    print(f"Failed to get stream URL: {e}")
    sys.exit(1)

# 2. Record with ffmpeg
# Check if ffmpeg is available
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except (subprocess.CalledProcessError, FileNotFoundError):
    print("Error: FFmpeg is not installed or not in PATH. Please install FFmpeg from https://ffmpeg.org/ and add it to your PATH.")
    sys.exit(1)

ffmpeg_cmd = [
    "ffmpeg",
    "-f", "hls",
    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "-headers", "Referer: https://chaturbate.com/\r\n",
    "-i", stream_url,
    "-c", "copy",
    filename
]

print(f"Starting recording for {ROOM_SLUG}...")
ffmpeg_process = subprocess.Popen(ffmpeg_cmd)

if AUTO_REFRESH:
    print(f"Auto-refresh enabled: checking stream URL every {AUTO_REFRESH_INTERVAL} seconds.")
    while True:
        time.sleep(AUTO_REFRESH_INTERVAL)
        try:
            response = requests.post(API_URL, headers=headers, data=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("success") and data.get("room_status") == "public":
                new_stream_url = data.get("url")
                # Select best quality for new URL
                try:
                    playlist_response = requests.get(new_stream_url)
                    playlist_response.raise_for_status()
                    playlist = playlist_response.text
                    lines = playlist.split('\n')
                    max_bandwidth = 0
                    best_url = None
                    for i, line in enumerate(lines):
                        if line.startswith('#EXT-X-STREAM-INF'):
                            if 'BANDWIDTH=' in line:
                                bandwidth_str = line.split('BANDWIDTH=')[1].split(',')[0]
                                bandwidth = int(bandwidth_str)
                                if bandwidth > max_bandwidth:
                                    max_bandwidth = bandwidth
                                    best_url = lines[i+1].strip()
                    if best_url:
                        if not best_url.startswith('http'):
                            new_stream_url = urllib.parse.urljoin(new_stream_url, best_url)
                        else:
                            new_stream_url = best_url
                except Exception as e:
                    print(f"Failed to select best quality for update: {e}")
                
                if new_stream_url != stream_url:
                    print("Stream URL updated, restarting recording...")
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait()
                    stream_url = new_stream_url
                    ffmpeg_cmd[8] = stream_url  # update the input URL
                    ffmpeg_process = subprocess.Popen(ffmpeg_cmd)
            elif data.get("room_status") == "ticket":
                print("Room switched to ticket mode. Stopping recording.")
                ffmpeg_process.terminate()
                ffmpeg_process.wait()
                break
            else:
                print("Room not public anymore, stopping recording.")
                ffmpeg_process.terminate()
                ffmpeg_process.wait()
                break
        except Exception as e:
            print(f"Failed to update stream URL: {e}")
            # Continue trying
else:
    print("Auto-refresh disabled. Recording current stream URL only.")
    ffmpeg_process.wait()
