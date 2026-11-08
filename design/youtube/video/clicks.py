"""The promo with the Moonlight Sonata recording (MUSIC) under it plus a soft mouse click at each pointer click
on the purchase screens (times from out/timeline.json, written by build.py). Output: out/promo-v2-moonlight.mp4"""
import json
import pathlib
import subprocess
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "out"
MUSIC = pathlib.Path.home() / "Downloads" / "Moonlight Sonata (Beethoven).mp3"
SR = 44100
rng = np.random.default_rng(7)
tl = json.loads((OUT / "timeline.json").read_text())
N = int(tl["total"] * SR)
buf = np.zeros(N)


def click():
    t = np.arange(int(0.06 * SR)) / SR
    n = np.diff(rng.standard_normal(len(t)), prepend=0) * np.exp(-t * 900)
    tick = np.sin(2 * np.pi * 2300 * t) * np.exp(-t * 400)
    thump = np.sin(2 * np.pi * 140 * t) * np.exp(-t * 120)
    return 0.35 * n + 0.5 * tick + 0.6 * thump


for scene, times in tl["clicks"].items():
    for tc in times:
        i = int((tl["starts"][scene][0] + tc) * SR)
        c = click()
        buf[i:i + len(c)] += c[: N - i]
buf *= 0.6 / np.abs(buf).max()
wav = OUT / "clicks.wav"
with wave.open(str(wav), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((buf * 32767).astype("<i2").tobytes())

T = tl["total"]
fc = (f"[1:a]atrim=0:{T},afade=t=in:d=0.3,afade=t=out:st={T - 4}:d=4,loudnorm=I=-13:TP=-1:LRA=7,volume=0.3,"
      f"aresample={SR},aformat=channel_layouts=stereo[m];[2:a]aformat=channel_layouts=stereo[c];"
      f"[m][c]amix=inputs=2:normalize=0,alimiter=limit=0.95[a]")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(OUT / "promo-v2-silent.mp4"), "-i", str(MUSIC),
                "-i", str(wav), "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                str(OUT / "promo-v2-moonlight.mp4")], check=True)
print(OUT / "promo-v2-moonlight.mp4")
