"""Vertical YouTube Short in phone view (1080x1920): the site captured in an iPhone-sized browser (mobile/cap_*.py)
with fingertip taps, and the terminal recorded in a tall window (tapes/v_install.tape, tapes/v_prompt.tape).
Music as clicks.py (Moonlight Sonata at 30%) with a click on each tap. Output: out/short-mobile.mp4
"""
import json
import pathlib
import wave

import numpy as np

import build_render as br
import cursor_fx

ROOT = pathlib.Path(__file__).resolve().parent
OUT, SEG, SHOTS = ROOT / "out", ROOT / "out" / "seg", ROOT / "mobile" / "shots"
SEG.mkdir(parents=True, exist_ok=True)
W, H, FPS = 1080, 1920, 30
TAPS = {**json.load(open(SHOTS / "taps_live.json")), **json.load(open(SHOTS / "taps_order.json"))}
INSTALL, PROMPT = OUT / "v-install.mp4", OUT / "v-prompt.mp4"
PATCH = ROOT / "scenes" / "v_install_patch.png"
ORANGE = "#eb6834"


def card(name, body):
    return br.render_size(f"""<div style="position:absolute;inset:0;background:radial-gradient(900px 700px at 80% 20%,rgba(235,104,52,.16),transparent 70%),
radial-gradient(800px 700px at 10% 85%,rgba(235,104,52,.10),transparent 70%),#faf6f3;display:flex;flex-direction:column;
align-items:center;justify-content:center;gap:40px;padding-bottom:160px">{body}</div>""", W, H)


def caption(text, top):
    return br.render_size(f"""<div style="position:absolute;left:0;right:0;top:{top}px;display:flex;justify-content:center">
<div style="background:{ORANGE};color:#fff;font-size:54px;font-weight:800;padding:16px 46px;border-radius:22px;max-width:980px;
text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.18)">{text}</div></div>""", W, H, transparent=True)


hook = card("hook", f"""<div style="font-size:120px;font-weight:900">کلاد می‌خوای؟</div>
<div style="font-size:84px;font-weight:800;text-align:center">ولی <span style="color:{ORANGE}">پرداخت ارزی</span><br>نداری؟</div>""")
end = card("end", f"""<div style="display:flex;align-items:center;gap:26px"><div style="width:150px;height:150px;border-radius:36px;
background:{ORANGE};color:#fff;font-weight:900;font-size:44px;display:flex;align-items:center;justify-content:center">راهکار</div>
<div style="font-size:64px;font-weight:800">راهکار هوش برتر</div></div>
<div style="font-size:130px;font-weight:900">اشتراک <span style="color:{ORANGE}">کلاد</span></div>
<div style="direction:ltr;font-family:'JetBrains Mono',monospace;font-size:96px;font-weight:600;color:{ORANGE}">rahkar.pro</div>
<div style="font-size:46px;font-weight:600;color:#4a4542">پرداخت ریالی · بدون فیلترشکن</div>""")
bakery = OUT / "bakery-mobile.png"

TOP, LOW = 150, 1470   # caption tops: over the page's header on screenshots, in the empty terminal below the text


def still(name, png, dur, tap_at=None):
    """A screenshot held for dur seconds with a gentle push-in; tap_at: when the fingertip taps TAPS[name]."""
    path = [(0, *TAPS[name], False), (tap_at, *TAPS[name], True)] if tap_at is not None else [(0, -500, -500, False)]
    d = cursor_fx.render(png, SEG / f"m_{name}_cur", dur, path, FPS, touch=True)
    n = int(dur * FPS)
    o = SEG / f"m_{name}.mp4"
    br.ff("-framerate", str(FPS), "-i", str(d / "%04d.png"), "-vf",
          f"scale={W*2}:{H*2},zoompan=z='1+0.03*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
          "format=yuv420p", "-r", str(FPS), str(o))
    return o, dur, ([tap_at + 0.15] if tap_at is not None else [])


def term(name, src, cuts, patch=False):
    """cuts: [(from, to, speed)] pieces of the recording joined into one clip."""
    parts, dur = [], 0.0
    for i, (a, b, sp) in enumerate(cuts):
        parts.append(f"[0:v]trim={a}:{b},setpts=(PTS-STARTPTS)/{sp}[p{i}]")
        dur += (b - a) / sp
    fc = ";".join(parts) + ";" + "".join(f"[p{i}]" for i in range(len(cuts))) + f"concat=n={len(cuts)}:v=1:a=0[c]"
    extra = []
    if patch:   # the account email on the "Authorized as" line becomes you@example.com (same font, tapes/v_patch.tape)
        extra = ["-loop", "1", "-i", str(PATCH)]
        fc += (";[1:v]crop=980:29:40:827[m];[c][m]overlay=40:827:shortest=1,"
               "drawbox=x=300:y=856:w=400:h=4:color=black@1:t=fill,drawbox=x=458:y=855:w=20:h=9:color=black@1:t=fill[c2]")
        last = "[c2]"
    else:
        last = "[c]"
    fc += f";{last}fps={FPS},format=yuv420p[o]"
    o = SEG / f"m_{name}.mp4"
    br.ff("-i", str(src), *extra, "-filter_complex", fc, "-map", "[o]", "-an", "-t", f"{dur:.3f}", str(o))
    return o, dur, []


T = json.load(open(OUT / "v-prompt-times.json"))   # measured on the take: when gclaude is open, typing, writing, done
pieces = [
    ("hook", still("hook", hook, 3.2), None),
    ("home", still("m_home", SHOTS / "m_home.png", 3.4, 2.1), caption("اشتراک کلاد با پرداخت ریالی", TOP)),
    ("pricing", still("m_pricing", SHOTS / "m_pricing.png", 3.6, 2.2), caption("بسته را انتخاب کنید", TOP)),
    ("visitor", still("m_visitor", SHOTS / "m_visitor.png", 3.2, 1.9), caption("بسته را انتخاب کنید", TOP)),
    ("signup", still("m_signup", SHOTS / "m_signup.png", 3.0, 1.8), caption("ثبت نام با گوگل، گیت‌هاب یا ایمیل", 1180)),
    ("order", still("m_order", SHOTS / "m_order.png", 3.6, 2.3), caption("پرداخت ریالی با زرین‌پال", TOP)),
    ("zarin", still("m_zarin", SHOTS / "m_zarin.png", 3.0, 1.8), caption("پرداخت ریالی با زرین‌پال", TOP)),
    ("paid", still("m_paid", SHOTS / "m_paid_raw.png", 3.0, 1.9), caption("بسته شما فعال شد ✔", 1400)),
    ("install", term("install", INSTALL, [(0.5, 4.3, 1.0), (10.1, 12.6, 1.0)]), caption("نصب با یک دستور", LOW)),
    ("auth", still("m_auth", SHOTS / "m_auth.png", 3.0, 1.9), caption("تأیید در مرورگر", TOP)),
    ("install2", term("install2", INSTALL, [(85.9, 89.9, 1.0)], patch=True), caption("نصب با یک دستور", LOW)),
    ("prompt", term("prompt", PROMPT, [(0.3, T["open"] + 0.6, 1.0), (T["type0"] - 0.2, T["sent"] + 1.0, 1.6)]),
     caption("اولین درخواست به کلاد", LOW)),
    ("work", term("work", PROMPT, [(T["sent"] + 1.0, T["write"], 14.0), (T["write"], T["done"], 4.0),
                                   (T["done"], T["done"] + 3.0, 1.0)]), caption("کلاد خودش کد را می‌نویسد…", LOW)),
    ("bakery", still("bakery", bakery, 4.0), caption("نتیجه: یک صفحه کامل فارسی", 1400)),
    ("end", still("end", end, 4.0), None),
]

faded, starts, clicks, t = [], [], [], 0.0
for name, (p, d, taps), cap in pieces:
    o = SEG / f"m_{name}_f.mp4"
    br.ff("-i", str(p), "-vf", f"fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.15,fade=t=out:st={max(d - 0.15, 0)}:d=0.15",
          "-an", "-r", str(FPS), str(o))
    faded.append(o)
    starts.append((t, t + d, cap))
    clicks += [t + c for c in taps]
    t += d
total = t
lst = SEG / "m_all.txt"
lst.write_text("".join(f"file '{p}'\n" for p in faded))
video = SEG / "m_video.mp4"
br.ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video))

# captions: one overlay per run of pieces sharing a caption image
runs = []
for a, b, cap in starts:
    if cap is None:
        continue
    if runs and runs[-1][0] == cap and abs(runs[-1][2] - a) < 1e-6:
        runs[-1][2] = b
    else:
        runs.append([cap, a, b])
inputs, vfc, v = [], [], "[0:v]"
for i, (cap, a, b) in enumerate(runs, 1):
    inputs += ["-i", str(cap)]
    vfc.append(f"{v}[{i}:v]overlay=0:0:enable='between(t,{a + 0.15:.2f},{b - 0.05:.2f})'[v{i}]")
    v = f"[v{i}]"

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
cw = OUT / "short_mobile_clicks.wav"
with wave.open(str(cw), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((buf * 32767).astype("<i2").tobytes())
MUSIC = pathlib.Path.home() / "Downloads" / "Moonlight Sonata (Beethoven).mp3"
n = len(runs)
afc = (f"[{n + 1}:a]atrim=0:{total},afade=t=in:d=0.3,afade=t=out:st={total - 3}:d=3,loudnorm=I=-13:TP=-1:LRA=7,"
       f"volume=0.3,aresample={SR},aformat=channel_layouts=stereo[m];[{n + 2}:a]aformat=channel_layouts=stereo[k];"
       f"[m][k]amix=inputs=2:normalize=0,alimiter=limit=0.95[a]")
OUTF = OUT / "short-mobile.mp4"
br.ff("-i", str(video), *inputs, "-i", str(MUSIC), "-i", str(cw), "-filter_complex", ";".join(vfc + [afc]),
      "-map", v, "-map", "[a]", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
      "-t", f"{total:.2f}", "-movflags", "+faststart", str(OUTF))
print(OUTF, f"{total:.1f}s", [(round(a, 1), round(b, 1)) for a, b, _ in starts])
