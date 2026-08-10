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
prompt (marker-gated) until it succeeds.

The venv is built with a Python that meets the SDK's floor (>= 3.11), NOT the interpreter running
this script — with-venv.sh launches us with bare `python3`, which on many machines is an old
system build (macOS ships 3.9). Prefers uv, which can download an interpreter when none is
installed; otherwise scans PATH for the newest suitable one. A venv left behind by an old
interpreter is rebuilt."""

import os
import shutil
import subprocess
import sys

MIN_VERSION = (3, 11)
CANDIDATES = (
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3",
    "python",
)


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


def _passes(exe, check):
    try:
        return (
            subprocess.run(
                [exe, "-c", check], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except Exception:
        return False


def _version_ok(exe):
    check = "import sys; sys.exit(0 if sys.version_info >= {} else 1)".format(
        MIN_VERSION
    )
    return _passes(exe, check)


def _venv_ok(exe):
    # Also require pip: a failed ensurepip (Debian without the python3.x-venv package) leaves a
    # working interpreter with no pip, and that venv must be rebuilt, not reused.
    check = "import sys, pip; sys.exit(0 if sys.version_info >= {} else 1)".format(
        MIN_VERSION
    )
    return _passes(exe, check)


def _find_python():
    for name in CANDIDATES:
        exe = shutil.which(name)
        if exe and _version_ok(exe):
            return exe
    return None


def _create_venv(venv):
    # Remove any leftover venv here rather than per-branch: uv errors on an existing venv unless
    # given --clear, which older uv versions lack.
    shutil.rmtree(venv, ignore_errors=True)
    uv = shutil.which("uv")
    if uv:
        try:
            # --seed installs pip so the install step below works the same either way.
            subprocess.run(
                [uv, "venv", "--seed", "--python", ">=3.11", venv],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return True
        except Exception:
            pass  # e.g. offline with no local interpreter to fall back on — try PATH instead
    exe = _find_python()
    if not exe:
        _notify("needs Python >= 3.11, but none was found")
        return False
    subprocess.run(
        [exe, "-m", "venv", venv],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return True


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
        if os.path.exists(py) and open(marker).read() == spec and _venv_ok(py):
            return 0
    except Exception:
        pass  # marker unreadable → fall through and rebuild (idempotent)

    try:
        os.makedirs(data, exist_ok=True)
        # Also rebuilds venvs left behind by an old interpreter (pip failed there, so no marker).
        if (not os.path.exists(py) or not _venv_ok(py)) and not _create_venv(venv):
            return 0
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
        detail = (
            getattr(e, "stderr", None) or getattr(e, "stdout", None) or str(e)
        ).strip()[-500:]
        _notify(f"dependency install failed: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
