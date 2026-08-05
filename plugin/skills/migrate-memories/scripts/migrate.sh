#!/usr/bin/env bash
# Harness-independent entry point for the migration CLI. Skills run under different hosts
# (Claude Code, Codex, ...) with no shared env-var contract, so resolve the plugin root
# from this file's own location instead of any host-provided variable and delegate to the
# plugin-level launcher (which derives the rest the same way).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
exec bash "$HERE/../../../bin/engram-migrate" "$@"
