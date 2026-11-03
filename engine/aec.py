"""Removes the game from the mic when you play on speakers instead of headphones.

The recorder captures the game twice: clean on its own track (what the speakers were sent) and again
through the room into the mic. With the clean copy as a reference this is echo cancellation, done
offline, so it can look at the whole recording instead of adapting on the fly:

1. Delay and drift. The mic hears the speakers a few ms late, and the two devices run on slightly
   different clocks, so that delay creeps over a long session (50 ppm is 0.18 s an hour). The delay is
   measured with GCC-PHAT on windows across the take, a robust line is fitted through it, and the
   reference is resampled onto the mic's timeline.
2. Room filter, solved, not adapted. Per STFT bin, a multi-tap filter (taps cover ~250 ms of reverb)
   maps the game to what the mic heard. It is the least-squares solution over 20 s blocks (with their
   neighbours, so it follows slow changes such as you moving or the volume knob), computed only from
   frames where the game plays and you don't speak, so your voice never teaches it to cancel itself.
   Two passes: the first uses every frame where the game plays; its residual shows where you speak;
   the second excludes those frames.
3. Residual suppression. A Wiener-style gain per bin takes out what a linear filter cannot (speaker
   distortion, the far reverb tail). It is calibrated: how much bleed the filter leaves is measured
   per bin where only the game plays, and only that much is suppressed (assuming the whole echo is
   left would eat the voice). Floored, and only where the game plays.

On a simulated session (voice at -24 dBFS, game bleeding in 3 dB under it through 250 ms of room,
23.4 ms late, 40 ppm drift): delay and drift found to 0.01 ms / 0.01 ppm; voice-to-everything-else from
3 dB to 28 dB. Without a voice the linear filter alone reaches the simulated room's hiss floor (35 dB).
"""
import numpy as np

from .audio import SR

FRAME, HOP = 2048, 512
TAPS = 24          # 24 hops of 512 at 48 kHz: ~256 ms of room response
BLOCK_S = 20.0     # the filter is re-solved every block, from the block and its neighbours
REG = 1e-3         # Tikhonov regularisation, relative to the reference's power in the bin
FLOOR_DB = -24     # the residual suppressor never cuts a bin by more than this
OVERSUB = 1.0      # how hard the suppressor leans on the (calibrated) residual estimate
CHUNK_S = 300.0    # long sessions are processed in chunks this long
OVERLAP_S = 20.0   # ... overlapping this much, crossfaded
SAFETY_MS = 3.0    # the reference is aligned this much early: causal taps absorb a late estimate, not an early one


def _stft(x):
    win = np.hanning(FRAME).astype(np.float32)
    pad = np.concatenate([np.zeros(FRAME, np.float32), x.astype(np.float32), np.zeros(FRAME, np.float32)])
    n = 1 + (len(pad) - FRAME) // HOP
    out = np.empty((n, FRAME // 2 + 1), np.complex64)
    for a in range(0, n, 4096):   # in chunks: an hour at once would need several GB
        b = min(n, a + 4096)
        idx = np.arange(FRAME)[None, :] + HOP * np.arange(a, b)[:, None]
        out[a:b] = np.fft.rfft(pad[idx] * win, axis=1)
    return out


def _istft(X, length):
    win = np.hanning(FRAME).astype(np.float32)
    out = np.zeros(HOP * (len(X) - 1) + FRAME, np.float32)
    norm = np.zeros_like(out)
    w2 = win ** 2
    for a in range(0, len(X), 4096):
        frames = np.fft.irfft(X[a:a + 4096], FRAME, axis=1).astype(np.float32) * win
        for i, f in enumerate(frames):
            s = (a + i) * HOP
            out[s:s + FRAME] += f; norm[s:s + FRAME] += w2
    out /= np.maximum(norm, 1e-6)
    return out[FRAME:FRAME + length]


def frames_of(length):
    """How many STFT frames a signal of `length` samples has."""
    return 1 + (length + 2 * FRAME - FRAME) // HOP


def frame_time(i):
    """Centre time (s) of STFT frame i (the signal is padded by FRAME at the start)."""
    return (i * HOP + FRAME / 2 - FRAME) / SR


# ---- delay and drift ---------------------------------------------------------------------------
def gcc_phat(a, b, max_lag):
    """Lag (samples) of b behind a, and how sharp the peak is."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    A = np.fft.rfft(a, n); B = np.fft.rfft(b, n)
    R = B * np.conj(A); R /= np.abs(R) + 1e-12
    cc = np.fft.irfft(R, n)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    i = int(np.argmax(np.abs(cc)))
    # parabolic refinement to a fraction of a sample
    frac = 0.0
    if 0 < i < len(cc) - 1:
        y0, y1, y2 = np.abs(cc[i - 1]), np.abs(cc[i]), np.abs(cc[i + 1])
        d = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / d if d != 0 else 0.0
    peak = float(np.abs(cc).max() / (np.abs(cc).mean() + 1e-12))
    return i - max_lag + frac, peak


def estimate_delay(ref, mic, window_s=None, max_ms=500.0):
    """Delay of the mic behind the reference over time: (times_s, lags, sharpness) per window.
    20 s windows on long sessions, shorter on short takes, so there are always several."""
    if window_s is None:
        window_s = float(np.clip(len(mic) / SR / 8, 4.0, 20.0))
    w = int(window_s * SR); max_lag = int(max_ms / 1000 * SR)
    times, lags, peaks = [], [], []
    for a in range(0, max(1, len(mic) - w + 1), w):
        r = ref[a:a + w]; m = mic[a:a + w]
        if len(r) < w // 2 or np.sqrt(np.mean(r ** 2)) < 1e-4:
            continue   # the game is silent here: nothing to measure
        lag, pk = gcc_phat(r, m, max_lag)
        times.append((a + w / 2) / SR); lags.append(lag); peaks.append(pk)
    return np.array(times), np.array(lags, float), np.array(peaks)


def fit_delay(times, lags, peaks):
    """A robust line through the per-window lags: delay(t) = d0 + drift * t (samples, t in s)."""
    if len(lags) == 0:
        return None
    good = peaks >= np.median(peaks) * 0.5
    t, l = times[good], lags[good]
    if len(l) < 3:
        return float(np.median(lags)), 0.0
    drift, d0 = np.polyfit(t, l, 1)
    for _ in range(4):   # refit without outliers (windows where something else dominated)
        res = np.abs(l - (d0 + drift * t))
        keep = res <= max(2.0, 3.0 * np.median(res))
        if keep.all() or keep.sum() < 3:
            break
        t, l = t[keep], l[keep]
        drift, d0 = np.polyfit(t, l, 1)
    return float(d0), float(drift)


def residual_delay(ref, mic, d0, drift, window_s=2.0, max_windows=80, max_lag=48, up=16):
    """After a first alignment (d0, drift), the delay that is left, measured precisely: GCC-PHAT on short windows
    spread over the take, the correlation upsampled `up` times around zero lag. A 2.5 ppm drift error
    (what one coarse pass leaves) is a few samples over a minute and wipes out the highs; this brings
    it down to a small fraction of a sample. Returns (d0, drift) in samples, samples/s, or None."""
    w = int(window_s * SR)
    n_win = max(1, len(mic) // w)
    picks = np.linspace(0, n_win - 1, min(n_win, max_windows)).astype(int)
    times, lags, peaks = [], [], []
    nfft = 1 << int(np.ceil(np.log2(2 * w)))
    for k in picks:
        a = k * w
        nn = np.arange(a, min(len(mic), a + w), dtype=np.float64)
        r = resample_at(ref, nn - (d0 + drift * nn / SR)); m = mic[a:a + w]   # aligned locally: no full pass
        if np.sqrt(np.mean(r ** 2)) < 1e-4:
            continue
        A = np.fft.rfft(r * np.hanning(len(r)), nfft); B = np.fft.rfft(m * np.hanning(len(m)), nfft)
        R = B * np.conj(A); R /= np.abs(R) + 1e-12
        cc = np.fft.irfft(R, nfft * up)                  # zero-padding in frequency = upsampled correlation
        L = max_lag * up
        seg = np.concatenate([cc[-L:], cc[:L + 1]])
        i = int(np.argmax(np.abs(seg)))
        times.append((a + w / 2) / SR); lags.append((i - L) / up)
        peaks.append(float(np.abs(seg).max() / (np.abs(seg).mean() + 1e-12)))
    if len(lags) < 3:
        return None
    return fit_delay(np.array(times), np.array(lags), np.array(peaks))


def resample_at(x, pos, half=16):
    """x read at fractional positions `pos` with a Kaiser-windowed sinc (2*half taps). Linear
    interpolation would dull the highs by an amount that changes as the drift moves the fraction,
    which no room filter can model; this is flat to ~20 kHz."""
    out = np.zeros(len(pos), np.float32)
    beta = 8.0
    for a in range(0, len(pos), SR):   # one second at a time
        p = pos[a:a + SR]
        i0 = np.floor(p).astype(np.int64)
        frac = (p - i0)[:, None]
        k = np.arange(-half + 1, half + 1)[None, :]
        idx = i0[:, None] + k
        t = k - frac
        win = np.i0(beta * np.sqrt(np.clip(1 - (t / half) ** 2, 0, 1))) / np.i0(beta)
        h = np.sinc(t) * win
        valid = (idx >= 0) & (idx < len(x))
        vals = np.where(valid, x[np.clip(idx, 0, len(x) - 1)], 0.0)
        out[a:a + len(p)] = (vals * h).sum(1)
    return out


def align(ref, d0, drift, length):
    """The reference resampled onto the mic's timeline: out[n] = ref[n - delay(n)]."""
    n = np.arange(length, dtype=np.float64)
    return resample_at(ref, n - (d0 + drift * n / SR))


# ---- the room filter ---------------------------------------------------------------------------
def _lag(R, l):
    if l == 0:
        return R
    out = np.zeros_like(R); out[l:] = R[:-l]; return out


def _solve_blocks(M, R, weight, taps, block, progress=None):
    """Weighted least-squares per-bin filters, one per block (solved from the block and its two
    neighbours), applied to the reference. weight: (frames,) or (frames, bins). Returns the echo
    estimate Y (same shape as M)."""
    F, K = M.shape
    nb = max(1, int(np.ceil(F / block)))
    # per-block statistics: c[d] = sum w conj(R_t) R_{t-d}, r[l] = sum w conj(R_{t-l}) M_t
    C = np.zeros((nb, taps, K), np.complex128); Rx = np.zeros((nb, taps, K), np.complex128)
    for b in range(nb):
        a, e = b * block, min(F, (b + 1) * block)
        w = weight[a:e, None] if weight.ndim == 1 else weight[a:e]
        Rb = R[max(0, a - taps):e]; off = a - max(0, a - taps)
        cur = Rb[off:]
        for d in range(taps):
            past = Rb[off - d:len(Rb) - d] if off - d >= 0 else np.vstack([np.zeros((d - off, K), Rb.dtype), Rb[:len(Rb) - d]])
            C[b, d] = (w * np.conj(cur) * past).sum(0)
            Rx[b, d] = (w * np.conj(past) * M[a:e]).sum(0)
        if progress:
            progress(0.5 * (b + 1) / nb)
    Y = np.zeros_like(M)
    for b in range(nb):
        lo, hi = max(0, b - 1), min(nb, b + 2)
        c = C[lo:hi].sum(0); r = Rx[lo:hi].sum(0)          # (taps, K)
        # Hermitian Toeplitz normal matrix per bin: A[l, m] = c[m - l] (m >= l), conj(c[l - m]) otherwise
        idx = np.arange(taps)
        D = idx[None, :] - idx[:, None]
        A = np.where(D[..., None] >= 0, c[np.abs(D)], np.conj(c[np.abs(D)]))   # (taps, taps, K)
        A = np.moveaxis(A, -1, 0)                                                # (K, taps, taps)
        reg = REG * np.real(c[0]) + 1e-12
        A = A + reg[:, None, None] * np.eye(taps)[None]
        W = np.linalg.solve(A, np.moveaxis(r, -1, 0)[..., None])[..., 0]        # (K, taps)
        a, e = b * block, min(F, (b + 1) * block)
        Ra = R[max(0, a - taps):e]; off = a - max(0, a - taps)
        y = np.zeros((e - a, K), np.complex128)
        for l in range(taps):
            seg = Ra[off - l:len(Ra) - l] if off - l >= 0 else np.vstack([np.zeros((l - off, K), Ra.dtype), Ra[:len(Ra) - l]])
            y += seg * W[:, l][None, :]
        Y[a:e] = y.astype(np.complex64)
        if progress:
            progress(0.5 + 0.5 * (b + 1) / nb)
    return Y


def _smooth(p, a=0.6):
    """One-pole smoothing along time (axis 0), vectorised in chunks."""
    out = np.empty_like(p); acc = p[0].copy()
    for i in range(len(p)):
        acc *= a; acc += (1 - a) * p[i]; out[i] = acc
    return out


def _cancel_chunk(mic, r, speech_frames_fn, progress):
    """Room filter and residual suppression on one aligned chunk. Returns (out, (energy before, after)
    over frames where only the game plays)."""
    say = lambda a, b: (lambda p: progress(a + (b - a) * p))
    M, R = _stft(mic), _stft(r)
    game_on = (np.abs(R) ** 2).mean(axis=1) > 1e-9
    block = int(BLOCK_S * SR / HOP)
    # pass 1: every frame where the game plays
    Y = _solve_blocks(M, R, game_on.astype(np.float32), TAPS, block, progress=say(0.0, 0.3))
    E = M - Y
    speech = np.zeros(len(M), bool)
    if speech_frames_fn is not None:
        speech = np.asarray(speech_frames_fn(_istft(E, len(mic))), bool)[:len(M)]
        if len(speech) < len(M):
            speech = np.pad(speech, (0, len(M) - len(speech)))
    # pass 2: only frames where the game plays and nobody speaks, so your voice never teaches the
    # filter to cancel itself. (Per-bin reweighting on top of this was tried: no gain.)
    quiet = game_on & ~speech
    if speech.any() and quiet.sum() > block // 4:
        Y = _solve_blocks(M, R, quiet.astype(np.float32), TAPS, block, progress=say(0.3, 0.85))
        E = M - Y
    # residual suppression, calibrated: how much bleed the filter leaves is measured where only the game
    # plays, and only that much is taken out (assuming the whole echo is left would eat your voice)
    pe = np.abs(Y) ** 2
    if quiet.sum() > 10:
        leak = np.clip((np.abs(E[quiet]) ** 2).sum(0) / (pe[quiet].sum(0) + 1e-12), 1e-4, 1.0)   # per bin
    else:
        leak = np.full(M.shape[1], 0.1)
    pr = _smooth(pe * leak[None, :]); pm = _smooth(np.abs(E) ** 2)
    g = np.maximum(10 ** (FLOOR_DB / 20), 1 - OVERSUB * pr / (pm + 1e-12)).astype(np.float32)
    g[~game_on] = 1.0
    out = _istft(E * g, len(mic))
    progress(1.0)
    # for the report, only frames the predicted bleed explains (at least half the mic's energy): frames
    # with a word the detector missed would count your voice as bleed that was not removed
    em = (np.abs(M) ** 2).sum(1); ey = pe.sum(1)
    sure = quiet & (ey >= 0.5 * em)
    before = float(em[sure].sum()) if sure.any() else 0.0
    after = float((np.abs(E[sure] * g[sure]) ** 2).sum()) if sure.any() else 0.0
    return out, (before, after)


def cancel(mic, ref, speech_frames_fn=None, progress=None, log=print):
    """mic: (n,) mono mic; ref: (n',) mono game track (what the speakers were sent).
    speech_frames_fn(signal) -> bool per STFT frame where someone speaks (enables the second pass).
    Returns (cleaned mic, report)."""
    say = (lambda a, b: (lambda p: progress(a + (b - a) * p))) if progress else (lambda a, b: (lambda p: None))
    times, lags, peaks = estimate_delay(ref, mic)
    fit = fit_delay(times, lags, peaks)
    if fit is None:
        log('the game track is silent: nothing to remove')
        return mic.copy(), {'skipped': 'game track silent'}
    d0, drift = fit
    sharp = float(np.median(peaks)) if len(peaks) else 0.0
    log(f'delay {d0 / SR * 1000:.1f} ms, drift {drift / SR * 1e6:.1f} ppm, {len(lags)} windows, peak sharpness {sharp:.0f}')
    if sharp < 8:
        log('warning: the game barely correlates with the mic (headphones? speakers muted?): removal will do little')
    # second, fine measurement on the coarsely aligned reference; add what is left
    fine = residual_delay(ref, mic, d0, drift)
    if fine is not None:
        d0 += fine[0]; drift += fine[1]
        log(f'refined: delay {d0 / SR * 1000:.2f} ms, drift {drift / SR * 1e6:.2f} ppm')
    r = align(ref, d0 - SAFETY_MS / 1000 * SR, drift, len(mic))
    # long sessions in 5-minute chunks with a 20 s overlap, crossfaded (an hour at once would need ~15 GB)
    chunk, ov = int(CHUNK_S * SR), int(OVERLAP_S * SR)
    out = np.zeros(len(mic), np.float32)
    num = den = 0.0
    starts = list(range(0, max(1, len(mic) - ov), chunk - ov)) if len(mic) > chunk else [0]
    for ci, a in enumerate(starts):
        e = min(len(mic), a + chunk)
        o, (n_, d_) = _cancel_chunk(mic[a:e], r[a:e], speech_frames_fn,
                                    say(0.1 + 0.9 * ci / len(starts), 0.1 + 0.9 * (ci + 1) / len(starts)))
        num += n_; den += d_
        if ci == 0:
            out[a:e] = o
        else:
            f = min(ov, e - a)
            ramp = np.linspace(0, 1, f, dtype=np.float32)
            out[a:a + f] = out[a:a + f] * (1 - ramp) + o[:f] * ramp
            out[a + f:e] = o[f:]
    removed = 10 * np.log10(num / den) if den > 0 and num > 0 else 0.0
    return out, {'delay_ms': round(d0 / SR * 1000, 2), 'drift_ppm': round(drift / SR * 1e6, 2),
                 'windows': int(len(lags)), 'peak_sharpness': round(sharp, 1), 'bleed_removed_db': round(float(removed), 1)}
