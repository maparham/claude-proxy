"""Render each blog post's social preview image, src/claude_proxy/static/og-<slug>.png (1200x630): the post's cover in its
color with its illustration, the Rahkar brand, its tag and title. LinkedIn, Telegram and WhatsApp take no SVG, so the
covers are drawn here in headless Chrome. Run after adding a post or editing a cover or title:

    uv run python scripts/blog_og.py [slug ...]
"""
from __future__ import annotations

import html
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_proxy import blog  # noqa: E402

STATIC = Path(blog.__file__).parent / "static"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=block">
<link rel="stylesheet" href="{static}/app.css">
<link rel="stylesheet" href="{static}/home.css">
<style>
  html, body {{ margin: 0; width: 1200px; height: 630px; overflow: hidden; background: #f9f9f7; color: #0b0b0b; }}
  body {{ font-family: Vazirmatn, sans-serif; position: relative; }}
  .cover {{ position: absolute; inset: 0 0 auto 0; height: 300px; }}
  .cover .big {{ width: 260px; height: 260px; bottom: -50px; inset-inline-start: 60px; }}
  .text {{ position: absolute; right: 72px; left: 72px; top: 336px; bottom: 44px; display: flex; flex-direction: column; }}
  .tag {{ align-self: flex-start; font-size: 22px; font-weight: 600; padding: 4px 16px; border-radius: 999px;
    background: color-mix(in srgb, var(--c) 14%, #fff); color: color-mix(in srgb, var(--c) 70%, #000); }}
  h1 {{ margin: 14px 0 0; font-size: {size}px; line-height: 1.3; font-weight: 800; letter-spacing: -0.01em; }}
  .foot {{ margin-top: auto; display: flex; align-items: center; justify-content: space-between; }}
  .brand {{ display: flex; align-items: center; gap: 14px; font-size: 28px; font-weight: 800; }}
  .brand img {{ width: 52px; height: 52px; border-radius: 13px; }}
  .url {{ direction: ltr; font: 600 24px ui-monospace, Menlo, monospace; color: #898781; }}
</style>
</head>
<body style="--c: var(--{color})">
{cover}
<div class="text">
  {tag}
  <h1>{title}</h1>
  <div class="foot"><div class="brand"><img src="{static}/rahkar-icon.svg" alt="">راهکار هوش برتر</div><div class="url">rahkar.pro</div></div>
</div>
</body>
</html>
"""


def render(p: blog.Post, tmp: Path) -> Path:
    tag = f'<span class="tag">{html.escape(p.tag)}</span>' if p.tag else ""
    size = 58 if len(p.title) <= 42 else 48
    page = tmp / f"{p.slug}.html"
    page.write_text(PAGE.format(static=STATIC.as_uri(), color=p.color, cover=blog.cover(p), tag=tag,
                                title=html.escape(p.title), size=size), encoding="utf-8")
    out = STATIC / f"og-{p.slug}.png"
    subprocess.run([CHROME, "--headless", f"--user-data-dir={tmp / 'profile'}", "--hide-scrollbars",
                    "--allow-file-access-from-files", "--window-size=1200,630", "--virtual-time-budget=5000",
                    f"--screenshot={out}", page.as_uri()], check=True, capture_output=True)
    return out


def main() -> None:
    wanted = set(sys.argv[1:])
    with tempfile.TemporaryDirectory() as tmp:
        for p in blog.posts():
            if not wanted or p.slug in wanted:
                print(render(p, Path(tmp)))


if __name__ == "__main__":
    main()
