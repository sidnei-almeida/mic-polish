#!/usr/bin/env bash
# One-time gain fix for the Arcano AM-BLACK-1 (USB, C-Media chip): its hardware capture gain ships
# at the minimum (0/62 = -8 dB), so the voice arrives quiet and every later boost lifts the noise
# with it. Gain belongs at the source. This sets the hardware gain (default 50/62, about +17 dB;
# 62/62 clipped, 40/62 peaked at only -23 dBFS) and the PipeWire source to 100%, so nothing in
# software boosts it again.
#
#   ./setup-mic.sh [hw_gain 0-62]
#
# Other C-Media USB mics usually expose the same ALSA controls. Point the script at yours with
# MIC_POLISH_CARD (a substring of the name in `arecord -l`) and MIC_POLISH_SOURCE (a substring of
# the name in `pactl list short sources`).
#
# Check with a test recording: speaking normally should peak around -12 dBFS, never hit 0.
# If it clips when you get excited, lower the number; if it peaks below -20 dBFS, raise it.
set -euo pipefail
gain="${1:-50}"
[[ "$gain" =~ ^[0-9]+$ ]] && [ "$gain" -le 62 ] || { echo "gain must be a number from 0 to 62" >&2; exit 2; }
card_match="${MIC_POLISH_CARD:-AM-BLACK-1}"
src_match="${MIC_POLISH_SOURCE:-ARCANO_AM-BLACK-1}"
card=$(arecord -l | awk -F'[ :]' -v m="$card_match" 'index($0, m) && /^card/ {print $2; exit}')
[ -n "$card" ] || { echo "no capture card matching '$card_match' (set MIC_POLISH_CARD)" >&2; exit 1; }
src=$(pactl list short sources | awk -v m="$src_match" 'index($2, m) && $2 !~ /monitor/ {print $2; exit}')
[ -n "$src" ] || { echo "no PipeWire source matching '$src_match' (set MIC_POLISH_SOURCE)" >&2; exit 1; }
# PipeWire drives the hardware gain from the source volume: setting the source to 100% pushes the
# mic's gain to its maximum (62/62, +22.5 dB), which clips. So set PipeWire first, then the
# hardware gain last, so it sticks.
pactl set-source-mute "$src" 0
pactl set-source-volume "$src" 100%
sleep 0.3
amixer -q -c "$card" cset name='Mic Capture Switch' on
amixer -q -c "$card" cset name='Mic Capture Volume' "$gain"
sleep 0.3
now=$(amixer -c "$card" cget name='Mic Capture Volume' | awk -F= '/: values/ {print $2}')
echo "hardware gain: $now/62 (asked for $gain)   PipeWire: $(pactl get-source-volume "$src" | grep -o '[0-9]*%' | head -1)"
[ "$now" = "$gain" ] || echo "warning: something reset the hardware gain" >&2
