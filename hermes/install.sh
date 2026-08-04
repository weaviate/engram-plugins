#!/usr/bin/env bash
# Install the Engram memory provider for Hermes Agent.
#
# Hermes discovers memory providers in two places:
#   1. $HERMES_HOME/plugins/<name>/             (user-installed — survives `hermes update`)
#   2. <hermes checkout>/plugins/memory/<name>/ (bundled — the only option on older versions)
# This script picks the best target automatically: the user-plugins dir when the
# installed Hermes supports it, otherwise the checkout's bundled dir.
#
# Usage:
#   bash install.sh [--link] [--bundled] [HERMES_REPO]
#
#   --link       symlink instead of copying (development — edits here go live)
#   --bundled    force install into the checkout's plugins/memory/ (e.g. for an
#                upstream PR worktree) even if user-plugins are supported
#   HERMES_REPO  path to the Hermes checkout; auto-detected when omitted:
#                $HERMES_REPO → $HERMES_HOME/hermes-agent (~/.hermes default)
#                → /usr/local/lib/hermes-agent (root installs)

set -euo pipefail

usage() {
  sed -n '14,23p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,\} \{0,1\}//'
}

LINK=false
BUNDLED=false
REPO_ARG=""
for arg in "$@"; do
  case "$arg" in
    --link) LINK=true ;;
    --bundled) BUNDLED=true ;;
    -h|--help) usage; exit 0 ;;
    *) REPO_ARG="$arg" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/engram"
HH="${HERMES_HOME:-$HOME/.hermes}"

if [ ! -f "$SRC/__init__.py" ]; then
  echo "error: provider package not found at $SRC" >&2
  exit 1
fi

find_repo() {
  local candidates=()
  if [ -n "$REPO_ARG" ]; then
    candidates+=("$REPO_ARG")
  fi
  if [ -n "${HERMES_REPO:-}" ]; then
    candidates+=("$HERMES_REPO")
  fi
  candidates+=("$HH/hermes-agent")
  candidates+=("/usr/local/lib/hermes-agent")
  local c
  for c in "${candidates[@]}"; do
    if [ -d "$c/plugins/memory" ]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

REPO="$(find_repo || true)"

supports_user_plugins() {
  # User-installed providers ($HERMES_HOME/plugins/) were added to Hermes'
  # discovery after the first bundled-only versions — detect by the marker
  # function in the checkout's plugins/memory/__init__.py.
  [ -n "$1" ] && grep -q "_get_user_plugins_dir" "$1/plugins/memory/__init__.py" 2>/dev/null
}

DEST=""
if [ "$BUNDLED" = false ]; then
  if supports_user_plugins "$REPO"; then
    DEST="$HH/plugins/engram"
  elif [ -z "$REPO" ] && [ -d "$HH/plugins" ]; then
    # No checkout in the usual places (e.g. desktop install), but the
    # user-plugins dir exists — Hermes clearly supports it.
    DEST="$HH/plugins/engram"
  fi
fi

if [ -z "$DEST" ] && [ -n "$REPO" ]; then
  DEST="$REPO/plugins/memory/engram"
fi

if [ -z "$DEST" ]; then
  cat >&2 <<EOF
error: no Hermes Agent installation found.

Looked for a checkout in:
  \$HERMES_REPO, $HH/hermes-agent, /usr/local/lib/hermes-agent
and for a user-plugins dir in:
  $HH/plugins

Pass the checkout path explicitly:
  bash install.sh /path/to/hermes-agent

(No Hermes yet? Install it first:
 curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash)
EOF
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

# Replace whatever is there (a previous copy or an old symlink). rm -rf on a
# symlink removes the link, never its target.
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  rm -rf "$DEST"
fi

if [ "$LINK" = true ]; then
  ln -s "$SRC" "$DEST"
  echo "Linked $DEST -> $SRC"
else
  # tar-to-tar copy excludes Python caches
  tar -cf - --exclude='__pycache__' --exclude='*.pyc' -C "$SCRIPT_DIR" engram \
    | tar -xf - -C "$(dirname "$DEST")"
  echo "Copied $SRC -> $DEST"
fi

cat <<EOF

Engram memory provider installed.

Next steps:
  1. Get an API key at https://console.weaviate.cloud/engram
  2. Run:  hermes memory setup    (choose "engram", paste the key)

     or manually:
       hermes config set memory.provider engram
       echo "ENGRAM_API_KEY=..." >> ~/.hermes/.env
  3. Start a chat — memory works from the first turn.
EOF
