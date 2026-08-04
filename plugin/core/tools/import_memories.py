#!/usr/bin/env python3
"""One-shot importer: Claude Code's local file-based memories -> Engram.

Claude Code keeps per-project memories as markdown fact files under
~/.claude/projects/<munged-path>/memory/*.md (MEMORY.md is an index, not a fact, and is
skipped). Those files are siloed per project; importing them into Engram makes them
recallable everywhere.

Each fact is sent as a single user message (Engram's extraction distills it) tagged with
`repo_name` resolved from the source project's `git remote get-url origin` — the same
property the store hook attaches — so recall scoping treats imported and hook-stored
memories identically. The munged directory name is decoded back to a real path by a
filesystem-pruned search (the munging maps both '/' and '.' to '-', so decoding is
ambiguous without checking what actually exists on disk).

Already-imported files are recorded (by content hash) in the plugin data dir and skipped
on re-runs; --force re-imports everything. Invoked by the /engram:import-memories command
via with-venv.sh; prints a human-readable report and exits 0 unless nothing could run at all.
"""

import argparse
import glob
import hashlib
import json
import os
import sys

from core import get_client, get_user_id
from core.scope import scope_schema
from core.util import data_dir, git_repo

STATE_FILE = "claude-import-state.json"


def decode_project_dir(name):
    """Decode a munged project dir name (e.g. '-Users-me-src-repo--bare') back to candidate
    absolute paths. The munging maps '/' and '.' to '-' and keeps literal '-', so each '-'
    is a three-way branch; pruning against real directories keeps the search tiny. A
    candidate must re-munge to exactly `name`, which also rejects paths mangled by
    accidental '..' components."""
    matches = []

    def rec(prefix, rest):
        i = rest.find("-")
        if i < 0:
            full = prefix + rest
            if os.path.isdir(full):
                matches.append(full)
            return
        comp, tail = prefix + rest[:i], rest[i + 1 :]
        if os.path.isdir(comp):
            rec(comp + "/", tail)
        rec(comp + ".", tail)
        rec(comp + "-", tail)

    if name.startswith("-"):
        rec("/", name[1:])
    return [m for m in matches if m.replace("/", "-").replace(".", "-") == name]


def resolve_project(name):
    """(path, repo_slug) for a munged project dir name. Among candidate decodings, prefer
    one that has a git origin — that's the one worth tagging with."""
    for path in decode_project_dir(name):
        slug = git_repo(path)
        if slug:
            return path, slug
    cands = decode_project_dir(name)
    return (cands[0], None) if cands else (None, None)


def parse_memory(text):
    """(meta, body) from a memory file: `name:`/`description:`/`type:` out of the frontmatter
    (naive line scan — `type` sits indented under `metadata:`, stripping handles it), body
    after the closing '---'. No frontmatter -> whole file is the body."""
    meta = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta, text.strip()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return meta, text.strip()
    for ln in lines[1:end]:
        s = ln.strip()
        for key in ("name", "description", "type"):
            if s.startswith(key + ":") and key not in meta:
                meta[key] = s[len(key) + 1 :].strip()
    return meta, "\n".join(lines[end + 1 :]).strip()


def build_message(meta, body, slug, project_path):
    """The fact as a single user message. Repo/type context goes in the text (not properties)
    so extraction keeps it even where the scope schema has no matching property."""
    bits = []
    if slug:
        bits.append(f"repository: {slug}")
    elif project_path:
        bits.append(f"project: {project_path}")
    if meta.get("type"):
        bits.append(f"kind: {meta['type']}")
    header = (
        "Note imported from my Claude Code project memory"
        + (f" ({', '.join(bits)})" if bits else "")
        + ". Please remember this:"
    )
    title = meta.get("description") or meta.get("name") or ""
    return "\n\n".join(p for p in (header, title, body) if p)


def repo_properties(slug):
    """{'repo_name': slug} unless the group's schema is known and lacks repo_name (then the
    server would have nothing to do with it). Schema unavailable -> send it anyway; the
    server is the authority."""
    if not slug:
        return None
    try:
        allowed = scope_schema().get("properties", [])
    except Exception:
        return {"repo_name": slug}
    return {"repo_name": slug} if "repo_name" in allowed else None


def load_state():
    try:
        with open(os.path.join(data_dir(), STATE_FILE)) as f:
            return json.load(f)
    except Exception:
        return {}  # no data dir / first run / corrupt state -> treat everything as new


def save_state(state):
    try:
        path = os.path.join(data_dir(), STATE_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    except Exception as e:
        print(f"warning: could not record import state ({e}) — a re-run will re-import.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without storing")
    ap.add_argument("--force", action="store_true", help="re-import already-imported files")
    ap.add_argument(
        "--projects-dir",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects dir (default: ~/.claude/projects)",
    )
    args = ap.parse_args(argv)

    files = sorted(
        f
        for f in glob.glob(os.path.join(args.projects_dir, "*", "memory", "*.md"))
        if os.path.basename(f) != "MEMORY.md"
    )
    if not files:
        print(f"No Claude memory files found under {args.projects_dir}.")
        return 0

    client, user_id = None, None
    if not args.dry_run:
        client = get_client()
        if client is None:
            print("ENGRAM_API_KEY not set — cannot import. Set it and re-run.")
            return 1
        user_id = get_user_id()
        if not user_id:
            print(
                "No stable identity — set git user.email or ENGRAM_USER_ID and re-run "
                "(prevents mixing memories between users)."
            )
            return 1

    state = load_state()
    projects = {}  # munged dir name -> (path, slug), resolved once per project
    counts = {"imported": 0, "already": 0, "empty": 0, "failed": 0}

    for f in files:
        proj = os.path.basename(os.path.dirname(os.path.dirname(f)))
        if proj not in projects:
            projects[proj] = resolve_project(proj)
        path, slug = projects[proj]
        where = slug or path or proj
        rel = os.path.basename(f)

        text = open(f, encoding="utf-8", errors="replace").read()
        meta, body = parse_memory(text)
        if not body and not meta.get("description"):
            counts["empty"] += 1
            print(f"skip (no content):  {where} · {rel}")
            continue

        digest = hashlib.sha256(text.encode()).hexdigest()
        if not args.force and state.get(f) == digest:
            counts["already"] += 1
            print(f"skip (already imported): {where} · {rel}")
            continue

        label = meta.get("description") or meta.get("name") or rel
        if args.dry_run:
            counts["imported"] += 1
            tag = f" [repo_name={slug}]" if slug else " [no repo tag]"
            print(f"would import: {where} · {rel}{tag} — {label}")
            continue

        try:
            client.memories.add(
                [{"role": "user", "content": build_message(meta, body, slug, path)}],
                user_id=user_id,
                properties=repo_properties(slug),
            )
        except Exception as e:
            counts["failed"] += 1
            print(f"FAILED: {where} · {rel} — {e}")
            continue
        state[f] = digest
        counts["imported"] += 1
        print(f"imported: {where} · {rel} — {label}")

    if not args.dry_run and counts["imported"]:
        save_state(state)

    unresolved = sorted(p for p, (path, _) in projects.items() if not path)
    verb = "would import" if args.dry_run else "imported"
    print(
        f"\n{verb}: {counts['imported']}  ·  already imported: {counts['already']}"
        f"  ·  empty: {counts['empty']}  ·  failed: {counts['failed']}"
    )
    if unresolved:
        print(
            "projects whose path no longer exists (imported without a repo tag): "
            + ", ".join(unresolved)
        )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
