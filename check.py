import subprocess, sys

url = "https://edge31-waw.live.mmcdn.com/v1/edge/streams/origin.seltin_sweety.01KRRYT52B6Y102P9FHHZ42T43/llhls.m3u8"
cmd = [
    "ffmpeg",
    "-f", "hls",
    "-user_agent", "Mozilla/5.0",
    "-headers", "Referer: https://chaturbate.com/\r\n",
    "-i", url,
    "-map", "0",
    "-c", "copy",
    "-bsf:v", "h264_mp4toannexb",
    "-f", "mpegts",
    "-y", "test_with_audio.ts"
]
subprocess.run(cmd)