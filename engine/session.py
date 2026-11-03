"""A whole recording session in one command: the gameplay MKV in, everything the edit needs out.

    python -m engine.session <recording.mkv|mic.wav> [--out DIR] [--game FILE] [--mic-track N] [--game-track N]
                             [--no-aec] [--no-duck] [--ai] [--warm] [--pitch N] [--json]

Input: GPU Screen Recorder's MKV with the game on audio track 1 and the mic on track 2 (the default
mapping), or any audio file with just the mic (then --game can point at the game audio separately).

Steps:
  1. read      the tracks, checks (silent mic, clipping, tracks swapped)
  2. aec       the game removed from the mic, using the game track as the reference (engine/aec.py)
  3. speech    where you speak (Silero VAD)
  4. room      a clip of the room's own noise from the quietest non-speech moments of the whole
               session, put in front of the take so the denoiser learns the room, not the first 3 s
  5. polish    enhance.sh: high-pass, denoise (DeepFilterNet if installed, else RNNoise; --ai: Resemble
               Enhance), EQ, de-esser, compressor, -16 LUFS, limiter
  6. edit      the game ducked under your voice, markers (CSV and FilmCraft commands), a report

Outputs in DIR (default: <recording>-polish/ next to it):
  voice.wav            the mic, game removed and polished, 48 kHz 24-bit mono, same timeline as the video
  game-ducked.wav      the game track ducked under the voice, 48 kHz stereo (only with a game track)
  markers.csv          speech segments; filmcraft.jsonl the same as FilmCraft markers
  duck.json            the ducking keyframes [(time_s, gain_db)]
  report.json          what was measured and done

--json prints one JSON object per line ({"type": "progress"|"log"|"warning"|"done"|"error", ...}) for a
front end; otherwise plain lines.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import warnings

import numpy as np

warnings.filterwarnings('ignore', category=FutureWarning)

from . import aec, duck, vad   # noqa: E402
from .audio import SR, load, probe_audio_tracks, rms_db, save   # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = ['read', 'aec', 'speech', 'room', 'polish', 'edit']


class Out:
    def __init__(self, as_json):
        self.as_json = as_json
        self.last = {}

    def emit(self, kind, **kw):
        if self.as_json:
            print(json.dumps({'type': kind, **kw}), flush=True)
        elif kind == 'progress':
            pct = int(kw['pct'] * 100)
            if self.last.get(kw['step']) is None or pct - self.last[kw['step']] >= 20 or pct == 100:
                self.last[kw['step']] = pct
                print(f'[{kw["step"]}] {pct}%', flush=True)
        elif kind in ('log', 'warning', 'error'):
            print(('warning: ' if kind == 'warning' else 'error: ' if kind == 'error' else '') + kw['msg'], flush=True)
        elif kind == 'done':
            print('done:', json.dumps(kw.get('outputs', {}), indent=2), flush=True)

    def progress(self, step):
        return lambda p: self.emit('progress', step=step, pct=round(float(p), 3))

    def log(self, msg):
        self.emit('log', msg=msg)

    def warn(self, msg):
        self.emit('warning', msg=msg)


def check_mic(x, o):
    """Problems worth knowing before an hour of processing."""
    level = rms_db(x)
    if level < -70:
        raise SystemExit('the mic track is silent (wrong track, or the mic was muted)')
    clipped = float(np.mean(np.abs(x) > 0.985))
    if clipped > 1e-4:
        o.warn(f'the mic clipped on {clipped * 100:.2f}% of samples: lower the mic gain for the next session (clipping cannot be undone)')
    peak = float(np.abs(x).max())
    if peak < 0.05:
        o.warn(f'the mic is very quiet (peak {20 * np.log10(peak + 1e-9):.0f} dBFS): raise the gain (setup-mic.sh) for less hiss')
    return {'mic_rms_dbfs': round(level, 1), 'mic_peak_dbfs': round(20 * np.log10(peak + 1e-12), 1), 'clipped_pct': round(clipped * 100, 3)}


def run_enhance(voice, segments, out_path, args, o):
    """enhance.sh on the voice, with the room's noise in front for the denoiser to learn, then trimmed."""
    clip = vad.noise_clip(voice, segments)
    lead = 0
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'take.wav')
        if clip is not None:
            lead = len(clip)
            save(src, np.concatenate([clip, voice]))
            o.log(f'room noise learned from {len(clip) / SR:.1f} s of the quietest non-speech moments ({rms_db(clip):.0f} dBFS)')
        else:
            save(src, voice)
            o.warn('not enough non-speech audio to learn the room: the denoiser learns from the first seconds')
        dst = os.path.join(tmp, 'polished.wav')
        cmd = [os.path.join(HERE, 'enhance.sh')]
        if args.ai:
            cmd.append('--ai')
        if args.warm:
            cmd.append('--warm')
        if args.pitch:
            cmd += ['--pitch', str(args.pitch)]
        cmd += [src, dst]
        p = subprocess.run(cmd, capture_output=True, text=True)
        for line in (p.stdout + p.stderr).splitlines():
            if line.strip():
                o.log('polish: ' + line.strip())
        if p.returncode != 0:
            raise SystemExit(f'enhance.sh failed ({p.returncode})')
        y = load(dst)
    y = y[lead:lead + len(voice)]
    if len(y) < len(voice):
        y = np.pad(y, (0, len(voice) - len(y)))
    save(out_path, y)
    return y


def main(argv=None):
    ap = argparse.ArgumentParser(description='Clean a gameplay session\'s mic: remove the game, polish the voice, mark the speech.')
    ap.add_argument('input')
    ap.add_argument('--out')
    ap.add_argument('--game', help='the game audio as a separate file (when the input is mic only)')
    ap.add_argument('--mic-track', type=int, default=None, help='audio track of the mic, from 1 (default 2 when there are two)')
    ap.add_argument('--game-track', type=int, default=None, help='audio track of the game, from 1 (default 1)')
    ap.add_argument('--no-aec', action='store_true', help='keep the game bleed in the mic')
    ap.add_argument('--no-duck', action='store_true', help='do not write game-ducked.wav')
    ap.add_argument('--duck-db', type=float, default=-8.0)
    ap.add_argument('--ai', action='store_true', help='Resemble Enhance instead of the classic denoisers (GPU)')
    ap.add_argument('--warm', action='store_true')
    ap.add_argument('--pitch', type=float, default=0.0)
    ap.add_argument('--vad-threshold', type=float, default=0.5)
    ap.add_argument('--keep-intermediate', action='store_true', help='also write voice-aec.wav (before polishing)')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    o = Out(args.json)
    t_start = time.time()
    report = {'input': os.path.abspath(args.input), 'steps': {}}
    try:
        if not os.path.exists(args.input):
            raise SystemExit(f'input not found: {args.input}')
        base = os.path.splitext(os.path.abspath(args.input))[0]
        out = os.path.abspath(args.out or base + '-polish')
        os.makedirs(out, exist_ok=True)

        # 1. read
        o.emit('step', step='read')
        tracks = probe_audio_tracks(args.input)
        if not tracks:
            raise SystemExit('no audio in the input')
        game = None
        if len(tracks) >= 2 and not args.game:
            mt = (args.mic_track or 2) - 1; gt = (args.game_track or 1) - 1
            o.log(f'{len(tracks)} audio tracks: game = track {gt + 1}, mic = track {mt + 1}, {tracks[0]["duration"] / 60:.1f} min')
            mic = load(args.input, track=mt)
            game2 = load(args.input, track=gt, channels=2)
        else:
            mic = load(args.input, track=(args.mic_track or 1) - 1)
            game2 = load(args.game, channels=2) if args.game else None
            o.log(f'mic only{" + game from " + args.game if args.game else ""}, {len(mic) / SR / 60:.1f} min')
        if game2 is not None:
            game2 = game2[:, :len(mic)] if game2.shape[1] >= len(mic) else np.pad(game2, ((0, 0), (0, len(mic) - game2.shape[1])))
            game = game2.mean(0)
        report['steps']['read'] = check_mic(mic, o)
        o.progress('read')(1.0)

        # 2. aec
        voice = mic
        if game is not None and not args.no_aec and rms_db(game) > -70:
            o.emit('step', step='aec')
            mask_fn = lambda x: vad.frame_mask(vad.speech_segments(x, args.vad_threshold), aec.frames_of(len(x)), aec.HOP)
            times, lags, peaks = aec.estimate_delay(game, mic)
            fit = aec.fit_delay(times, lags, peaks)
            if fit is not None and fit[0] < -0.001 * SR and len(tracks) >= 2 and not args.game:
                o.warn('the mic is ahead of the game, which cannot be: the tracks look swapped; swapping them')
                mic, game = game, mic
                game2 = load(args.input, track=mt, channels=2)[:, :len(mic)]   # the real game, in stereo
                report['steps']['read'] = check_mic(mic, o)
                report['tracks_swapped'] = True
            voice, rep = aec.cancel(mic, game, speech_frames_fn=mask_fn, progress=o.progress('aec'), log=o.log)
            report['steps']['aec'] = rep
            if 'bleed_removed_db' in rep:
                o.log(f'game bleed removed: about {rep["bleed_removed_db"]:.0f} dB (conservative estimate)')
            if args.keep_intermediate:
                save(os.path.join(out, 'voice-aec.wav'), voice)
        elif game is None:
            o.log('no game track: skipping the game removal')

        # 3. speech
        o.emit('step', step='speech')
        segs = vad.speech_segments(voice, args.vad_threshold, progress=o.progress('speech'))
        talk = sum(b - a for a, b in segs)
        o.log(f'{len(segs)} speech segments, {talk / 60:.1f} min of talking')
        report['steps']['speech'] = {'segments': len(segs), 'talk_s': round(talk, 1)}

        # 4-5. room + polish
        o.emit('step', step='polish')
        final = run_enhance(voice, segs, os.path.join(out, 'voice.wav'), args, o)
        o.progress('polish')(1.0)
        # speech again on the polished voice: cleaner, so the markers are tighter
        segs = vad.speech_segments(final, args.vad_threshold)

        # 6. edit
        o.emit('step', step='edit')
        outputs = {'voice': os.path.join(out, 'voice.wav')}
        env = duck.duck_envelope(segs, len(final) / SR, args.duck_db)
        with open(os.path.join(out, 'duck.json'), 'w') as fh:
            json.dump({'depth_db': args.duck_db, 'keyframes': env}, fh)
        outputs['duck'] = os.path.join(out, 'duck.json')
        if game2 is not None and not args.no_duck:
            g = duck.gain_curve(env, game2.shape[1])
            save(os.path.join(out, 'game-ducked.wav'), game2 * g[None, :])
            outputs['game_ducked'] = os.path.join(out, 'game-ducked.wav')
        duck.write_markers(segs, os.path.join(out, 'markers.csv'), os.path.join(out, 'filmcraft.jsonl'))
        outputs['markers'] = os.path.join(out, 'markers.csv'); outputs['filmcraft'] = os.path.join(out, 'filmcraft.jsonl')
        report['steps']['edit'] = {'segments': len(segs), 'duck_db': args.duck_db}
        report['seconds'] = round(time.time() - t_start, 1)
        report['outputs'] = outputs
        with open(os.path.join(out, 'report.json'), 'w') as fh:
            json.dump(report, fh, indent=2)
        o.progress('edit')(1.0)
        o.emit('done', outputs=outputs, seconds=report['seconds'])
        return 0
    except SystemExit as e:
        o.emit('error', msg=str(e))
        return 1
    except Exception as e:   # noqa: BLE001
        o.emit('error', msg=f'{type(e).__name__}: {e}', trace=traceback.format_exc())
        return 1


if __name__ == '__main__':
    sys.exit(main())
