import requests, re, urllib.parse

ROOM_SLUG = "seltin_sweety"
url = "https://chaturbate.com/get_edge_hls_url_ajax/"
headers = {"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}
resp = requests.post(url, headers=headers, data={"room_slug": ROOM_SLUG}).json()
master = resp["url"]
content = requests.get(master).text

audio_found = False
for line in content.splitlines():
    if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
        print("AUDIO MEDIA:", line)
        audio_found = True
    if line.startswith("#EXT-X-STREAM-INF") and "mp4a" in line:
        print("STREAM WITH AUDIO:", line)
        audio_found = True
if not audio_found:
    print("Ни одной аудиодорожки не найдено – в стриме нет звука.")