"""Game-bleed removal on a simulated session: a voice take plus a game track played through a fake room
(reverb, a fractional delay, clock drift) into the mic. Checks the delay/drift estimate and how much
bleed goes away without hurting the voice.

    .venv/bin/python -m tests.test_aec <voice.wav> <game audio (any file)> [out_dir]
"""
import os
import sys
import time

import numpy as np

from engine import aec, vad
from engine.audio import SR, load, save, rms_db


def simulate(voice, game, delay_ms=23.4, drift_ppm=40.0, bleed_db=-3.0, seed=1):
    rng = np.random.default_rng(seed)
    n = len(voice)
    voice = voice * 10 ** ((-24 - rms_db(voice)) / 20)   # a mic with its gain set right (raw test takes are ~-45)
    g = game[:n] if len(game) >= n else np.pad(game, (0, n - len(game)))
    # room: direct sound plus 250 ms of decaying, darkening reflections
    ir_len = int(0.25 * SR)
    t = np.arange(ir_len) / SR
    # a real room: the direct sound is the strongest arrival, reflections start a couple of ms later
    ir = rng.standard_normal(ir_len) * np.exp(-t / 0.05) * 0.02
    ir[:int(0.002 * SR)] = 0
    ir = np.convolve(ir, np.ones(8) / 8)[:ir_len]
    ir[0] = 1.0
    wet = np.fft.irfft(np.fft.rfft(g, 2 * n) * np.fft.rfft(ir, 2 * n), 2 * n)[:n]
    # delay that grows with the drift
    pos = np.arange(n) - (delay_ms / 1000 * SR + drift_ppm * 1e-6 * np.arange(n))
    bleed = aec.resample_at(wet.astype(np.float32), pos)   # band-limited, like real sound
    bleed *= 10 ** ((rms_db(voice) + bleed_db - rms_db(bleed)) / 20)
    hiss = rng.standard_normal(n).astype(np.float32) * 10 ** (-62 / 20)
    return (voice + bleed + hiss).astype(np.float32), g.astype(np.float32), bleed.astype(np.float32), voice.astype(np.float32)


def main(voice_path, game_path, out_dir=None):
    voice = load(voice_path)
    game = load(game_path)
    if len(game) > 60 * SR:
        game = game[30 * SR:]          # skip a quiet start
    mic, ref, bleed, voice = simulate(voice, game)
    segs = vad.speech_segments(voice)
    t0 = time.time()
    mask_fn = lambda x: vad.frame_mask(vad.speech_segments(x), aec.frames_of(len(x)), aec.HOP)
    out, rep = aec.cancel(mic, ref, speech_frames_fn=mask_fn, log=lambda s: print('  ' + s))
    took = time.time() - t0
    # where only the game plays: how much is left; where you speak: how close to the clean voice
    quiet = np.ones(len(voice), bool)
    for a, b in segs:
        quiet[int(a * SR):int(b * SR)] = False
    before = rms_db((mic - voice)[quiet]); after = rms_db((out - voice)[quiet])   # only what is not your voice
    sdr_in = 10 * np.log10(np.sum(voice ** 2) / np.sum((mic - voice) ** 2))
    sdr_out = 10 * np.log10(np.sum(voice ** 2) / np.sum((out - voice) ** 2))
    print(f'expected: delay 23.4 ms, drift 40 ppm   got: {rep["delay_ms"]} ms, {rep["drift_ppm"]} ppm')
    print(f'bleed where you do not speak: {before:.1f} -> {after:.1f} dBFS ({before - after:.1f} dB removed)')
    print(f'voice vs everything else: {sdr_in:.1f} dB in the mic -> {sdr_out:.1f} dB after')
    print(f'{len(voice) / SR:.0f} s of audio in {took:.1f} s')
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        save(f'{out_dir}/mic-simulated.wav', mic); save(f'{out_dir}/mic-cleaned.wav', out)
    ok = abs(rep["delay_ms"] - 23.4) < 0.2 and abs(rep["drift_ppm"] - 40) < 2 and before - after > 25 and sdr_out > sdr_in + 20
    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:4]))
