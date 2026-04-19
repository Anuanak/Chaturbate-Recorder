import requests
import subprocess
import sys

# Configuration
ROOM_SLUG = "seltin_sweety"
API_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"

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
    else:
        print(f"Error: Room status is '{data.get('room_status')}'. Cannot record.")
        sys.exit(1)

except Exception as e:
    print(f"Failed to get stream URL: {e}")
    sys.exit(1)

# 2. Record with ffmpeg
ffmpeg_cmd = [
    "ffmpeg",
    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "-headers", "Referer: https://chaturbate.com/\r\n",
    "-i", stream_url,
    "-c", "copy",
    f"{ROOM_SLUG}_recording.mp4"
]

print(f"Starting recording for {ROOM_SLUG}...")
subprocess.run(ffmpeg_cmd)
