#!/usr/bin/env bash
# Records the voice on its own, straight from the microphone, as 48 kHz 32-bit float mono WAV,
# while the game or screen is captured separately (OBS etc.). Stop with Ctrl+C.
#
#   ./record.sh [out.wav]            (default takes/take-<date>.wav)
#
# The microphone is the first PipeWire source whose name matches MIC_POLISH_SOURCE
# (default: ARCANO_AM-BLACK-1). List yours with: pactl list short sources
#
# Start each take with ~3 s of silence: the enhancer learns the room noise from it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
match="${MIC_POLISH_SOURCE:-ARCANO_AM-BLACK-1}"
out="${1:-$here/takes/take-$(date +%Y%m%d-%H%M%S).wav}"
mkdir -p "$(dirname "$out")"
src=$(pactl list short sources | awk -v m="$match" 'index($2, m) && $2 !~ /monitor/ {print $2; exit}')
[ -n "$src" ] || { echo "no PipeWire source matching '$match' (set MIC_POLISH_SOURCE)" >&2; exit 1; }
echo "Recording from $src -> $out (Ctrl+C to stop). Stay silent for the first 3 seconds."
pw-record --target "$src" --rate 48000 --channels 1 --format f32 "$out"
