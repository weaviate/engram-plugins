#!/usr/bin/env bash
# Run a Python module under the plugin's own venv (built by ensure_deps.py into the plugin data
# dir), which carries only the third-party Engram SDK. The core package ships inside
# this plugin dir, so we add the plugin root to PYTHONPATH to import it. Falls back to system
# python3 until the venv is built. stdin is passed through.
# ROOT and DATA are host-provided (CLAUDE_PLUGIN_ROOT/DATA) — no derivation, no fallback, so a
# misconfigured run fails loudly instead of guessing a path.
ROOT="${CLAUDE_PLUGIN_ROOT:-}"
DATA="${CLAUDE_PLUGIN_DATA:-}"
if [ -z "$ROOT" ] || [ -z "$DATA" ]; then
  echo "Engram · CLAUDE_PLUGIN_ROOT / CLAUDE_PLUGIN_DATA not set" >&2
  exit 1
fi
PY="$DATA/venv/bin/python"
# Build/repair the venv on demand until a successful install is recorded (ensure_deps writes the
# install-spec marker only after pip succeeds). Gating on the marker — not just the venv python —
# means a partial install (venv created but pip failed) retries next prompt instead of crashing on
# `import engram` forever. Idempotent once installed.
[ -f "$DATA/venv/install-spec" ] || python3 "$ROOT/hooks/ensure_deps.py" >&2
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" "$@"
