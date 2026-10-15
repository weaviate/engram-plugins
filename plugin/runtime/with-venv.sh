#!/usr/bin/env bash
# Run a Python module under the plugin's own venv (built by ensure_deps.py into the plugin data
# dir), which carries only the third-party Engram SDK. The plugin's packages ship inside
# this plugin dir, so we add the plugin root to PYTHONPATH to import it. Falls back to system
# python3 until the venv is built. stdin is passed through.
# usage: with-venv.sh <plugin data dir> <python args...>
# Each assistant's hooks file passes DATA from its host's own variable, and ROOT is where this
# script lives, so nothing here names a host. No fallback, so a misconfigured run fails loudly
# instead of guessing a path.
ROOT="$(dirname "$(cd "$(dirname "$0")" && pwd -P)")"
DATA="${1:-}"
if [ -z "$DATA" ]; then
  echo "Engram · with-venv.sh needs the plugin data dir as its first argument" >&2
  exit 1
fi
shift
PY="$DATA/venv/bin/python"
# Build/repair the venv on demand until a successful install is recorded (ensure_deps writes the
# install-spec marker only after pip succeeds). Gating on the marker — not just the venv python —
# means a partial install (venv created but pip failed) retries next prompt instead of crashing on
# `import engram` forever. Idempotent once installed.
[ -f "$DATA/venv/install-spec" ] || python3 "$ROOT/runtime/ensure_deps.py" "$DATA" >&2
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" "$@"
