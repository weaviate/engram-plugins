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

    def records(self):
        # every observation type migrates — Engram's extraction decides what each memory
        # becomes and what to keep, so the adapter does no relevance filtering of its own
        con = self._connect()
        try:
            yield from self._observations(con)
            yield from self._summaries(con)
        finally:
            con.close()

    def _observations(self, con):
        # merged_into_project is claude-mem's own project rename/merge — honor it
        q = (
            "SELECT id, title, narrative, text, facts, created_at, "
            "COALESCE(merged_into_project, project) FROM observations ORDER BY id"
        )
        for oid, title, narrative, text, facts, created, project in con.execute(q):
            content = _obs_content(title, narrative, text, facts)
            if not content:
                continue
            yield Record(
                uid=f"obs:{oid}",
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
            yield Record(
                uid=f"sum:{row[0]}",
                content=content,
                created_at=row[6],
                project=row[7],
            )

    def describe_selection(self):
        """Dry-run report lines: observation counts by type, plus summaries."""
        con = self._connect()
        try:
            counts = dict(
                con.execute(
                    "SELECT COALESCE(type, '(untyped)'), COUNT(*) "
                    "FROM observations GROUP BY 1"
                )
            )
            (n_sum,) = con.execute("SELECT COUNT(*) FROM session_summaries").fetchone()
        finally:
            con.close()
        by_type = ", ".join(f"{t} ({n})" for t, n in sorted(counts.items())) or "none"
        return [
            f"observation rows: {by_type} — rows with no usable content are dropped "
            "at planning",
            f"session summary rows: {n_sum}",
        ]
