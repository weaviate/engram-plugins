"""Claude Code local-memory source: per-project markdown fact files.

Claude Code keeps per-project memories under ~/.claude/projects/<munged-path>/memory/*.md
(MEMORY.md is an index, not a fact, and is skipped). They are siloed to the project they
were written in; migrating them into Engram makes them recallable everywhere.

The munged directory name is decoded back to a real path by the shared registry decoder
(core.migrate.claude_projects). The decoded absolute path becomes the record's project, so the
engine resolves repo_name from that directory's git remote — the same property the store
hook attaches, keeping migrated and hook-stored memories identically scoped.

Records carry the file's content hash in their uid: editing a fact file yields a new uid,
so the changed content is re-migrated on the next run while the checkpoint still remembers
the old version."""

import glob
import hashlib
import os
from datetime import datetime, timezone

from . import Record
from .claude_projects import decode_project_dir
from ..util import git_repo

DEFAULT_DIR = "~/.claude/projects"

def _resolve_project(name):
    """Best decoded path for a munged dir name: prefer a candidate with a git origin (the
    one worth scoping by), else the first existing decoding, else None (deleted since)."""
    candidates = decode_project_dir(name)
    for path in candidates:
        if git_repo(path):
            return path
    return candidates[0] if candidates else None


def _parse_memory(text):
    """(meta, body) from a memory file: `name:`/`description:`/`type:` out of the
    frontmatter (naive line scan — `type` sits indented under `metadata:`, stripping
    handles it), body after the closing '---'. No frontmatter → whole file is the body."""
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


class ClaudeMemorySource:
    name = "claude-memory"

    def __init__(self, path=None):
        self.db_path = os.path.expanduser(path or DEFAULT_DIR)

    def _files(self):
        return sorted(
            f
            for f in glob.glob(os.path.join(self.db_path, "*", "memory", "*.md"))
            if os.path.basename(f) != "MEMORY.md"
        )

    def available(self):
        return self.db_path if self._files() else None

    def records(self):
        projects = {}
        for f in self._files():
            proj_name = os.path.basename(os.path.dirname(os.path.dirname(f)))
            if proj_name not in projects:
                projects[proj_name] = _resolve_project(proj_name)
            text = open(f, encoding="utf-8", errors="replace").read()
            meta, body = _parse_memory(text)
            title = meta.get("description") or meta.get("name") or ""
            content = " — ".join(p for p in (title, body) if p)
            if not content:
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()[:12]
            ts = datetime.fromtimestamp(os.path.getmtime(f), tz=timezone.utc)
            yield Record(
                uid=f"{f}@{digest}",
                content=content,
                # no authored timestamp exists; the file's mtime is the best available
                created_at=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                project=projects[proj_name] or proj_name,
            )

    def describe_selection(self):
        files = self._files()
        counts, decodable, unresolved = {}, {}, set()
        for f in files:
            proj_name = os.path.basename(os.path.dirname(os.path.dirname(f)))
            if proj_name not in decodable:  # decode once per project, not per file
                decodable[proj_name] = bool(decode_project_dir(proj_name))
            if not decodable[proj_name]:
                unresolved.add(proj_name)
            meta, _ = _parse_memory(open(f, encoding="utf-8", errors="replace").read())
            t = meta.get("type") or "(untyped)"
            counts[t] = counts.get(t, 0) + 1
        lines = [
            f"memory files included: {len(files)} "
            f"({', '.join(f'{t} ({n})' for t, n in sorted(counts.items()))})"
        ]
        if unresolved:
            lines.append(
                f"projects whose path no longer exists: {len(unresolved)} — their munged "
                "names can't resolve a repo; recover with --map=<munged-name>=owner/repo "
                "(the leading '-' requires the '=' form)"
            )
        return lines
