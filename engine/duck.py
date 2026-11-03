"""What the edit needs from the speech segments: the game ducked under your voice, and markers.

- duck_envelope: gain keyframes for the game track, -8 dB while you speak, easing down just before
  and back up just after (a mix engineer's ride, not a pumping compressor).
- markers.csv: one row per segment (start, end, duration, in seconds and timecode), for any editor.
- filmcraft.jsonl: `markers.add` commands for filmcraft-cli (`filmcraft-cli --project p.fcproj --save
  run filmcraft.jsonl`), one marker per segment, spanning it.
"""
import csv
import json

import numpy as np

from .audio import SR

TICKS = 254_016_000_000   # FilmCraft's ticks per second


def duck_envelope(segments, duration, depth_db=-8.0, down_s=0.15, up_s=0.4, hold_gap_s=1.0):
    """[(time_s, gain_db)] keyframes. Segments closer than hold_gap_s stay ducked in between, so the game
    doesn't bob up between two sentences."""
    if not segments:
        return [(0.0, 0.0), (round(duration, 3), 0.0)]
    merged = []
    for a, b in segments:
        if merged and a - merged[-1][1] < hold_gap_s:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    pts = [(0.0, 0.0)]
    for a, b in merged:
        t0 = max(pts[-1][0], a - down_s)
        pts += [(t0, 0.0), (a, depth_db), (b, depth_db), (min(duration, b + up_s), 0.0)]
    pts.append((duration, 0.0))
    out = []
    for t, g in pts:   # drop duplicates
        t = round(float(t), 3)
        if not out or t > out[-1][0]:
            out.append((t, float(g)))
    return out


def gain_curve(envelope, n, sr=SR):
    """The envelope as a linear gain per sample (linear in dB between keyframes)."""
    t = np.arange(n) / sr
    ts = np.array([p[0] for p in envelope]); gs = np.array([p[1] for p in envelope])
    return (10 ** (np.interp(t, ts, gs) / 20)).astype(np.float32)


def timecode(sec, fps=60):
    f = int(round(sec * fps))
    return f'{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}'


def write_markers(segments, csv_path, jsonl_path, fps=60, name='fala'):
    with open(csv_path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['n', 'start_s', 'end_s', 'duration_s', 'start_tc', 'end_tc'])
        for i, (a, b) in enumerate(segments, 1):
            w.writerow([i, f'{a:.3f}', f'{b:.3f}', f'{b - a:.3f}', timecode(a, fps), timecode(b, fps)])
    with open(jsonl_path, 'w') as fh:
        fh.write(f'# {len(segments)} speech markers: filmcraft-cli --project <p.fcproj> --save run {jsonl_path.split("/")[-1]}\n')
        for i, (a, b) in enumerate(segments, 1):
            fh.write(json.dumps({'id': 'markers.add', 'params': {
                'target': 'program', 'time': round(a * TICKS), 'name': f'{name} {i}',
                'color': '#f0a030', 'durationFrames': max(1, int(round((b - a) * fps)))}}) + '\n')
