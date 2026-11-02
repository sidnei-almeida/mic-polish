#!/usr/bin/env bash
# Cleans up a voice take recorded on a cheap USB microphone, for narration and video essays.
#
#   ./enhance.sh [--ai] [--warm] [--pitch N] <in.wav> [out.wav]   (default takes/<in>-enhanced.wav)
#
# --ai       runs Resemble Enhance first (ai_enhance.py, on the GPU when available): it removes the
#            noise and regenerates the voice, restoring the lows and highs the mic cuts. It replaces
#            steps 2-3. On a degraded test take it took the voice-to-noise gap from 36 dB to 71 dB
#            (filters alone: 47 dB). Needs the Python environment from ./setup.sh.
#
# --warm     adds +3 dB at 150 Hz and -1 dB at 3 kHz for a fuller voice. Off by default: on the
#            Portuguese takes the plain version was preferred, and heavier bass read as fake.
#
# --pitch N  shifts the voice N semitones (e.g. -1 for a slightly deeper voice), keeping its
#            resonances so it still sounds like the same person. Up to about -2 is unnoticeable.
#
# Chain, in order:
#   1. high-pass 80 Hz          desk thumps, rumble, mains hum
#   2. spectral denoise         learns the room's steady hiss from the take's first 3 s of silence
#   3. RNNoise (speech model)   the remaining noise, steady or not; does most of the work (if it
#                               ever sounds robotic, lower mix= to 0.9). DeepFilterNet replaces it
#                               when installed (runs first, on the raw take).
#   4. EQ                       -3 dB at 300 Hz (boxy cheap-mic sound), +3 dB at 4 kHz (clarity),
#                               +2 dB shelf above 10 kHz (air); --warm adds a fuller low end
#   5. de-esser                 tames the "s" sounds the presence boost brings up
#   6. compressor               evens out loud and quiet words (3:1 above -21 dB)
#   7. loudness                 measured (EBU R128), then one fixed gain to -16 LUFS: a voice that
#                               sits above game audio mixed around -24 LUFS
#   8. limiter                  holds the peaks that gain pushes up under -1.5 dBFS
#
# Every step is a line in the filter graph below; comment one out to hear what it does.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

usage() {
  echo "usage: $(basename "$0") [--ai] [--warm] [--pitch N] <in.wav> [out.wav]" >&2
  exit 2
}

ai=0
warm=0
pitch=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ai) ai=1; shift ;;
    --warm) warm=1; shift ;;
    --pitch)
      # A number only: it ends up inside a python3 -c expression below.
      [[ "${2:-}" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || { echo "--pitch takes a number of semitones, e.g. -1" >&2; exit 2; }
      pitch="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) break ;;
  esac
done
[ $# -ge 1 ] && [ $# -le 2 ] || usage
in="$1"
[ -f "$in" ] || { echo "input not found: $in" >&2; exit 1; }
for tool in ffmpeg ffprobe python3; do
  command -v "$tool" >/dev/null || { echo "missing dependency: $tool" >&2; exit 1; }
done

base="$(basename "${in%.*}")"
out="${2:-$here/takes/${base}-enhanced.wav}"
mkdir -p "$(dirname "$out")"
orig="$in"
model="$here/models/sh.rnnn"   # rnnoise-models "somnolent-hogwash": speech recorded with noise

# DeepFilterNet (github.com/Rikorose/DeepFilterNet), when its `deep-filter` binary is installed,
# replaces RNNoise: it is a newer speech-denoising network and usually cleaner on bad mics.
denoise="arnndn=m='$model':mix=1,"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
noisefloor="asendcmd=c='0.3 afftdn sn start; 2.8 afftdn sn stop',
afftdn=nr=12:nf=-50:tn=1,"
if [ "$ai" = 1 ]; then
  py="$here/.venv/bin/python"
  [ -x "$py" ] || { echo "--ai needs the Python environment: run ./setup.sh first" >&2; exit 1; }
  # The GPU runs this at ~3x real time. If a game holds the GPU, the driver can time the job out
  # and leave it stuck, so give it a deadline and fall back to the CPU (much slower).
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in")
  limit=$(python3 -c "print(int(60 + 2 * $dur))")
  if ! timeout "$limit" "$py" "$here/ai_enhance.py" "$in" "$tmp/ai.wav" --mode enhance 2>&1 | grep -E '^enhance'; then
    echo "GPU did not finish in ${limit}s (is a game using it?); running on the CPU instead" >&2
    "$py" "$here/ai_enhance.py" "$in" "$tmp/ai.wav" --mode enhance --device cpu 2>&1 | grep -E '^enhance'
  fi
  in="$tmp/ai.wav"
  denoise=""
  noisefloor=""
  echo "denoise: Resemble Enhance (AI)"
elif command -v deep-filter >/dev/null; then
  ffmpeg -hide_banner -v error -y -i "$in" -ar 48000 -ac 1 "$tmp/in.wav"
  deep-filter "$tmp/in.wav" -o "$tmp/df" >/dev/null
  in="$tmp/df/in.wav"
  denoise=""
  echo "denoise: DeepFilterNet"
else
  echo "denoise: RNNoise (install deep-filter for DeepFilterNet)"
fi

warmth=""
[ "$warm" = 1 ] && warmth="equalizer=f=150:t=q:w=1.0:g=3,equalizer=f=3000:t=q:w=1.5:g=-1,"

shift=""
if [ "$pitch" != 0 ]; then
  shift="rubberband=pitch=$(python3 -c "print(2 ** ($pitch / 12))"):formant=preserved:pitchq=quality,"
  echo "pitch: $pitch semitones"
fi

chain="highpass=f=80:poles=2,
$noisefloor
$denoise
$shift
equalizer=f=300:t=q:w=1.2:g=-3,
equalizer=f=4000:t=q:w=1.0:g=3,
highshelf=f=10000:g=2,
$warmth
deesser=i=0.4:m=0.5:f=0.5,
acompressor=threshold=-21dB:ratio=3:attack=5:release=80:makeup=2"
chain=$(echo "$chain" | tr -d '\n')

# Pass 1: measure loudness after the chain.
mi=$(ffmpeg -hide_banner -nostats -i "$in" -af "$chain,ebur128" -f null - 2>&1 | awk '/Integrated loudness/ {f=1} f && /I:/ {print $2; exit}')
[[ "$mi" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || { echo "could not measure loudness (is the take silent?)" >&2; exit 1; }

# Pass 2: one fixed gain to -16 LUFS, then a limiter for the peaks that gain pushes over -1.5 dBFS.
# (loudnorm's dynamic mode can't lift a quiet take more than ~10 dB, and its linear mode stops short
# to protect speech's high peaks; a fixed gain plus a limiter does both.)
gain=$(python3 -c "print(round(-16 - $mi, 2))")
echo "loudness: $mi LUFS -> -16 LUFS (gain ${gain} dB)"
ffmpeg -hide_banner -v error -y -i "$in" -af "$chain,volume=${gain}dB,aresample=48000,alimiter=limit=0.84:level=disabled:attack=1:release=60:asc=1" \
  -ar 48000 -ac 1 -c:a pcm_s24le "$out"

# Report: loudness, and the noise floor (the quietest 10% of 50 ms windows) before and after.
python3 -I - "$orig" "$out" <<'PY'
import subprocess, sys
import numpy as np
def load(p):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', p, '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)
def stats(x):
    w = 2400
    rms = np.sqrt(np.array([np.mean(x[i:i + w]**2) for i in range(0, len(x) - w, w)]) + 1e-12)
    db = 20 * np.log10(rms)
    return np.percentile(db, 10), np.percentile(db, 95)
for label, p in (('before', sys.argv[1]), ('after', sys.argv[2])):
    floor, speech = stats(load(p))
    print(f'{label:>6}: noise floor {floor:6.1f} dBFS   speech {speech:6.1f} dBFS   gap {speech - floor:5.1f} dB')
PY
echo "-> $out"
