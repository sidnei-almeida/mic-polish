"""Builds a fake GPU Screen Recorder session for the end-to-end test: an MKV with the game on audio track
1 and a simulated mic (voice + game through a room) on track 2, AAC like the real recorder.

    .venv/bin/python -m tests.make_session <voice.wav> <game audio> <out.mkv> [--swap]
"""
import os
import subprocess
import sys
import tempfile

import numpy as np

from engine.audio import SR, load, save
from tests.test_aec import simulate


def main(voice_path, game_path, out, swap=False):
    voice = load(voice_path); game2 = load(game_path, channels=2)
    n = 3 * len(voice)
    v = np.zeros(n, np.float32)
    for k in range(3):   # three takes of the voice with gaps of game in between
        a = k * len(voice) + k * 5 * SR
        if a < n:
            seg = voice[:n - a]; v[a:a + len(seg)] = seg[:max(0, n - a)]
    g2 = np.tile(game2, (1, int(np.ceil(n / game2.shape[1]))))[:, :n]
    mic, _, _, _ = simulate(v, g2.mean(0))
    with tempfile.TemporaryDirectory() as tmp:
        save(f'{tmp}/game.wav', g2, bits=16); save(f'{tmp}/mic.wav', mic, bits=16)
        first, second = (f'{tmp}/mic.wav', f'{tmp}/game.wav') if swap else (f'{tmp}/game.wav', f'{tmp}/mic.wav')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', first, '-i', second, '-map', '0:a', '-map', '1:a',
                        '-c:a', 'aac', '-b:a', '320k', out], check=True)
    print(out, f'{n / SR:.0f} s', 'tracks swapped' if swap else '')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3], '--swap' in sys.argv)
