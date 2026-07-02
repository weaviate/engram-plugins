#!/usr/bin/env python3
"""Ensure the third-party Engram SDK is installed in the plugin's venv. Idempotent.

Runs on demand from hooks/with-venv.sh until a successful install is recorded, so the plugin
works on the first prompt after install without a restart — the one-time build happens on that
prompt.

The core package itself ships inside this plugin dir and is imported straight from
the plugin root (see hooks/with-venv.sh), so the ONLY thing installed here is the SDK pinned
in ../requirements.txt. Re-installs when that file changes (the marker stores its contents).
Always exits 0 — a flaky network must never block a coding session. If the install fails the SDK
stays missing and the hook's `import engram` errors loudly; with-venv.sh re-runs this on the next
prompt (marker-gated) until it succeeds."""

import os
import subprocess
import sys


def _plugin_root():
    # Host-provided (with-venv.sh, our only caller, guarantees it). No fallback: fail loudly
    # rather than guess.
    d = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not d:
        sys.stderr.write("Engram · CLAUDE_PLUGIN_ROOT not set\n")
        sys.exit(1)
    return d


def _data_dir():
    # Host-provided; no fallback (a guessed path diverges from where the hooks read). with-venv.sh
    # already guards this, but fail loudly here too if somehow run without it.
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        sys.stderr.write("Engram · CLAUDE_PLUGIN_DATA not set\n")
        sys.exit(1)
    return d


def _notify(message):
    """Surface a failure to stderr. If the install fails the SDK stays missing and the hook's
    `import engram` errors loudly; with-venv.sh re-runs this (marker-gated) on the next prompt."""
    sys.stderr.write(f"Engram · {message}\n")


def main():
    req = os.path.join(_plugin_root(), "requirements.txt")
    try:
        spec = open(req).read()
    except Exception as e:
        _notify(f"cannot read {req}: {e}")
        return 0

    data = _data_dir()
    venv = os.path.join(data, "venv")
    py = os.path.join(venv, "bin", "python")
    marker = os.path.join(venv, "install-spec")
    try:
        if os.path.exists(py) and open(marker).read() == spec:
            return 0
    except Exception:
        pass  # marker unreadable → fall through and rebuild (idempotent)

    try:
        os.makedirs(data, exist_ok=True)
        if not os.path.exists(py):
            subprocess.run(
                [sys.executable, "-m", "venv", venv],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        subprocess.run(
            [py, "-m", "pip", "install", "-q", "--upgrade", "-r", req],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        with open(marker, "w") as f:
            f.write(spec)
    except Exception as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip()[-500:]
        _notify(f"dependency install failed: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
