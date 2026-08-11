"""Claude Code's per-project session registry, used as a directory index.

~/.claude/projects/ holds one munged directory name per working directory the user ever
ran a Claude Code session in. Decoding those names back to real paths reconstructs the
user's actual layout — wherever they keep their repositories — so a source project
recorded only as a directory basename (claude-mem stores basename(cwd)) can be located
without assuming any workspace convention."""

import os

DEFAULT_PROJECTS_DIR = "~/.claude/projects"


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


def index_by_basename(projects_dir=DEFAULT_PROJECTS_DIR):
    """basename → [paths] for every registry entry that decodes to an existing directory,
    most recent session first (the registry entry's mtime tracks the last session)."""
    root = os.path.expanduser(projects_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return {}
    dated = {}
    for name in names:
        try:
            mtime = os.path.getmtime(os.path.join(root, name))
        except OSError:
            mtime = 0.0
        for path in decode_project_dir(name):
            dated.setdefault(os.path.basename(path), []).append((mtime, path))
    index = {}
    for base, entries_ in dated.items():
        entries_.sort(reverse=True)
        index[base] = [p for _, p in entries_]
    return index
