#!/usr/bin/env bash
# A whole gameplay session: the game removed from the mic, the voice polished, the speech marked.
#   ./session.sh <recording.mkv> [options]      (see: ./session.sh --help)
here=$(cd "$(dirname "$0")" && pwd)
py="$here/.venv/bin/python"
[ -x "$py" ] || { echo "needs the Python environment: run ./setup.sh first" >&2; exit 1; }
cd "$here" && exec "$py" -m engine.session "$@"
