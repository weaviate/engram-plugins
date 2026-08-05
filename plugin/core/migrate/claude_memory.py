"""Claude Code local-memory source: per-project markdown fact files.

Claude Code keeps per-project memories under ~/.claude/projects/<munged-path>/memory/*.md
(MEMORY.md is an index, not a fact, and is skipped). They are siloed to the project they
were written in; migrating them into Engram makes them recallable everywhere.

The munged directory name is decoded back to a real path by a filesystem-pruned search
(the munging maps both '/' and '.' to '-', so decoding is ambiguous without checking what
actually exists on disk). The decoded absolute path becomes the record's project, so the
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
from ..util import git_repo

DEFAULT_DIR = "~/.claude/projects"

def decode_project_dir(name):
    """Decode a munged project dir name (e.g. '-Users-me-src-repo--bare') back to candidate
    absolute paths. The munging maps '/' and '.' to '-' and keeps literal '-', so each '-'
    is a three-way branch — but every branch is pruned against the real directory listing
    (a component prefix that matches no entry is dead), which bounds the search by what
    exists on disk instead of exponential in the dash count (an unpruned search hangs on
    ~25 dashes — an ordinary deep kebab-case path). Iterative on an explicit stack so a
    pathological name can't hit the recursion limit. A candidate must re-munge to exactly
    `name`, which also rejects paths mangled by accidental '..' components."""
    if not name.startswith("-"):
        return []

    listings = {}

    def entries(d):
        if d not in listings:
            try:
                listings[d] = os.listdir(d)
            except OSError:
                listings[d] = []
        return listings[d]

    matches = []
    stack = [("/", "", name[1:])]  # (confirmed dir, partial component, rest of name)
    while stack:
        dirpath, partial, rest = stack.pop()
        i = rest.find("-")
        if i < 0:
            full = os.path.join(dirpath, partial + rest)
            if os.path.isdir(full):
                matches.append(full)
            continue
        comp, tail = partial + rest[:i], rest[i + 1 :]
        # '/' branch: comp is a complete path component
        if comp in entries(dirpath) and os.path.isdir(os.path.join(dirpath, comp)):
            stack.append((os.path.join(dirpath, comp), "", tail))
        # '.'/'-' branches: the component continues — only viable if some real entry
        # starts with it
        for ch in (".", "-"):
            nxt = comp + ch
            if any(e.startswith(nxt) for e in entries(dirpath)):
                stack.append((dirpath, nxt, tail))
    return [m for m in matches if m.replace("/", "-").replace(".", "-") == name]


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

    def records(self, include_all=False):
        # include_all is moot for this source: every file is a deliberately saved fact,
        # there is no low-signal tier to exclude
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

    def describe_selection(self, include_all=False):
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
