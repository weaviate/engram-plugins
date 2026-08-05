"""Migration tests. Stdlib-only: a synthetic claude-mem DB stands in for the real store and
a fake client covers execution — the one test constructing real SDK input types skips when
the SDK isn't importable (run under the plugin venv for it).

    cd plugin && python3 -m unittest tests.test_migrate -v
"""

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from core.migrate import Record
from core.migrate.claude_mem import ClaudeMemSource
from core.migrate.engine import (
    KIND_TO_TOPIC,
    load_checkpoint,
    plan,
    plan_conversations,
    reconcile_pending,
    render_report,
    rollback,
    save_checkpoint,
)


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY, project TEXT, text TEXT, type TEXT, title TEXT,
            narrative TEXT, facts TEXT, created_at TEXT, merged_into_project TEXT);
        CREATE TABLE session_summaries (
            id INTEGER PRIMARY KEY, project TEXT, request TEXT, learned TEXT,
            completed TEXT, next_steps TEXT, notes TEXT, created_at TEXT,
            merged_into_project TEXT);
        """
    )
    con.executemany(
        "INSERT INTO observations (id, project, text, type, title, narrative, facts,"
        " created_at, merged_into_project) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "alpha", None, "decision", "Chose X", "Because Y.", None,
             "2026-05-06T05:25:22.932Z", None),
            # narrative missing → text column; merged project overrides project
            (2, "old-name", "Legacy body", "bugfix", "Fixed Z", None, None,
             "2026-06-01T00:00:00Z", "alpha"),
            # neither narrative nor text → facts fallback
            (3, "alpha", None, "feature", "Added W", None,
             json.dumps(["f1", "f2"]), "2026-07-01T00:00:00Z", None),
            # excluded by default
            (4, "alpha", None, "discovery", "Noticed V", "Detail.", None,
             "2026-07-02T00:00:00Z", None),
            # unknown type → never yielded
            (5, "alpha", None, "someday_new", "???", "Detail.", None,
             "2026-07-03T00:00:00Z", None),
        ],
    )
    con.execute(
        "INSERT INTO session_summaries (id, project, request, learned, completed,"
        " next_steps, notes, created_at, merged_into_project)"
        " VALUES (10, 'beta', 'Do a thing', 'Learned it', '', NULL, NULL,"
        " '2026-08-01T00:00:00Z', NULL)"
    )
    con.commit()
    con.close()


class ClaudeMemAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "claude-mem.db")
        make_db(self.db)
        self.source = ClaudeMemSource(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_curated_selection_and_composition(self):
        recs = {r.uid: r for r in self.source.records()}
        self.assertEqual(
            set(recs), {"obs:1", "obs:2", "obs:3", "sum:10"}
        )  # no discovery, no unknown type
        self.assertEqual(recs["obs:1"].kind, "architecture")
        self.assertEqual(recs["obs:1"].content, "Chose X — Because Y.")
        self.assertEqual(recs["obs:2"].kind, "task")
        self.assertEqual(recs["obs:2"].project, "alpha")  # merged_into_project wins
        self.assertEqual(recs["obs:2"].content, "Fixed Z — Legacy body")
        self.assertEqual(recs["obs:3"].content, "Added W — f1; f2")  # facts fallback
        self.assertEqual(recs["sum:10"].kind, "task")
        # empty/NULL summary fields are dropped, populated ones labeled
        self.assertEqual(
            recs["sum:10"].content,
            "Session summary —\nRequest: Do a thing\nLearned: Learned it",
        )

    def test_all_includes_low_signal_types(self):
        uids = {r.uid for r in self.source.records(include_all=True)}
        self.assertIn("obs:4", uids)
        self.assertNotIn("obs:5", uids)  # unknown types stay out even with --all

    def test_describe_selection(self):
        text = "\n".join(self.source.describe_selection())
        self.assertIn("discovery (1)", text)  # reported as excluded
        self.assertIn("someday_new (1)", text)  # reported as unknown
        self.assertIn("session summaries included: 1", text)

    def test_readonly(self):
        con = self.source._connect()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM observations")
        con.close()


def fake_resolver(mapping):
    return lambda project: mapping.get(project)


class EngineTest(unittest.TestCase):
    def recs(self):
        return [
            Record("a1", "architecture", "one", "2026-05-06T05:25:22Z", "alpha"),
            Record("a2", "task", "two", None, "alpha"),
            Record("b1", "task", "three", "2026-06-01T00:00:00Z", "beta"),
            Record("x1", "task", "lost", "2026-06-01T00:00:00Z", "unmapped"),
        ]

    def test_plan_groups_batches_and_skips(self):
        resolve = fake_resolver({"alpha": "org/alpha", "beta": "org/beta"})
        planned = plan(self.recs(), resolve, KIND_TO_TOPIC, batch_size=1)
        self.assertEqual(planned["items"], 3)
        self.assertEqual(planned["skipped"], {"unmapped": 1})
        self.assertEqual([r for r, _ in planned["batches"]],
                         ["org/alpha", "org/alpha", "org/beta"])  # batch_size=1 splits
        by_uid = {u: (t, c) for _, items in planned["batches"] for u, t, c in items}
        self.assertEqual(by_uid["a1"], ("DomainAndArchitecture", "[2026-05-06] one"))
        self.assertEqual(by_uid["a2"], ("TaskStatus", "two"))  # no date → no prefix
        report = render_report(planned, ["src line"], "hdr")
        self.assertIn("unmapped", report)
        self.assertIn("org/alpha", report)

    def test_plan_skip_uids(self):
        resolve = fake_resolver({"alpha": "org/alpha", "beta": "org/beta"})
        planned = plan(self.recs(), resolve, KIND_TO_TOPIC, 50, skip_uids={"a1", "b1"})
        self.assertEqual(planned["items"], 1)
        self.assertEqual(planned["already"], 2)

    def test_plan_conversations_orders_chronologically(self):
        recs = [
            Record("a1", "architecture", "one", "2026-05-06T10:00:00Z", "alpha"),
            Record("a2", "task", "two", "2026-05-06T05:00:00Z", "alpha"),
            Record("b1", "task", "three", "2026-04-01T00:00:00Z", "beta"),
            Record("x1", "task", "lost", "2026-06-01T00:00:00Z", "unmapped"),
        ]
        resolve = fake_resolver({"alpha": "org/alpha", "beta": "org/beta"})
        planned = plan_conversations(recs, resolve)
        # earliest day first; within a day, items sorted by timestamp
        self.assertEqual(
            [(repo, [u for u, _, _ in items]) for repo, items in planned["batches"]],
            [("org/beta", ["b1"]), ("org/alpha", ["a2", "a1"])],
        )
        # raw content, no [date] prefix — created_at carries the date in this mode
        self.assertEqual(planned["batches"][1][1][0], ("a2", "2026-05-06T05:00:00Z", "two"))
        self.assertEqual(planned["skipped"], {"unmapped": 1})

    def test_rollback_deletes_via_run_manifests(self):
        manifests = {
            "r1": ["m1", "m2"],
            "r2": ["m3"],
        }

        class Http404(Exception):
            status_code = 404

        deleted = []

        def delete(mid, user_id=None, group=None):
            if mid == "m2":
                raise Http404()  # already gone from an interrupted earlier rollback
            deleted.append(mid)

        client = SimpleNamespace(
            runs=SimpleNamespace(
                get=lambda rid: SimpleNamespace(
                    status="completed",
                    error=None,
                    committed_operations=SimpleNamespace(
                        created=[SimpleNamespace(memory_id=m) for m in manifests[rid]],
                        updated=[], deleted=[],
                    ),
                )
            ),
            memories=SimpleNamespace(delete=delete),
        )
        cp = {"done": {"a1": "r1", "a2": "r1"}, "pending": {"r2": ["b1"]}}
        n_deleted, gone, errors = rollback(cp, client, "u@x", log=lambda *_: None)
        self.assertEqual((n_deleted, gone, errors), (2, 1, []))
        self.assertEqual(sorted(deleted), ["m1", "m3"])

    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "cp.json")
            self.assertEqual(load_checkpoint(path), {"done": {}, "pending": {}})
            cp = {"done": {"a1": "r1"}, "pending": {"r2": ["a2"]}}
            save_checkpoint(path, cp)
            self.assertEqual(load_checkpoint(path), cp)

    def test_reconcile_pending(self):
        cp = {"done": {}, "pending": {"r1": ["a1"], "r2": ["a2"], "r3": ["a3"]}}
        statuses = {
            "r1": SimpleNamespace(status="completed", error=None),
            "r2": SimpleNamespace(status="failed", error="boom"),
            "r3": SimpleNamespace(status="running", error=None),
        }
        client = SimpleNamespace(runs=SimpleNamespace(get=lambda rid: statuses[rid]))
        reconcile_pending(cp, client, lambda *_: None)
        self.assertEqual(cp["done"], {"a1": "r1"})
        self.assertEqual(cp["pending"], {"r3": ["a3"]})  # running stays reserved


@unittest.skipUnless(importlib.util.find_spec("engram"), "Engram SDK not installed")
class ExecuteTest(unittest.TestCase):
    def test_execute_commits_and_checkpoints(self):
        from core.migrate.engine import execute

        calls = []

        def add(inp, user_id=None, properties=None):
            calls.append((len(inp.items), user_id, properties))
            return SimpleNamespace(run_id=f"r{len(calls)}")

        client = SimpleNamespace(
            memories=SimpleNamespace(add=add),
            runs=SimpleNamespace(
                wait=lambda rid, timeout, interval: SimpleNamespace(
                    status="completed", error=None
                )
            ),
        )
        planned = {
            "batches": [
                ("org/alpha", [("a1", "TaskStatus", "one"), ("a2", "TaskStatus", "two")]),
                ("org/beta", [("b1", "Processes", "three")]),
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            cp_path = os.path.join(tmp, "cp.json")
            cp = {"done": {}, "pending": {}}
            result = execute(planned, client, "u@x", cp, cp_path, log=lambda *_: None,
                             extra_properties={"session_id": "migration:claude-mem"})
            self.assertEqual(result, {"committed": 2, "failed": [], "pending": 0})
            self.assertEqual(
                calls,
                [(2, "u@x", {"repo_name": "org/alpha", "session_id": "migration:claude-mem"}),
                 (1, "u@x", {"repo_name": "org/beta", "session_id": "migration:claude-mem"})],
            )
            self.assertEqual(load_checkpoint(cp_path)["done"],
                             {"a1": "r1", "a2": "r1", "b1": "r2"})

    def test_execute_conversation_builds_input_and_preserves_order(self):
        from engram import ConversationInput

        from core.migrate.engine import execute

        submitted = []

        def add(inp, user_id=None, properties=None):
            submitted.append(inp)
            return SimpleNamespace(run_id=f"r{len(submitted)}")

        # first run never finishes → conversation mode must abort, not skip ahead
        client = SimpleNamespace(
            memories=SimpleNamespace(add=add),
            runs=SimpleNamespace(
                wait=lambda rid, timeout, interval: SimpleNamespace(
                    status="running", error=None
                )
            ),
        )
        planned = {
            "batches": [
                ("org/a", [("a2", "2026-05-06T05:00:00Z", "two"),
                           ("a1", "2026-05-06T10:00:00Z", "one")]),
                ("org/b", [("b1", "2026-06-01T00:00:00Z", "three")]),
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            cp = {"done": {}, "pending": {}}
            result = execute(planned, client, "u@x", cp, os.path.join(tmp, "cp.json"),
                             wait_timeout=1, log=lambda *_: None, mode="conversation")
        self.assertEqual(len(submitted), 1)  # aborted after the unfinished first run
        self.assertEqual(result["pending"], 1)
        inp = submitted[0]
        self.assertIsInstance(inp, ConversationInput)
        self.assertEqual(inp.created_at, "2026-05-06T05:00:00Z")
        self.assertEqual(inp.messages[0].role, "system")
        self.assertEqual([m.content for m in inp.messages[1:]], ["two", "one"])
        self.assertEqual(inp.messages[1].created_at, "2026-05-06T05:00:00Z")
        self.assertEqual(cp["pending"], {"r1": ["a2", "a1"]})


if __name__ == "__main__":
    unittest.main()
