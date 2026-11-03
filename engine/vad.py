"""Where you speak, with Silero VAD (github.com/snakers4/silero-vad, MIT, runs on the CPU, ~1 ms per
32 ms chunk). Segments are padded and merged so a sentence with short pauses stays one piece."""
import numpy as np

from .audio import SR

VAD_SR = 16000
_model = None


def _load():
    global _model
    if _model is None:
        import warnings
        warnings.filterwarnings('ignore', category=FutureWarning)
        from silero_vad import load_silero_vad
        _model = load_silero_vad()
    return _model


def speech_segments(x, threshold=0.5, pad_s=0.2, merge_gap_s=0.6, min_s=0.35, progress=None):
    """[(start_s, end_s)] where someone speaks in mono float audio at 48 kHz."""
    import torch
    model = _load()
    y = x[::3].astype(np.float32)          # 48 kHz -> 16 kHz (the mic is already band-limited well below 8 kHz)
    chunk = 512                            # Silero's window at 16 kHz
    n = len(y) // chunk
    probs = np.zeros(n, np.float32)
    model.reset_states()
    step = max(1, n // 100)
    with torch.no_grad():
        for i in range(n):
            probs[i] = float(model(torch.from_numpy(y[i * chunk:(i + 1) * chunk]), VAD_SR))
            if progress and i % step == 0:
                progress(i / n)
    on = probs > threshold
    segs, start = [], None
    for i, v in enumerate(on):
        t = i * chunk / VAD_SR
        if v and start is None:
            start = t
        elif not v and start is not None:
            segs.append([start, t]); start = None
    if start is not None:
        segs.append([start, n * chunk / VAD_SR])
    # pad, merge close ones, drop blips
    dur = len(x) / SR
    segs = [[max(0.0, a - pad_s), min(dur, b + pad_s)] for a, b in segs]
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] <= merge_gap_s:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if progress:
        progress(1.0)
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= min_s]


def frame_mask(segments, frames, hop, sr=SR):
    """A bool per STFT frame (hop in samples) that is inside a segment."""
    m = np.zeros(frames, bool)
    for a, b in segments:
        m[int(a * sr / hop):int(b * sr / hop) + 1] = True
    return m


def noise_clip(x, segments, seconds=3.0, sr=SR):
    """The quietest stretches outside speech, joined into a `seconds` clip: what the room sounds like.
    None when there is not enough non-speech audio."""
    mask = np.ones(len(x), bool)
    for a, b in segments:
        mask[int(a * sr):int(b * sr)] = False
    w = int(0.25 * sr)
    cand = [i for i in range(0, len(x) - w, w) if mask[i:i + w].all()]
    if len(cand) * w < seconds * sr:
        return None
    rms = np.array([np.sqrt(np.mean(x[i:i + w] ** 2)) for i in cand])
    # the quietest 40%, then take them in time order until the clip is long enough
    keep = sorted(np.array(cand)[np.argsort(rms)[:max(int(seconds * 4), len(cand) * 2 // 5)]])
    parts = [x[i:i + w] for i in keep][:int(np.ceil(seconds * sr / w))]
    clip = np.concatenate(parts)[:int(seconds * sr)]
    return clip.astype(np.float32)
