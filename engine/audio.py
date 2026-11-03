"""Audio in and out through ffmpeg, so any container works (an MKV from GPU Screen Recorder with the game
on track 1 and the mic on track 2, or a plain WAV)."""
import json
import subprocess
import wave

import numpy as np

SR = 48000


def probe_audio_tracks(path: str) -> list[dict]:
    """The audio streams of a file, in order: [{'index', 'channels', 'duration'}]."""
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries',
         'stream=index,channels:stream_tags=title:format=duration', '-of', 'json', path],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    dur = float(info.get('format', {}).get('duration', 0) or 0)
    return [{'index': s['index'], 'channels': s.get('channels', 1), 'duration': dur,
             'title': s.get('tags', {}).get('title', '')} for s in info.get('streams', [])]


def load(path: str, track: int = 0, channels: int = 1, sr: int = SR) -> np.ndarray:
    """Audio track `track` (0 = first audio stream) as float32, shape (n,) for mono or (channels, n)."""
    raw = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-map', f'0:a:{track}', '-ac', str(channels), '-ar', str(sr),
         '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    return x if channels == 1 else x.reshape(-1, channels).T.copy()


def save(path: str, x: np.ndarray, sr: int = SR, bits: int = 24):
    """Writes mono (n,) or multichannel (ch, n) float audio as PCM, clipped to [-1, 1]."""
    x = np.atleast_2d(np.asarray(x, dtype=np.float32))
    x = np.clip(x, -1, 1)
    if bits == 16:
        data = (x.T * 32767).astype('<i2').tobytes()
    else:  # 24-bit little endian
        v = (x.T.reshape(-1) * 8388607).astype('<i4')
        data = np.frombuffer(v.tobytes(), dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(path, 'wb') as w:
        w.setnchannels(x.shape[0]); w.setsampwidth(bits // 8); w.setframerate(sr)
        w.writeframes(data)


def peaks(x: np.ndarray, per_second: int = 50, sr: int = SR) -> list[float]:
    """A compact waveform for the UI: the peak of each 1/per_second s window, 0..1."""
    x = np.abs(np.atleast_2d(x)).max(axis=0)
    w = max(1, sr // per_second)
    n = len(x) // w
    if n == 0:
        return []
    p = x[:n * w].reshape(n, w).max(axis=1)
    return np.round(np.minimum(p, 1.0), 3).tolist()


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12))
