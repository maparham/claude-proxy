"""Helpers shared by short.py: headless-Chrome rendering of an HTML body at any size, and ffmpeg."""
import hashlib
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
FR = ROOT / "frames"
FR.mkdir(exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HEAD = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800;900&family=JetBrains+Mono:wght@400;600&display=block" rel="stylesheet">
<style>*{margin:0;padding:0;box-sizing:border-box}html,body{width:%dpx;height:%dpx;overflow:hidden;background:none}
body{font-family:Vazirmatn,sans-serif;color:#1d1b1a;position:relative}</style></head><body>"""


def render_size(body, w, h, transparent=False):
    html = HEAD % (w, h) + body + "</body></html>"
    key = hashlib.sha1(f"{w}x{h}{transparent}{html}".encode()).hexdigest()[:12]
    src, png = FR / f"r_{key}.html", FR / f"r_{key}.png"
    src.write_text(html, encoding="utf-8")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
           f"--window-size={w},{h}", "--virtual-time-budget=6000", "--user-data-dir=/tmp/yt-video-chrome",
           f"--screenshot={png}", src.as_uri()]
    if transparent:
        cmd.insert(1, "--default-background-color=00000000")
    subprocess.run(cmd, check=True, capture_output=True)
    return png


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)
