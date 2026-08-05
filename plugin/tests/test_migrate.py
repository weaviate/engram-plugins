"""Migration tests. A synthetic claude-mem DB stands in for the real store and a fake
client covers execution. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_migrate -v
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace

from core.migrate import Record, sources
from core.migrate.claude_mem import ClaudeMemSource
from core.migrate.claude_memory import ClaudeMemorySource, decode_project_dir
from core.migrate.claude_projects import index_by_basename
from core.migrate.engine import (
    execute,
    load_checkpoint,
    plan_conversations,
    project_dir_finder,
    reconcile_pending,
    render_report,
    rollback,
    save_checkpoint,
    settle,
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
            # a low-signal type: migrates like everything else, extraction decides
            (4, "alpha", None, "discovery", "Noticed V", "Detail.", None,
             "2026-07-02T00:00:00Z", None),
            # a type this adapter has never heard of: migrates all the same
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

    def test_selection_and_composition(self):
        recs = {r.uid: r for r in self.source.records()}
        # every observation migrates, discovery and unknown types included — Engram's
        # extraction decides what to keep
        self.assertEqual(
            set(recs), {"obs:1", "obs:2", "obs:3", "obs:4", "obs:5", "sum:10"}
        )
        self.assertEqual(recs["obs:1"].content, "Chose X — Because Y.")
        self.assertEqual(recs["obs:2"].project, "alpha")  # merged_into_project wins
        self.assertEqual(recs["obs:2"].content, "Fixed Z — Legacy body")
        self.assertEqual(recs["obs:3"].content, "Added W — f1; f2")  # facts fallback
        # empty/NULL summary fields are dropped, populated ones labeled
        self.assertEqual(
            recs["sum:10"].content,
            "Session summary —\nRequest: Do a thing\nLearned: Learned it",
        )

    def test_describe_selection(self):
        text = "\n".join(self.source.describe_selection())
        self.assertIn("discovery (1)", text)
        self.assertIn("someday_new (1)", text)
        self.assertIn("session summary rows: 1", text)

    def test_readonly(self):
        con = self.source._connect()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM observations")
        con.close()

    def test_readonly_survives_uri_reserved_path_chars(self):
        # a '?' in the filename must not smuggle URI params past mode=ro
        tricky = os.path.join(self.tmp.name, "mem?mode=rw#x.db")
        make_db(tricky)
        source = ClaudeMemSource(tricky)
        self.assertTrue(any(source.records()))
        con = source._connect()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM observations")
        con.close()


def fake_props(mapping):
    """props_for stub: mapping names a repo_name per project; missing → None (skip)."""
    return lambda project: (
        {"repo_name": mapping[project], "session_id": "migration:test"}
        if project in mapping
        else None
    )


MEMORY_FILE = """---
name: prefers-uv
description: The user prefers uv for dependency management
metadata:
  type: user
---

Use uv, not pip, when adding dependencies.
"""


class ClaudeMemoryAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a real project dir whose munged name the adapter must decode back
        self.proj = os.path.join(self.tmp.name, "my.repo")
        os.makedirs(self.proj)
        munged = self.proj.replace("/", "-").replace(".", "-")
        self.memdir = os.path.join(self.tmp.name, "projects", munged, "memory")
        os.makedirs(self.memdir)
        with open(os.path.join(self.memdir, "MEMORY.md"), "w") as f:
            f.write("- index entry, not a fact")
        with open(os.path.join(self.memdir, "prefers-uv.md"), "w") as f:
            f.write(MEMORY_FILE)
        with open(os.path.join(self.memdir, "bare.md"), "w") as f:
            f.write("A fact with no frontmatter at all.")
        self.source = ClaudeMemorySource(os.path.join(self.tmp.name, "projects"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_decode_project_dir_roundtrip(self):
        munged = self.proj.replace("/", "-").replace(".", "-")
        self.assertIn(self.proj, decode_project_dir(munged))

    def test_decode_deep_kebab_name_stays_fast(self):
        # regression guard: the unpruned Θ(2ⁿ) search hung on ~25 dashes
        name = "-Users-nobody-" + "-".join(["word"] * 40)
        start = time.time()
        self.assertEqual(decode_project_dir(name), [])
        self.assertLess(time.time() - start, 2.0)

    def test_records_skip_index_and_decode_project(self):
        recs = {os.path.basename(r.uid.split("@")[0]): r for r in self.source.records()}
        self.assertEqual(set(recs), {"prefers-uv.md", "bare.md"})  # MEMORY.md skipped
        pref = recs["prefers-uv.md"]
        self.assertEqual(
            pref.content,
            "The user prefers uv for dependency management — "
            "Use uv, not pip, when adding dependencies.",
        )
        self.assertEqual(pref.project, self.proj)  # munged name decoded to the real path
        self.assertRegex(pref.created_at, r"^\d{4}-\d{2}-\d{2}T")  # file mtime
        self.assertEqual(recs["bare.md"].content, "A fact with no frontmatter at all.")

    def test_uid_changes_when_content_changes(self):
        (before,) = [r.uid for r in self.source.records() if "prefers-uv" in r.uid]
        with open(os.path.join(self.memdir, "prefers-uv.md"), "a") as f:
            f.write("\nAlso: never use pipenv.")
        (after,) = [r.uid for r in self.source.records() if "prefers-uv" in r.uid]
        self.assertNotEqual(before, after)  # edited fact re-migrates under a fresh uid

    def test_available_and_registry(self):
        self.assertTrue(self.source.available())
        self.assertIsNone(ClaudeMemorySource(os.path.join(self.tmp.name, "nope")).available())
        self.assertIn("claude-memory", sources())

    def test_describe_selection_counts_types(self):
        text = "\n".join(self.source.describe_selection())
        self.assertIn("user (1)", text)
        self.assertIn("(untyped) (1)", text)


class ProjectDirFinderTest(unittest.TestCase):
    def test_absolute_project_checked_directly(self):
        from core.migrate.engine import project_dir_finder

        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "proj")
            os.makedirs(proj)
            find = project_dir_finder([])  # no repos dirs needed for absolute paths
            self.assertEqual(find(proj), proj)
            self.assertIsNone(find(os.path.join(tmp, "missing")))

    def test_bare_name_probed_against_repos_dirs(self):
        from core.migrate.engine import project_dir_finder

        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "proj")
            os.makedirs(proj)
            find = project_dir_finder([tmp])
            self.assertEqual(find("proj"), proj)
            # the repos dir itself matches when its basename is the project name
            self.assertEqual(project_dir_finder([proj])("proj"), proj)


class EngineTest(unittest.TestCase):
    def recs(self):
        return [
            Record("a1", "one", "2026-05-06T10:00:00Z", "alpha"),
            Record("a2", "two", "2026-05-06T05:00:00Z", "alpha"),
            Record("b1", "three", "2026-04-01T00:00:00Z", "beta"),
            Record("x1", "lost", "2026-06-01T00:00:00Z", "unmapped"),
        ]

    def test_plan_orders_chronologically_and_skips_unresolvable(self):
        props_for = fake_props({"alpha": "org/alpha", "beta": "org/beta"})
        planned = plan_conversations(self.recs(), props_for)
        # earliest day first; within a day, items sorted by timestamp
        self.assertEqual(
            [(label, [u for u, _, _ in items]) for label, _, items in planned["batches"]],
            [("org/beta", ["b1"]), ("org/alpha", ["a2", "a1"])],
        )
        self.assertEqual(planned["batches"][1][2][0], ("a2", "2026-05-06T05:00:00Z", "two"))
        # each batch carries the project's resolved scope properties
        self.assertEqual(planned["batches"][0][1],
                         {"repo_name": "org/beta", "session_id": "migration:test"})
        self.assertEqual(planned["skipped"], {"unmapped": 1})
        report = render_report(planned, ["src line"], "hdr")
        self.assertIn("unmapped", report)
        self.assertIn("org/alpha", report)

    def test_plan_labels_by_project_when_no_repo_property(self):
        # a group without a repo_name property: props resolve without one, nothing skips,
        # and the batch label falls back to the project name
        planned = plan_conversations(
            self.recs(), lambda project: {"session_id": "migration:test"}
        )
        self.assertEqual(planned["items"], 4)
        self.assertEqual(planned["skipped"], {})
        self.assertIn(
            ("unmapped", {"session_id": "migration:test"},
             [("x1", "2026-06-01T00:00:00Z", "lost")]),
            planned["batches"],
        )

    def test_plan_skip_uids(self):
        props_for = fake_props({"alpha": "org/alpha", "beta": "org/beta"})
        planned = plan_conversations(self.recs(), props_for, skip_uids={"a1", "b1"})
        self.assertEqual(planned["items"], 1)
        self.assertEqual(planned["already"], 2)

    def test_plan_excludes_undated(self):
        recs = [
            Record("a1", "dated", "2026-05-06T10:00:00Z", "alpha"),
            Record("a2", "no date", None, "alpha"),
            Record("a3", "garbage date", "not-a-ts", "alpha"),
        ]
        planned = plan_conversations(recs, fake_props({"alpha": "org/alpha"}))
        self.assertEqual(planned["items"], 1)
        self.assertEqual(planned["undated"], 2)
        self.assertEqual(planned["project_counts"], {"alpha": 1})
        self.assertIn("undated records excluded: 2", render_report(planned, [], "h"))

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
        done, failed = reconcile_pending(cp, client, lambda *_: None)
        self.assertEqual((done, failed), (1, ["r2"]))
        self.assertEqual(cp["done"], {"a1": "r1"})
        self.assertEqual(cp["pending"], {"r3": ["a3"]})  # running stays reserved


class ExecuteTest(unittest.TestCase):
    def _client(self, calls):
        def add(inp, user_id=None, properties=None):
            calls.append((len(inp.messages) - 1, user_id, properties))  # minus system msg
            return SimpleNamespace(run_id=f"r{len(calls)}")

        # no runs attribute: execute must not wait on the pipeline (Engram queues
        # internally); settle() is the only status reader
        return SimpleNamespace(memories=SimpleNamespace(add=add))

    def _planned(self):
        props = {"session_id": "migration:claude-mem"}
        return {
            "batches": [
                ("org/alpha", {"repo_name": "org/alpha", **props},
                 [("a1", "2026-05-06T05:00:00Z", "one"),
                  ("a2", "2026-05-06T10:00:00Z", "two")]),
                ("org/beta", {"repo_name": "org/beta", **props},
                 [("b1", "2026-06-01T00:00:00Z", "three")]),
            ]
        }

    def test_execute_submits_all_without_waiting(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cp_path = os.path.join(tmp, "cp.json")
            cp = {"done": {}, "pending": {}}
            result = execute(self._planned(), self._client(calls), "u@x", cp, cp_path,
                             log=lambda *_: None)
            self.assertEqual(result, {"submitted": 2, "failed": []})
            self.assertEqual(
                calls,
                [(2, "u@x", {"repo_name": "org/alpha", "session_id": "migration:claude-mem"}),
                 (1, "u@x", {"repo_name": "org/beta", "session_id": "migration:claude-mem"})],
            )
            # every run is pending until settle() confirms it
            self.assertEqual(load_checkpoint(cp_path)["pending"],
                             {"r1": ["a1", "a2"], "r2": ["b1"]})

    def test_execute_sends_no_properties_when_none_resolved(self):
        calls = []
        planned = {"batches": [("proj", {}, [("a1", "2026-05-06T05:00:00Z", "one")])]}
        with tempfile.TemporaryDirectory() as tmp:
            cp = {"done": {}, "pending": {}}
            execute(planned, self._client(calls), "u@x", cp,
                    os.path.join(tmp, "cp.json"), log=lambda *_: None)
        self.assertEqual([props for _, _, props in calls], [None])

    def test_execute_stops_on_submit_error(self):
        def add(inp, user_id=None, properties=None):
            raise RuntimeError("missing required property")

        client = SimpleNamespace(memories=SimpleNamespace(add=add))
        with tempfile.TemporaryDirectory() as tmp:
            cp = {"done": {}, "pending": {}}
            result = execute(self._planned(), client, "u@x", cp,
                             os.path.join(tmp, "cp.json"), log=lambda *_: None)
        # the first failed submit stops the loop: later days must not overtake it
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(cp["pending"], {})

    def test_execute_builds_conversation_input(self):
        from engram import ConversationInput

        submitted = []

        def add(inp, user_id=None, properties=None):
            submitted.append(inp)
            return SimpleNamespace(run_id=f"r{len(submitted)}")

        client = SimpleNamespace(memories=SimpleNamespace(add=add))
        planned = {
            "batches": [
                ("org/a", {"repo_name": "org/a"},
                 [("a2", "2026-05-06T05:00:00Z", "two"),
                  ("a1", "2026-05-06T10:00:00Z", "one")]),
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            execute(planned, client, "u@x", {"done": {}, "pending": {}},
                    os.path.join(tmp, "cp.json"), log=lambda *_: None)
        inp = submitted[0]
        self.assertIsInstance(inp, ConversationInput)
        self.assertEqual(inp.created_at, "2026-05-06T05:00:00Z")
        self.assertEqual(inp.messages[0].role, "system")
        self.assertEqual([m.content for m in inp.messages[1:]], ["two", "one"])
        self.assertEqual(inp.messages[1].created_at, "2026-05-06T05:00:00Z")

    def test_settle_confirms_and_releases(self):
        statuses = {
            "r1": SimpleNamespace(status="completed", error=None),
            "r2": SimpleNamespace(status="failed", error="boom"),
        }
        client = SimpleNamespace(runs=SimpleNamespace(get=lambda rid: statuses[rid]))
        cp = {"done": {}, "pending": {"r1": ["a1", "a2"], "r2": ["b1"]}}
        with tempfile.TemporaryDirectory() as tmp:
            committed, failed = settle(cp, client, os.path.join(tmp, "cp.json"),
                                       timeout=5, interval=0, log=lambda *_: None)
        self.assertEqual((committed, failed), (1, ["r2"]))
        self.assertEqual(cp["done"], {"a1": "r1", "a2": "r1"})
        self.assertEqual(cp["pending"], {})  # failed run released for resubmission


class ProjectDiscoveryTest(unittest.TestCase):
    def _registry(self, root, *paths):
        reg = os.path.join(root, "registry")
        os.makedirs(reg, exist_ok=True)
        for path in paths:
            os.makedirs(path, exist_ok=True)
            os.makedirs(os.path.join(reg, path.replace("/", "-").replace(".", "-")),
                        exist_ok=True)
        return reg

    def test_registry_index_decodes_any_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, "work", "clients", "acme.io", "api")
            reg = self._registry(tmp, deep)
            index = index_by_basename(reg)
            self.assertEqual(index["api"], [deep])

    def test_finder_layers_explicit_then_registry_then_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = os.path.join(tmp, "explicit", "proj")
            registry_home = os.path.join(tmp, "elsewhere", "proj")
            fallback = os.path.join(tmp, "fallback", "proj")
            for d in (explicit, registry_home, fallback):
                os.makedirs(d)
            index = {"proj": [registry_home]}
            # explicit dirs win over the registry
            find = project_dir_finder([os.path.dirname(explicit)], index,
                                      [os.path.dirname(fallback)])
            self.assertEqual(find("proj"), explicit)
            # registry wins over fallback dirs
            find = project_dir_finder([], index, [os.path.dirname(fallback)])
            self.assertEqual(find("proj"), registry_home)
            # fallback used when the registry has no entry
            find = project_dir_finder([], {}, [os.path.dirname(fallback)])
            self.assertEqual(find("proj"), fallback)
            self.assertIsNone(project_dir_finder([], {}, [])("proj"))

    def test_registry_candidates_prefer_git_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "a", "proj")
            remoted = os.path.join(tmp, "b", "proj")
            os.makedirs(plain)
            os.makedirs(remoted)
            subprocess.run(["git", "init", "-q", remoted], check=True)
            subprocess.run(["git", "-C", remoted, "remote", "add", "origin",
                            "git@github.com:acme/proj.git"], check=True)
            find = project_dir_finder([], {"proj": [plain, remoted]}, [])
            self.assertEqual(find("proj"), remoted)


class PropsBuilderTest(unittest.TestCase):
    SCHEMA = {"properties": ["repo_name", "session_id"]}

    def test_resolution_error_skips_project_instead_of_crashing(self):
        def broken(cwd, marker):
            raise ValueError("invalid JSON in .engram.json")

        with unittest.mock.patch("core.scope.resolve_scope", broken):
            from core.migrate.__main__ import _props_builder

            props_for = _props_builder(self.SCHEMA, "claude-mem",
                                       lambda p: "/some/dir", {}, {})
            self.assertIsNone(props_for("proj"))

    def test_resolved_props_flow_through_with_map_and_extra(self):
        def ok(cwd, marker):
            return {"repo_name": "acme/from-git", "session_id": marker}, True, []

        with unittest.mock.patch("core.scope.resolve_scope", ok):
            from core.migrate.__main__ import _props_builder

            props_for = _props_builder(self.SCHEMA, "claude-mem",
                                       lambda p: "/some/dir",
                                       {"proj": "acme/mapped"}, {"team": "payments"})
            self.assertEqual(props_for("proj"),
                             {"repo_name": "acme/mapped",
                              "session_id": "migration:claude-mem",
                              "team": "payments"})

    def test_dirless_project_fills_marker_or_skips(self):
        from core.migrate.__main__ import _props_builder

        def no_dir(project):
            return None

        # repo_name required and unmappable → skip
        self.assertIsNone(
            _props_builder(self.SCHEMA, "claude-mem", no_dir, {}, {})("proj"))
        # only session_id required → marker fills it
        self.assertEqual(
            _props_builder({"properties": ["session_id"]}, "claude-mem",
                           no_dir, {}, {})("proj"),
            {"session_id": "migration:claude-mem"})


class SettleTimeoutTest(unittest.TestCase):
    def test_settle_returns_with_pending_on_timeout(self):
        client = SimpleNamespace(runs=SimpleNamespace(
            get=lambda rid: SimpleNamespace(status="running", error=None)))
        cp = {"done": {}, "pending": {"r1": ["a1"]}}
        with tempfile.TemporaryDirectory() as tmp:
            committed, failed = settle(cp, client, os.path.join(tmp, "cp.json"),
                                       timeout=0.01, interval=0, log=lambda *_: None)
        self.assertEqual((committed, failed), (0, []))
        self.assertEqual(cp["pending"], {"r1": ["a1"]})  # reserved for the next run



if __name__ == "__main__":
    unittest.main()
