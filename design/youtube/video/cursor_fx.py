"""Animated mouse pointer with click ripples over a still screenshot (for the purchase screens)."""
import math
import pathlib
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFilter

SS = 4  # supersampling for smooth edges


def _pointer(scale=2.2):
    pts = [(0, 0), (0, 17), (4.2, 13.2), (7, 19.6), (9.6, 18.5), (6.9, 12.3), (12.4, 12.3)]
    k = scale * SS
    w, h = int(16 * k), int(23 * k)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    p = [(x * k + 2 * SS, y * k + 2 * SS) for x, y in pts]
    d.polygon(p, fill=(20, 20, 20, 255), outline=(255, 255, 255, 255), width=int(1.4 * k))
    return im.resize((w // SS, h // SS), Image.LANCZOS)


def _shadow(ptr):
    a = ptr.split()[3].point(lambda v: v * 0.35)
    sh = Image.new("RGBA", ptr.size, (0, 0, 0, 0))
    sh.putalpha(a)
    return sh.filter(ImageFilter.GaussianBlur(2.5))


def _ring(r, alpha, color=(235, 104, 52)):
    size = int((r + 8) * 2)
    im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = size * SS / 2
    d.ellipse([c - r * SS, c - r * SS, c + r * SS, c + r * SS], fill=color + (int(alpha * 70),),
              outline=color + (int(alpha * 255),), width=int(5 * SS))
    return im.resize((size, size), Image.LANCZOS)


def _ease(p):
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


def render(base_png, out_dir, dur, path, fps=30):
    """path: list of moves (t_arrive, x, y, click). The pointer glides from the previous point to (x, y), arriving
    at t_arrive (the first entry is where it starts, at t=0), and clicks there when click is true."""
    out_dir = pathlib.Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    base = Image.open(base_png).convert("RGBA")
    ptr = _pointer()
    sh = _shadow(ptr)
    small = ptr.resize((int(ptr.width * 0.86), int(ptr.height * 0.86)), Image.LANCZOS)
    clicks = [t for t, _, _, c in path if c]
    for n in range(int(round(dur * fps))):
        t = n / fps
        x, y = path[0][1], path[0][2]
        for (t0, x0, y0, _), (t1, x1, y1, _) in zip(path, path[1:]):
            mv = 0.9   # seconds the glide takes
            if t >= t1:
                x, y = x1, y1
            elif t > t1 - mv:
                e = _ease((t - (t1 - mv)) / mv)
                x, y = x0 + (x1 - x0) * e, y0 + (y1 - y0) * e
                break
            else:
                x, y = x0, y0
                break
        fr = base.copy()
        pressed = False
        for tc in clicks:
            ct = tc + 0.15               # the press, a moment after arriving
            if ct <= t < ct + 0.6:
                p = (t - ct) / 0.6
                rg = _ring(14 + 46 * math.sqrt(p), 1 - p)
                fr.alpha_composite(rg, (int(x - rg.width / 2), int(y - rg.height / 2)))
            if ct <= t < ct + 0.14:
                pressed = True
        p_im = small if pressed else ptr
        fr.alpha_composite(sh, (int(x - 4 + 3), int(y - 4 + 5)))
        fr.alpha_composite(p_im, (int(x - 4), int(y - 4)))
        fr.convert("RGB").save(out_dir / f"{n:04d}.png", compress_level=1)
    return out_dir
