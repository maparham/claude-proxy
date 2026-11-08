"""Build the rahkar.pro promo video (silent, for background music added later).

Renders HTML scenes with headless Chrome, cuts the real vhs terminal recordings in out/
(tapes/install.tape, tapes/prompt.tape), overlays Persian captions. Output: out/promo-v2-silent.mp4
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
FR = ROOT / "frames"
OUT = ROOT / "out"
SEG = OUT / "seg"
for d in (FR, OUT, SEG):
    d.mkdir(parents=True, exist_ok=True)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, H, FPS = 1920, 1080, 30
ORANGE = "#eb6834"

HEAD = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800;900&family=JetBrains+Mono:wght@400;600&display=block" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{W}px;height:{H}px;overflow:hidden}}
body{{font-family:Vazirmatn,sans-serif;color:#1d1b1a;
 background:radial-gradient(1000px 600px at 75% 40%,rgba(235,104,52,.12),transparent 70%),
 radial-gradient(800px 500px at 20% 70%,rgba(235,104,52,.07),transparent 70%),#faf6f3}}
.center{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:36px;padding-bottom:120px}}
.o{{color:{ORANGE}}}
.hidden{{visibility:hidden}}
.term{{direction:ltr;width:1180px;border-radius:24px;background:#1c1c1e;box-shadow:0 40px 80px rgba(0,0,0,.25);
 overflow:hidden;font-family:"JetBrains Mono",monospace;color:#e8e6e3;font-size:30px}}
.bar{{height:52px;background:#2a2a2d;display:flex;gap:12px;align-items:center;padding:0 22px}}
.bar i{{width:17px;height:17px;border-radius:50%;display:block}}
.tb{{padding:28px 36px 34px;line-height:1.65;min-height:520px}}
.dim{{color:#8b8885}} .ok{{color:#4cc26b}} .add{{color:#4cc26b}} .rm{{color:#ff6b6b}}
.cur{{display:inline-block;width:16px;height:32px;background:#e8e6e3;vertical-align:-6px}}
</style></head><body>"""
TAIL = "</body></html>"


def render(name, body, transparent=False):
    html = FR / f"{name}.html"
    png = FR / f"{name}.png"
    html.write_text(HEAD + body + TAIL, encoding="utf-8")
    if transparent:
        html.write_text(html.read_text(encoding="utf-8").replace(
            "background:radial-gradient", "background:none;--x:radial-gradient"), encoding="utf-8")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1", f"--window-size={W},{H}",
           "--virtual-time-budget=6000", "--user-data-dir=/tmp/yt-video-chrome",
           f"--screenshot={png}", html.as_uri()]
    if transparent:
        cmd.insert(1, "--default-background-color=00000000")
    subprocess.run(cmd, check=True, capture_output=True)
    return png


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def still_seq(name, frames, zoom=True):
    """frames: list of (png, seconds). Concats stills, adds a gentle push-in."""
    lst = SEG / f"{name}.txt"
    lst.write_text("".join(f"file '{p}'\nduration {d}\n" for p, d in frames)
                   + f"file '{frames[-1][0]}'\n")
    total = sum(d for _, d in frames)
    n = int(total * FPS)
    vf = f"fps={FPS},scale={W}:{H},format=yuv420p"
    if zoom:
        vf = (f"fps={FPS},scale={W*2}:{H*2},zoompan=z='1+0.04*on/{n}':x='iw/2-(iw/zoom/2)':"
              f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},format=yuv420p")
    out = SEG / f"{name}.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-vf", vf, "-t", f"{total}", "-r", str(FPS), str(out))
    return out, total


# ---------- scene 1: hook ----------
s1a = render("s1a", """<div class="center">
<div style="font-size:110px;font-weight:900">کلاد می‌خوای؟</div>
<div class="hidden" style="font-size:72px;font-weight:800">ولی <span class="o">پرداخت ارزی</span> نداری؟</div></div>""")
s1b = render("s1b", """<div class="center">
<div style="font-size:110px;font-weight:900">کلاد می‌خوای؟</div>
<div style="font-size:72px;font-weight:800">ولی <span class="o">پرداخت ارزی</span> نداری؟</div></div>""")

# ---------- scene 2: the site ----------
site = ROOT / "scenes" / "site.png"
s2 = FR / "s2.png"
ff("-i", str(site), "-vf", f"scale={W}:{H}", str(s2))

# ---------- scene 3: three promises ----------
items = ["پرداخت ریالی", "بدون فیلترشکن", "بدون احراز هویت", "راه‌اندازی فوری"]
s3 = []
for k in range(1, len(items) + 1):
    rows = "".join(
        f'<div class="{"" if i < k else "hidden"}" style="font-size:80px;font-weight:800;display:flex;gap:28px;align-items:center">'
        f'<span class="o" style="font-size:72px">✔</span>{t}</div>'
        for i, t in enumerate(items))
    s3.append(render(f"s3_{k}", f'<div class="center" style="align-items:center;gap:22px">'
                     f'<div style="display:flex;flex-direction:column;gap:22px">{rows}</div></div>'))

# ---------- scene 5: end card ----------
s5 = render("s5", f"""<div class="center" style="gap:30px">
<div style="display:flex;align-items:center;gap:26px">
 <div style="width:150px;height:150px;border-radius:36px;background:{ORANGE};color:#fff;font-weight:900;font-size:44px;
  display:flex;align-items:center;justify-content:center;box-shadow:0 10px 30px rgba(235,104,52,.4)">راهکار</div>
 <div style="font-size:64px;font-weight:800">راهکار هوش برتر</div></div>
<div style="font-size:120px;font-weight:900">اشتراک <span class="o">کلاد</span></div>
<div style="direction:ltr;font-family:'JetBrains Mono',monospace;font-size:84px;font-weight:600" class="o">rahkar.pro</div>
<div style="font-size:40px;font-weight:600;color:#4a4542">Opus · Sonnet · ترمینال و VS Code</div></div>""")

# ---------- captions (transparent overlays) ----------
CAP = [
    "می‌خوای از کلاد استفاده کنی ولی پرداخت ارزی نداری؟",
    "توی راهکار، اشتراک کلاد رو با پرداخت ریالی می‌خری.",
    "بدون فیلترشکن، بدون احراز هویت، راه‌اندازی فوری.",
    "یه دستور بدی، کلاد خودش کد رو می‌نویسه، تست می‌کنه و تحویل می‌ده.",
    "همین الان از rahkar.pro شروع کن.",
]
caps = []
for i, c in enumerate(CAP, 1):
    caps.append(render(f"cap{i}", f"""<div style="position:absolute;left:0;right:0;bottom:56px;display:flex;justify-content:center">
<div style="background:rgba(20,18,17,.82);color:#fff;font-size:46px;font-weight:600;padding:16px 40px;border-radius:18px;max-width:1700px;text-align:center">{c}</div></div>""",
                       transparent=True))

# ---------- terminal captions (real recordings) ----------
TCAP = ["نصب با یک دستور، و تأیید در مرورگر",
        "اولین درخواست به کلاد، با gclaude",
        "کلاد خودش کد را می‌نویسد…",
        "نتیجه: یک صفحه کامل، در کمتر از دو دقیقه"]
tcaps = []
for i, c in enumerate(TCAP, 1):
    tcaps.append(render(f"tcap{i}", f"""<div style="position:absolute;left:0;right:0;bottom:44px;display:flex;justify-content:center">
<div style="background:{ORANGE};color:#fff;font-size:42px;font-weight:700;padding:12px 38px;border-radius:16px;text-align:center">{c}</div></div>""",
                        transparent=True))

acap = render("acap", f"""<div style="position:absolute;left:0;right:0;bottom:44px;display:flex;justify-content:center">
<div style="background:{ORANGE};color:#fff;font-size:42px;font-weight:700;padding:12px 38px;border-radius:16px">در مرورگر، کد را بررسی و تأیید کنید</div></div>""", transparent=True)

# ---------- real terminal clips (vhs recordings in out/) ----------
INSTALL, PROMPT = OUT / "term-install.mp4", OUT / "term-prompt.mp4"
BLACK = "color=black@1:t=fill"


def clip(name, src, start, end, speed, boxes, logo_fix=False):
    """Cut [start,end) of src, hide boxes (x,y,w,h[,t0,t1] in source time), play at speed x.
    logo_fix: a stray typed "B" pushed the top half of Claude Code's logo one cell right; cover it with a clean one."""
    db = []
    for b in boxes:
        x, y, w, h = b[:4]
        en = f":enable='between(t,{b[4]},{b[5]})'" if len(b) > 4 else ""
        db.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:{BLACK}{en}")
    tail = ",".join(db + [f"trim={start}:{end}", f"setpts=(PTS-STARTPTS)/{speed}", f"fps={FPS}", "format=yuv420p"])
    if logo_fix:   # paste a clean logo (from tapes/logo.tape, same font and size) over the broken one
        fc = (f"[1:v]format=rgba[p];[0:v][p]overlay=66:148:enable='between(t,9.6,86)',{tail}[o]")
    else:
        fc = f"[0:v]{tail}[o]"
    o = SEG / f"{name}.mp4"
    extra = ["-i", str(ROOT / "scenes" / "logo_patch.png")] if logo_fix else []
    ff("-i", str(src), *extra, "-filter_complex", fc, "-map", "[o]", "-an", str(o))
    return o, round((end - start) / speed, 3)


# install take (live): "~/project % " becomes "$ " (row shifted left, "$" from a vhs frame in the same font),
# and the account email becomes you@example.com (a line from the same vhs frame, tapes/patch.tape).
PATCH = ROOT / "scenes" / "install_patch.png"


def install_clip(name, start, end, speed):
    tail = f"trim={start}:{end},setpts=(PTS-STARTPTS)/{speed},fps={FPS},format=yuv420p"
    fc = ("[1:v]split[p1][p2];[p1]crop=36:37:74:113[dol];[dol]split[d1][d2];"
          "[p2]crop=700:35:74:821[mail];"
          "[0:v]split[a][b];[b]crop=1576:37:284:113[row];"
          "[a]drawbox=x=74:y=113:w=1790:h=37:color=black@1:t=fill[c];"
          "[c][row]overlay=111:113:shortest=1[c2];[c2][d1]overlay=74:113[c3];"
          "[c3]drawbox=x=74:y=957:w=240:h=36:color=black@1:t=fill[c4];"
          "[c4][d2]overlay=74:954:enable='gte(t,15.18)'[c5];"
          f"[c5][mail]overlay=74:821:enable='gte(t,13.95)',drawbox=x=330:y=855:w=180:h=4:color=black@1:t=fill:enable='gte(t,13.95)',{tail}[o]")
    o = SEG / f"{name}.mp4"
    ff("-i", str(INSTALL), "-loop", "1", "-i", str(PATCH), "-filter_complex", fc, "-map", "[o]", "-an",
       "-t", f"{(end - start) / speed:.3f}", str(o))
    return o, round((end - start) / speed, 3)


PROMPT2 = OUT / "term-prompt2.mp4"   # tapes/prompt2.tape: gclaude through the local demo gateway (you@example.com)
t_install = install_clip("t_install", 0.5, 8.7, 1.0)          # up to "Waiting for you to authorize it there..."
t_install2 = install_clip("t_install2", 13.9, 17.88, 1.0)     # authorized, gclaude set up
# "gclaude" typed and Claude Code opening (0.3-3.3 s), then the idle wait cut, then the prompt typed and sent
_o = SEG / "t_prompt.mp4"
ff("-i", str(PROMPT2), "-vf", "trim=0.3:27.0,setpts=PTS-STARTPTS,select='not(between(t,3.0,14.3))',"
   f"setpts=N/{FPS}/TB,fps={FPS},format=yuv420p", "-an", str(_o))
t_prompt = (_o, round(3.0 + (27.0 - 0.3 - 14.3), 3))
t_work = clip("t_work", PROMPT2, 27.0, 83.5, 7.0, [])        # Claude working
t_write = clip("t_write", PROMPT2, 83.5, 98.5, 3.0, [])     # Write(index.html)
t_done = clip("t_done", PROMPT2, 98.5, 102.0, 1.0, [])       # the summary
# hold the last frame for 2.5 s
t_hold_png = SEG / "t_done_last.png"
ff("-sseof", "-0.1", "-i", str(t_done[0]), "-frames:v", "1", str(t_hold_png))

bakery = ROOT / "out" / "bakery2.png"   # the page this take built


# ---------- purchase stage (live site for pricing and sign-up; local demo gateway for the paid order) ----------
NORM = ROOT / "scenes" / "norm"


def framed(name, crop=None):
    """scenes/norm/<name>.png (16:9, 1920x1080), optionally zoomed to crop=(x, y, w, h) at 16:9."""
    out = FR / f"buy_{name}.png"
    vf = f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},scale={W}:{H}" if crop else f"scale={W}:{H}"
    ff("-i", str(NORM / f"{name}.png"), "-vf", vf, str(out))
    return out


b_pricing = framed("buy1")
b_visitor = framed("buy2", (400, 345, 1120, 630))
b_signup = framed("signup", (330, 120, 1120, 630))
b_order = framed("p4_order_ticked", (400, 225, 1120, 630))
b_zarin = framed("p5_zarinpal")
b_paid = framed("p6_paid", (320, 180, 1280, 720))
import cursor_fx


CLICKS = {}   # scene -> click times within it (the press, as cursor_fx draws it), for the sound track (audio.py)


def clicked(name, png, dur, path):
    """A still with the pointer gliding to the buttons and clicking (cursor_fx), then the usual push-in."""
    CLICKS[name] = [t + 0.15 for t, _, _, c in path if c]
    d = cursor_fx.render(png, SEG / f"{name}_cur", dur, path, FPS)
    n = int(dur * FPS)
    vf = (f"scale={W*2}:{H*2},zoompan=z='1+0.04*on/{n}':x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},format=yuv420p")
    out = SEG / f"{name}.mp4"
    ff("-framerate", str(FPS), "-i", str(d / "%04d.png"), "-vf", vf, "-r", str(FPS), str(out))
    return out, dur


BCAP = ["بسته روزانه یا هفتگی را انتخاب کنید", "با گوگل، گیت‌هاب یا ایمیل ثبت نام کنید",
        "پرداخت ریالی با زرین‌پال", "بسته شما فعال شد ✔"]
bcaps = [render(f"bcap{i}", f"""<div style="position:absolute;left:0;right:0;bottom:44px;display:flex;justify-content:center">
<div style="background:{ORANGE};color:#fff;font-size:42px;font-weight:700;padding:12px 38px;border-radius:16px">{c}</div></div>""",
                transparent=True) for i, c in enumerate(BCAP, 1)]

# ---------- assemble scenes ----------
segs = [
    ("s1", still_seq("s1", [(s1a, 1.8), (s1b, 3.8)])),
    ("s2", clicked("s2", s2, 6.0, [(0, 1700, 820, False), (3.8, 1400, 364, True)])),   # the hero button
    ("b_pricing", clicked("b_pricing", b_pricing, 4.6, [(0, 1250, 900, False), (2.4, 678, 476, True)])),
    ("b_visitor", clicked("b_visitor", b_visitor, 4.0, [(0, 900, 900, False), (2.0, 1324, 776, True)])),
    ("b_signup", clicked("b_signup", b_signup, 4.2, [(0, 1250, 880, False), (2.2, 906, 344, True)])),
    ("b_order", clicked("b_order", b_order, 4.8, [(0, 1100, 1000, False), (1.5, 1505, 769, False), (2.9, 1318, 842, True)])),
    ("b_zarin", clicked("b_zarin", b_zarin, 4.4, [(0, 1450, 960, False), (2.4, 1044, 824, True)])),
    ("b_paid", clicked("b_paid", b_paid, 4.0, [(0, 1700, 900, False), (2.4, 1410, 598, True)])),
    ("t_install", t_install),
    ("auth", still_seq("auth", [(ROOT / "scenes" / "authorize.png", 4.5)])),
    ("t_install2", t_install2),
    ("t_prompt", t_prompt),
    ("t_work", t_work),
    ("t_write", t_write),
    ("t_done", t_done),
    ("t_hold", still_seq("t_hold", [(t_hold_png, 3.0)], zoom=False)),
    ("bakery", still_seq("bakery", [(bakery, 6.0)])),
    ("s5", still_seq("s5", [(s5, 6.5)])),
]
starts, t = {}, 0.0
for name, (_, d) in segs:
    starts[name] = (t, t + d)
    t += d
total = t

faded = []
for name, (p, d) in segs:
    o = SEG / f"{name}_f.mp4"
    ff("-i", str(p), "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p,"
       f"fade=t=in:st=0:d=0.2,fade=t=out:st={max(d-0.2, 0)}:d=0.2", "-an", "-r", str(FPS), str(o))
    faded.append(o)
cl = SEG / "all.txt"
cl.write_text("".join(f"file '{p}'\n" for p in faded))
video = SEG / "video.mp4"
ff("-f", "concat", "-safe", "0", "-i", str(cl), "-c", "copy", str(video))

# captions: (png, start, end) on the final timeline
S = starts
overlays = [
    (caps[0], S["s1"][0] + 0.3, S["s1"][1]),
    (caps[1], S["s2"][0] + 0.2, S["s2"][1]),
    (bcaps[0], S["b_pricing"][0] + 0.2, S["b_visitor"][1]),
    (bcaps[1], S["b_signup"][0] + 0.2, S["b_signup"][1]),
    (bcaps[2], S["b_order"][0] + 0.2, S["b_zarin"][1]),
    (bcaps[3], S["b_paid"][0] + 0.2, S["b_paid"][1]),
    (tcaps[0], S["t_install"][0] + 0.3, S["t_install"][1]),
    (acap, S["auth"][0] + 0.2, S["auth"][1]),
    (tcaps[0], S["t_install2"][0], S["t_install2"][1]),
    (tcaps[1], S["t_prompt"][0] + 0.3, S["t_prompt"][1]),
    (tcaps[2], S["t_work"][0] + 0.2, S["t_hold"][1]),
    (tcaps[3], S["bakery"][0] + 0.2, S["bakery"][1]),
    (caps[4], S["s5"][0] + 0.3, S["s5"][1]),
]
inputs, vfc, v = [], [], "[0:v]"
for i, (png, a, b) in enumerate(overlays, 1):
    inputs += ["-i", str(png)]
    vfc.append(f"{v}[{i}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{i}]")
    v = f"[v{i}]"
OUTF = OUT / "promo-v2-silent.mp4"
ff("-i", str(video), *inputs, "-filter_complex", ";".join(vfc), "-map", v,
   "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an",
   "-t", f"{total:.2f}", "-movflags", "+faststart", str(OUTF))
import json
(OUT / "timeline.json").write_text(json.dumps({"total": total, "starts": starts, "clicks": CLICKS}, indent=1))
print(OUTF, f"{total:.1f}s")
for k, (a, b) in starts.items():
    print(f"  {k:10s} {a:5.1f}-{b:5.1f}")
