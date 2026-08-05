"""claude-mem source (github.com/thedotmack/claude-mem): reads its local SQLite store.

Strictly read-only (sqlite URI mode=ro) — migration must never mutate the source. Only the
two LLM-summarized tables are read: `observations` and `session_summaries`. Raw
`user_prompts`, the FTS shadow tables, and sync bookkeeping carry no memory value."""

import json
import os
import sqlite3
from urllib.parse import quote

from . import Record

DEFAULT_DB = "~/.claude-mem/claude-mem.db"

# claude-mem observation `type` → our kind. Everything describing the codebase and its
# decisions maps to `architecture`; work items map to `task`. A type missing here (from a
# newer claude-mem) is skipped and reported by describe_selection rather than mis-filed.
KIND_BY_TYPE = {
    "discovery": "architecture",
    "change": "architecture",
    "refactor": "architecture",
    "decision": "architecture",
    "security_alert": "architecture",
    "security_note": "architecture",
    "bugfix": "task",
    "feature": "task",
}

# Migrated by default. discovery/change/refactor are transient per-session observations —
# the bulk of a store (>80% here) with low recall value — so they ride behind --all instead
# of polluting recall out of the box.
CURATED_TYPES = ("decision", "bugfix", "feature", "security_alert", "security_note")


def _obs_content(title, narrative, text, facts):
    """Title + narrative is the memory; `text` is the pre-narrative legacy column. Rows with
    neither fold the `facts` JSON array into readable text instead of being dropped."""
    body = (narrative or text or "").strip()
    if not body and facts:
        try:
            parsed = json.loads(facts)
        except Exception:
            parsed = None
        # only a JSON array joins element-wise — a stray object or string would otherwise
        # degrade into joined keys or characters
        if isinstance(parsed, list):
            body = "; ".join(str(f) for f in parsed)
        else:
            body = str(facts).strip()
    parts = [p.strip() for p in (title or "", body) if p and p.strip()]
    return " — ".join(parts)


def _summary_content(row):
    fields = (
        ("Request", row[0]),
        ("Learned", row[1]),
        ("Completed", row[2]),
        ("Next steps", row[3]),
        ("Notes", row[4]),
    )
    body = "\n".join(f"{k}: {v.strip()}" for k, v in fields if v and v.strip())
    return "Session summary —\n" + body if body else ""


class ClaudeMemSource:
    name = "claude-mem"

    def __init__(self, path=None):
        self.db_path = os.path.expanduser(path or DEFAULT_DB)

    def available(self):
        return self.db_path if os.path.exists(self.db_path) else None

    def _connect(self):
        # mode=ro guarantees read-only at the sqlite level, not just by convention.
        # Percent-encoding keeps URI-reserved characters in the path (?, #, %) from being
        # parsed as query/fragment — a crafted "...db?mode=rw" must not defeat ro.
        return sqlite3.connect("file:" + quote(self.db_path, safe="/") + "?mode=ro", uri=True)

    def _types(self, include_all):
        return list(KIND_BY_TYPE) if include_all else list(CURATED_TYPES)

    def records(self, include_all=False):
        con = self._connect()
        try:
            yield from self._observations(con, include_all)
            yield from self._summaries(con)
        finally:
            con.close()

    def _observations(self, con, include_all):
        types = self._types(include_all)
        # merged_into_project is claude-mem's own project rename/merge — honor it
        q = (
            "SELECT id, type, title, narrative, text, facts, created_at, "
            "COALESCE(merged_into_project, project) FROM observations "
            f"WHERE type IN ({','.join('?' * len(types))}) ORDER BY id"
        )
        for oid, typ, title, narrative, text, facts, created, project in con.execute(q, types):
            content = _obs_content(title, narrative, text, facts)
            if not content:
                continue
            yield Record(
                uid=f"obs:{oid}",
                kind=KIND_BY_TYPE[typ],
                content=content,
                created_at=created,
                project=project,
            )

    def _summaries(self, con):
        q = (
            "SELECT id, request, learned, completed, next_steps, notes, created_at, "
            "COALESCE(merged_into_project, project) FROM session_summaries ORDER BY id"
        )
        for row in con.execute(q):
            content = _summary_content(row[1:6])
            if not content:
                continue
            # summaries are progress/outcome reports → task (see engine.KIND_TO_TOPIC for
            # why they don't go to SessionHistory)
            yield Record(
                uid=f"sum:{row[0]}",
                kind="task",
                content=content,
                created_at=row[6],
                project=row[7],
            )

    def describe_selection(self, include_all=False):
        """Dry-run report lines: what's included, what --all would add, unknown types."""
        con = self._connect()
        try:
            # NULL types can't be migrated (records() filters on type IN (...)) and would
            # crash the sorted() below — drop them from the report rather than the CLI
            counts = dict(
                con.execute(
                    "SELECT type, COUNT(*) FROM observations "
                    "WHERE type IS NOT NULL GROUP BY type"
                )
            )
            (n_sum,) = con.execute("SELECT COUNT(*) FROM session_summaries").fetchone()
        finally:
            con.close()

        def fmt(names):
            return ", ".join(f"{t} ({counts[t]})" for t in sorted(names)) or "none"

        chosen = set(self._types(include_all))
        excluded = [t for t in counts if t in KIND_BY_TYPE and t not in chosen]
        unknown = [t for t in counts if t not in KIND_BY_TYPE]
        lines = [
            f"observations included: {fmt(t for t in counts if t in chosen)}",
            f"session summaries included: {n_sum}",
        ]
        if excluded:
            lines.append(f"observations excluded (add --all): {fmt(excluded)}")
        if unknown:
            lines.append(f"unknown observation types skipped: {fmt(unknown)}")
        return lines
