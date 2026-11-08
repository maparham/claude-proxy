"""Sound track for the promo: a soft synthesized background piece plus UI sounds (mouse clicks on the purchase
screens, key taps while the terminal types, a chime when the pass is active, when the computer is authorized and when
the finished page appears). Reads out/timeline.json (written by build.py), writes out/soundtrack.wav and muxes it
into out/promo-v2.mp4.
"""
import json
import pathlib
import subprocess
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "out"
SR = 44100
rng = np.random.default_rng(7)

tl = json.loads((OUT / "timeline.json").read_text())
TOTAL = tl["total"]
S = {k: v[0] for k, v in tl["starts"].items()}
N = int(TOTAL * SR) + SR
music = np.zeros((2, N))
sfx = np.zeros((2, N))


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def add(buf, t, sig, pan=0.0, gain=1.0):
    i = int(t * SR)
    if i >= N:
        return
    sig = sig[: N - i] * gain
    buf[0, i:i + len(sig)] += sig * np.sqrt((1 - pan) / 2)
    buf[1, i:i + len(sig)] += sig * np.sqrt((1 + pan) / 2)


def tvec(dur):
    return np.arange(int(dur * SR)) / SR


# ---------------- music ----------------
BPM = 84
BEAT = 60 / BPM
BAR = 4 * BEAT
# Cmaj9 - Am9 - Fmaj7(#11-ish, soft) - G6sus: warm, unresolved loop that resolves to C at the end
CHORDS = [
    (48, [60, 64, 67, 71, 74]),   # Cmaj9
    (45, [57, 60, 64, 67, 71]),   # Am9
    (41, [57, 60, 64, 65, 69]),   # Fmaj7
    (43, [55, 59, 62, 64, 67]),   # G6
]


def pad_voice(f, dur):
    t = tvec(dur)
    s = np.zeros_like(t)
    for cents in (-5, 0, 6):
        ff = f * 2 ** (cents / 1200)
        ph = rng.uniform(0, 2 * np.pi)
        s += np.sin(2 * np.pi * ff * t + ph) + 0.22 * np.sin(4 * np.pi * ff * t + ph) + 0.06 * np.sin(6 * np.pi * ff * t)
    att, rel = 0.9, 1.2
    env = np.minimum(1, t / att) * np.minimum(1, (dur - t) / rel).clip(0)
    return s * env / 3


def ep_note(f, dur=1.6):
    """Soft electric piano: sine with a bell-ish second partial, quick attack, gentle decay."""
    t = tvec(dur)
    env = (1 - np.exp(-t * 300)) * np.exp(-t * 2.6)
    s = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 6) \
        + 0.08 * np.sin(2 * np.pi * 3.01 * f * t) * np.exp(-t * 9)
    return s * env


def bass_note(f, dur):
    t = tvec(dur)
    env = (1 - np.exp(-t * 80)) * np.exp(-t * 0.9) * np.minimum(1, (dur - t) / 0.3).clip(0)
    return (np.sin(2 * np.pi * f * t) + 0.15 * np.sin(4 * np.pi * f * t)) * env


def shaker(dur=0.09):
    t = tvec(dur)
    n = rng.standard_normal(len(t))
    n = np.diff(np.diff(n, prepend=0), prepend=0)   # crude high-pass
    return n * np.exp(-t * 60) * 0.25


ARP = [0, 2, 4, 1, 3, 4, 2, 1]     # chord-tone indices per eighth note
end_music = TOTAL - 0.2
n_bars = int(np.ceil(end_music / BAR))
for b in range(n_bars):
    t0 = b * BAR
    last = t0 + BAR >= end_music - BAR   # the final bar: resolve to C
    root, tones = CHORDS[0] if last else CHORDS[b % 4]
    dur = (end_music - t0) if last else BAR + 1.2
    for k, m in enumerate(tones[:4]):
        add(music, t0, pad_voice(hz(m), dur), pan=(-0.5 + k / 3), gain=0.055)
    add(music, t0, bass_note(hz(root), min(dur, BAR + 0.3)), gain=0.16)
    # arpeggio from the site scene on, thinner while the hook text is on screen
    if t0 >= S["s2"] - 0.1 and not last:
        for e in range(8):
            if b % 2 and e in (3, 7):
                continue
            m = tones[ARP[e]] + 12
            add(music, t0 + e * BEAT / 2 + rng.normal(0, 0.006), ep_note(hz(m)), pan=0.35 * np.sin(e), gain=0.05)
    if last:
        for k, m in enumerate([72, 76, 79, 83]):
            add(music, t0 + k * 0.12, ep_note(hz(m), 3.5), pan=-0.3 + 0.2 * k, gain=0.05)
    # soft shaker on the off-beats once the demo starts, out for the end card
    if S["b_pricing"] - 0.1 <= t0 < S["s5"]:
        for e in range(4):
            add(music, t0 + e * BEAT + BEAT / 2, shaker(), pan=0.4, gain=0.35)

# reverb: exponentially decaying noise IR, a little different per channel for width
ir_t = tvec(2.4)
L = 1 << int(np.ceil(np.log2(N + len(ir_t))))
wet = np.zeros_like(music)
for ch in (0, 1):
    ir = rng.standard_normal(len(ir_t)) * np.exp(-ir_t * 2.8)
    ir /= np.sqrt((ir ** 2).sum())
    wet[ch] = np.fft.irfft(np.fft.rfft(music[ch], L) * np.fft.rfft(ir, L), L)[:N]
music = 0.75 * music + 0.45 * wet

t_all = np.arange(N) / SR
fade = np.minimum(1, t_all / 1.5) * np.clip((TOTAL - t_all) / 3.0, 0, 1)
music *= fade


# ---------------- UI sounds ----------------
def click(strength=1.0):
    t = tvec(0.06)
    n = np.diff(rng.standard_normal(len(t)), prepend=0) * np.exp(-t * 900)
    tick = np.sin(2 * np.pi * 2300 * t) * np.exp(-t * 400)
    thump = np.sin(2 * np.pi * 140 * t) * np.exp(-t * 120)
    return (0.35 * n + 0.5 * tick + 0.6 * thump) * strength


def key(strength=1.0):
    t = tvec(0.04)
    f = rng.uniform(1700, 2600)
    n = np.diff(rng.standard_normal(len(t)), prepend=0) * np.exp(-t * 1100)
    tick = np.sin(2 * np.pi * f * t) * np.exp(-t * 700)
    thump = np.sin(2 * np.pi * rng.uniform(170, 220) * t) * np.exp(-t * 200)
    return (0.4 * n + 0.3 * tick + 0.35 * thump) * strength


def chime():
    out = np.zeros(int(2.2 * SR))
    for k, m in enumerate([84, 88, 91]):   # C7 E7 G7, quick upward
        t = tvec(2.0)
        s = (np.sin(2 * np.pi * hz(m) * t) + 0.25 * np.sin(2 * np.pi * 2.76 * hz(m) * t) * np.exp(-t * 6)) \
            * (1 - np.exp(-t * 400)) * np.exp(-t * 2.2)
        i = int(k * 0.09 * SR)
        out[i:i + len(s)] += s[: len(out) - i]
    return out


for scene, times in tl["clicks"].items():
    for tc in times:
        add(sfx, S[scene] + tc, click(), pan=0.1, gain=0.55)


def typing(t0, t1, enter_at):
    t = t0
    while t < t1:
        add(sfx, t, key(rng.uniform(0.6, 1.0)), pan=rng.uniform(-0.2, 0.2), gain=0.22)
        t += rng.uniform(0.045, 0.085)
    add(sfx, enter_at, key(1.6), gain=0.3)


# install: typing measured at 0.07-3.3 s of the recording (clip starts at 0.5, plays at 1x), Enter ~0.6 s later
typing(S["t_install"] + 0.1, S["t_install"] + 2.8, S["t_install"] + 3.4)
# prompt: typing at 14.7-21.7 s of the recording (clip starts at 14.6), Enter ~0.8 s later
typing(S["t_prompt"] + 0.1, S["t_prompt"] + 7.1, S["t_prompt"] + 7.9)
add(sfx, S["auth"] + 0.15, chime(), gain=0.07)          # the browser approval page
add(sfx, S["b_paid"] + 0.2, chime(), gain=0.09)        # pass active
add(sfx, S["bakery"] + 0.2, chime(), gain=0.09)        # the finished page

# duck the music a little under each UI sound cluster? keep it simple: music stays soft
mix = music + sfx
mix /= np.abs(mix).max() / 0.89
mix = mix[:, : int(TOTAL * SR)]
wav = OUT / "soundtrack.wav"
with wave.open(str(wav), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((mix.T * 32767).astype("<i2").tobytes())

final = OUT / "promo-v2.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(OUT / "promo-v2-silent.mp4"), "-i", str(wav),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-shortest", "-movflags", "+faststart", str(final)], check=True)
print(final)
