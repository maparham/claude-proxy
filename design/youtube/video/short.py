"""Vertical YouTube Short (1080x1920, about 58 s) cut from the promo: each scene reframed around its action.

Reads out/seg/video.mp4 (build.py's cut before captions) and out/timeline.json; renders its own header and
captions; music as clicks.py (Moonlight Sonata at 30%) with a click on each pointer click. Output: out/short.mp4
"""
import json
import pathlib
import subprocess
import wave

import numpy as np

import build_render as br   # render() and ff() without running build.py

ROOT = pathlib.Path(__file__).resolve().parent
OUT, SEG = ROOT / "out", ROOT / "out" / "seg"
SRC = SEG / "video.mp4"
W, H, FPS = 1080, 1920, 30
tl = json.loads((OUT / "timeline.json").read_text())
S = {k: v[0] for k, v in tl["starts"].items()}

SHOT = ("shot", 864, 1080, 960, 1200, 60, 360)   # crop w,h -> scaled w,h at x,y
TERM = ("term", 1400, 1080, 1080, 833, 0, 470)
# scene, from, to (seconds into the scene), crop x, layout, speed, caption
PIECES = [
    ("s1", 0.9, 5.6, 528, SHOT, 1.1, None),
    ("s2", 1.6, 4.6, 968, SHOT, 1.0, "اشتراک کلاد، با پرداخت ریالی"),
    ("b_pricing", 0.6, 3.6, 246, SHOT, 1.0, "بسته را انتخاب کنید"),
    ("b_visitor", 0.4, 3.2, 892, SHOT, 1.0, "بسته را انتخاب کنید"),
    ("b_signup", 0.9, 3.4, 474, SHOT, 1.0, "ثبت نام با گوگل، گیت‌هاب یا ایمیل"),
    ("b_order", 1.3, 4.3, 886, SHOT, 1.0, "پرداخت ریالی با زرین‌پال"),
    ("b_zarin", 0.9, 3.4, 612, SHOT, 1.0, "پرداخت ریالی با زرین‌پال"),
    ("b_paid", 1.0, 3.5, 978, SHOT, 1.0, "بسته شما فعال شد ✔"),
    ("t_install", 0.0, 8.2, 40, TERM, 2.0, "نصب با یک دستور"),
    ("auth", 0.5, 3.5, 528, SHOT, 1.0, "تأیید در مرورگر"),
    ("t_install2", 0.3, 3.3, 40, TERM, 1.0, "نصب با یک دستور"),
    ("t_prompt", 0.0, 15.4, 40, TERM, 2.0, "اولین درخواست به کلاد"),
    ("t_work", 0.0, 8.07, 40, TERM, 2.7, "کلاد خودش کد را می‌نویسد…"),
    ("t_write", 0.0, 5.0, 40, TERM, 2.5, "کلاد خودش کد را می‌نویسد…"),
    ("t_done", 0.0, 3.0, 40, TERM, 1.0, "کلاد خودش کد را می‌نویسد…"),
    ("bakery", 0.3, 4.8, 528, SHOT, 1.0, "نتیجه: یک صفحه کامل فارسی"),
    ("s5", 0.5, 4.5, 528, SHOT, 1.0, None),
]

# ---------- background, header, captions ----------
BG_HTML = f"""<div style="position:absolute;inset:0;background:radial-gradient(900px 700px at 80% 20%,rgba(235,104,52,.16),transparent 70%),
radial-gradient(800px 700px at 10% 85%,rgba(235,104,52,.10),transparent 70%),#faf6f3"></div>
<div style="position:absolute;top:120px;left:0;right:0;display:flex;flex-direction:column;align-items:center;gap:14px">
 <div style="display:flex;align-items:center;gap:18px"><div style="width:84px;height:84px;border-radius:22px;background:#eb6834;color:#fff;
  font-weight:900;font-size:24px;display:flex;align-items:center;justify-content:center">راهکار</div>
  <div style="font-size:56px;font-weight:900">اشتراک <span style="color:#eb6834">کلاد</span></div></div>
 <div style="direction:ltr;font-family:'JetBrains Mono',monospace;font-size:40px;font-weight:600;color:#eb6834">rahkar.pro</div></div>"""


def page(body, transparent=False):
    return br.render_size(body, W, H, transparent)


bg = page(BG_HTML)
caps = {}
for *_, c in PIECES:
    if c and c not in caps:
        caps[c] = page(f"""<div style="position:absolute;left:0;right:0;top:1440px;display:flex;justify-content:center">
<div style="background:#eb6834;color:#fff;font-size:52px;font-weight:800;padding:16px 44px;border-radius:20px;max-width:960px;text-align:center">{c}</div></div>""",
                       transparent=True)

# ---------- cut the pieces ----------
segs, clicks, t = [], [], 0.0
for i, (scene, a, b, x, lay, speed, cap) in enumerate(PIECES):
    _, cw, chh, sw, sh, ox, oy = lay
    t0 = S[scene] + a
    dur = (b - a) / speed
    o = SEG / f"short_{i:02d}.mp4"
    fc = (f"[0:v]trim={t0}:{S[scene] + b},setpts=(PTS-STARTPTS)/{speed},crop={cw}:{chh}:{x}:0,scale={sw}:{sh},"
          f"format=rgba[c];[1:v][c]overlay={ox}:{oy}:shortest=1,fps={FPS},format=yuv420p,"
          f"fade=t=in:st=0:d=0.15,fade=t=out:st={max(dur - 0.15, 0)}:d=0.15[o]")
    br.ff("-i", str(SRC), "-loop", "1", "-i", str(bg), "-filter_complex", fc, "-map", "[o]", "-an",
          "-t", f"{dur:.3f}", "-r", str(FPS), str(o))
    segs.append((o, dur, cap))
    for tc in tl["clicks"].get(scene, []):
        if a <= tc <= b:
            clicks.append(t + (tc - a) / speed)
    t += dur
total = t

lst = SEG / "short.txt"
lst.write_text("".join(f"file '{p}'\n" for p, _, _ in segs))
video = SEG / "short_video.mp4"
br.ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video))

# captions over runs of pieces sharing one
runs, t = [], 0.0
for _, d, c in segs:
    if c and runs and runs[-1][0] == c and abs(runs[-1][2] - t) < 1e-6:
        runs[-1][2] = t + d
    elif c:
        runs.append([c, t, t + d])
    t += d
inputs, vfc, v = [], [], "[0:v]"
for i, (c, a, b) in enumerate(runs, 1):
    inputs += ["-i", str(caps[c])]
    vfc.append(f"{v}[{i}:v]overlay=0:0:enable='between(t,{a + 0.1:.2f},{b:.2f})'[v{i}]")
    v = f"[v{i}]"

# ---------- sound: music at 30% plus clicks ----------
SR = 44100
rng = np.random.default_rng(7)
buf = np.zeros(int(total * SR) + SR)
for tc in clicks:
    tt = np.arange(int(0.06 * SR)) / SR
    c = (0.35 * np.diff(rng.standard_normal(len(tt)), prepend=0) * np.exp(-tt * 900)
         + 0.5 * np.sin(2 * np.pi * 2300 * tt) * np.exp(-tt * 400) + 0.6 * np.sin(2 * np.pi * 140 * tt) * np.exp(-tt * 120))
    i = int(tc * SR)
    buf[i:i + len(c)] += c
buf *= 0.6 / max(np.abs(buf).max(), 1e-9)
cw_ = OUT / "short_clicks.wav"
with wave.open(str(cw_), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((buf * 32767).astype("<i2").tobytes())
MUSIC = pathlib.Path.home() / "Downloads" / "Moonlight Sonata (Beethoven).mp3"
n = len(runs)
afc = (f"[{n + 1}:a]atrim=0:{total},afade=t=in:d=0.3,afade=t=out:st={total - 3}:d=3,loudnorm=I=-13:TP=-1:LRA=7,"
       f"volume=0.3,aresample={SR},aformat=channel_layouts=stereo[m];[{n + 2}:a]aformat=channel_layouts=stereo[k];"
       f"[m][k]amix=inputs=2:normalize=0,alimiter=limit=0.95[a]")
OUTF = OUT / "short.mp4"
br.ff("-i", str(video), *inputs, "-i", str(MUSIC), "-i", str(cw_), "-filter_complex", ";".join(vfc + [afc]),
      "-map", v, "-map", "[a]", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
      "-t", f"{total:.2f}", "-movflags", "+faststart", str(OUTF))
print(OUTF, f"{total:.1f}s", "clicks:", [round(c, 1) for c in clicks])
