"""CLI: python -m core.migrate [flags]. Dry-run by default — --execute writes.

Run via bin/engram-migrate (the migrate-memories skill's scripts/migrate.sh delegates
there too), which sets up the plugin venv and data dir.

Exit codes: 0 done, 1 failure (failed submissions/runs, or a usage mistake this CLI
detects itself), 2 usage error caught by argparse, 3 incomplete — runs still in the
pipeline, re-run to reconcile."""

import argparse
import os
import sys

from . import sources
from .claude_projects import index_by_basename
from .engine import (
    checkpoint_path,
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

# how long --execute waits for the pipeline to settle after everything is submitted;
# leftover runs stay in the checkpoint and reconcile on the next invocation
SETTLE_TIMEOUT = 600


def _positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def _parse_kv(pairs, flag):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"--{flag} expects NAME=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        # an empty half silently doing nothing (e.g. --map proj= falling through to the
        # dir probe) is worse than an error
        if not k.strip() or not v.strip():
            sys.exit(f"--{flag} {p!r}: name and value must be non-empty")
        out[k.strip()] = v.strip()
    return out


def _schema(execute_mode):
    """The cached/fetched group schema — needed only to learn which scope properties the
    group requires (topics are Engram's own concern: extraction classifies memories, so no
    topic validation happens here). Dry-run degrades to a warning without a schema;
    --execute refuses to run — sending batches with the wrong property setup would just
    fail one by one server-side."""
    try:
        from ..scope import scope_schema

        return scope_schema()
    except Exception as e:
        if execute_mode:
            sys.exit(
                f"cannot read the group schema ({e}) — refusing to migrate without "
                "knowing the group's required scope properties; retry when the API is "
                "reachable"
            )
        print(f"note: couldn't read the group schema ({e}); scope properties not validated")
        return None


def _props_builder(schema, source_name, find_dir, map_overrides, extra):
    """Per-project scope properties, resolved the way the store hook resolves them:
    core.scope.resolve_scope on the project's directory, honoring the same config files
    (~/.engram/config.json, per-dir .engram.json) and source cascades — including the
    repo_name git-repo → cwd fallback, so a dir without a remote files under its path
    exactly like realtime adds do. The migration marker is passed as the session_id.
    --map forces repo_name; --property merges last. Returns None when the group's
    required properties can't be satisfied for a project — those records are skipped
    rather than mis-scoped."""
    from ..scope import resolve_scope

    required = (schema or {}).get("properties", [])
    marker = f"migration:{source_name}"

    def props_for(project):
        mapped = map_overrides.get(project)
        d = find_dir(project) if project else None
        if d:
            try:
                props, _user_required, _unresolved = resolve_scope(d, marker)
            except Exception as e:
                # one project's broken .engram.json, or an unreachable schema on an
                # uncached dry-run, must not abort the whole migration — the project is
                # reported as skipped instead (called once per project: planner memoizes)
                print(f"note: {project}: scope resolution failed ({e}) — skipped")
                return None
        else:
            # no directory to resolve from: only the session marker (when the group uses
            # it) and explicit overrides are trustworthy
            props = {p: marker for p in required if p == "session_id"}
        if mapped:
            props["repo_name"] = mapped
        props.update(extra)
        if [p for p in required if p not in props]:
            return None
        return props

    return props_for


def _load_checkpoint_or_exit(path):
    try:
        return load_checkpoint(path)
    except ValueError as e:
        sys.exit(str(e))


def _source_lines(adapter):
    fn = getattr(adapter, "describe_selection", None)  # optional in the adapter contract
    return fn() if fn else []


def _rollback(source_name):
    from ..client import get_client, get_user_id

    client = get_client()
    if client is None:
        sys.exit("ENGRAM_API_KEY not set — cannot roll back.")
    user_id = get_user_id()
    if not user_id:
        sys.exit("no stable identity — set git user.email or ENGRAM_USER_ID first.")
    cp_path = checkpoint_path(source_name)
    cp = _load_checkpoint_or_exit(cp_path)
    recorded = cp.get("user_id")
    if recorded and recorded != user_id:
        # deleting as the wrong identity 404s on every memory; clearing the checkpoint on
        # that would orphan them all
        sys.exit(
            f"the checkpoint was written as {recorded!r} but the current identity is "
            f"{user_id!r} — set ENGRAM_USER_ID={recorded} to roll back"
        )
    runs = set(cp["done"].values()) | set(cp["pending"])
    if not runs:
        print("Nothing to roll back — the checkpoint records no migration runs.")
        return 0
    print(f"Rolling back {len(runs)} migration runs "
          f"({len(cp['done'])} migrated items) …", flush=True)

    def progress(*a):
        print(*a, flush=True)

    deleted, gone, errors = rollback(cp, client, user_id, log=progress)
    if errors:
        # keep the checkpoint: unreachable manifests, in-flight runs, and failed deletes
        # all mean memories may survive, and clearing would leave no record to retry from
        print(f"\nDeleted {deleted} memories ({gone} already gone); "
              f"{len(errors)} runs not fully rolled back — checkpoint kept, re-run "
              "--rollback to retry.")
        return 1
    if deleted == 0 and gone > 0 and not recorded:
        # only an unstamped (legacy) checkpoint leaves the all-gone case ambiguous: with
        # a matching recorded identity (checked above) all-gone means an earlier rollback
        # already deleted everything, so falling through to the reset is safe — without
        # the stamp it could equally be an identity mismatch, so keep the record
        print(f"\nEvery delete reported the memory as already gone ({gone}). That can "
              "mean an earlier rollback finished — or an identity mismatch (this "
              "checkpoint predates identity stamping, so it cannot be verified). "
              f"Checkpoint kept; delete {cp_path} yourself once you have confirmed the "
              "memories are really gone.")
        return 1
    save_checkpoint(cp_path, {"done": {}, "pending": {}})
    print(f"\nDeleted {deleted} memories ({gone} already gone). Checkpoint reset — "
          "a new migration will start from scratch.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="engram-migrate",
        description="Migrate memories from another local memory system into Engram. "
                    "Memories go through Engram's extraction pipeline, which classifies "
                    "them into your group's topics itself.",
    )
    ap.add_argument("--source", default="claude-mem", choices=sorted(sources()))
    ap.add_argument("--db", help="override the source's default store location")
    ap.add_argument("--project", action="append", help="migrate only this source project (repeatable)")
    ap.add_argument("--map", action="append", metavar="NAME=owner/repo",
                    help="repo_name for a project whose directory can't be found (repeatable)")
    ap.add_argument("--property", action="append", dest="properties", metavar="KEY=VALUE",
                    help="extra scope property attached to every batch (repeatable)")
    ap.add_argument("--repos-dir", action="append",
                    help="extra dir to probe for project repos (default: cwd and its parent)")
    ap.add_argument("--limit", type=_positive,
                    help="cap the number of not-yet-migrated records (smoke runs)")
    ap.add_argument("--execute", action="store_true",
                    help="actually migrate (default is a dry-run report)")
    ap.add_argument("--rollback", action="store_true",
                    help="delete every memory this migration created (via run manifests) "
                         "and reset the checkpoint")
    ap.add_argument("--detect", action="store_true",
                    help="list registered sources and whether their store is present, "
                         "then exit")
    args = ap.parse_args()
    if args.detect:
        for name, cls in sorted(sources().items()):
            found = cls().available()
            print(f"{name}: {found or 'not found'}")
        return 0
    if args.rollback and args.execute:
        sys.exit("--rollback and --execute are mutually exclusive")

    if args.rollback:
        return _rollback(args.source)

    adapter = sources()[args.source](args.db)
    found = adapter.available()
    if not found:
        sys.exit(f"{args.source}: no store found at {adapter.db_path} (override with --db)")

    cp_path = checkpoint_path(args.source)
    cp = _load_checkpoint_or_exit(cp_path)
    skip = set(cp["done"]) | {u for uids in cp["pending"].values() for u in uids}

    records = adapter.records()
    if args.project:
        wanted = set(args.project)
        records = (r for r in records if r.project in wanted)
    records = list(records)
    if args.limit:
        # limit counts fresh work: slicing before the skip filter would re-select
        # already-migrated records on a resumed store and migrate nothing
        records = [r for r in records if r.uid not in skip][: args.limit]

    extra = _parse_kv(args.properties, "property")
    if "repo_name" in extra:
        # resolved and attached per project; a global override would file the whole
        # store under one repo
        sys.exit("repo_name is resolved per project — use --map NAME=owner/repo to change it")
    cwd = os.getcwd()
    find_dir = project_dir_finder(
        explicit_dirs=[os.path.expanduser(d) for d in (args.repos_dir or [])],
        registry_index=index_by_basename(),
        fallback_dirs=[os.path.dirname(cwd) or cwd, cwd],
    )
    schema = _schema(args.execute)
    props_for = _props_builder(schema, args.source, find_dir, _parse_kv(args.map, "map"), extra)

    if not args.execute:
        planned = plan_conversations(records, props_for, skip_uids=skip)
        header = f"Engram migration (dry run) — source: {args.source} ({found})"
        print(render_report(planned, _source_lines(adapter), header))
        print("\nDry run — nothing written. Add --execute to migrate.")
        return 0

    from ..client import get_client, get_user_id

    client = get_client()
    if client is None:
        sys.exit("ENGRAM_API_KEY not set — cannot migrate.")
    user_id = get_user_id()
    if not user_id:
        sys.exit("no stable identity — set git user.email or ENGRAM_USER_ID first.")

    def progress(*a):
        # a migration is watched via tee/pipes where python block-buffers stdout — flush
        # per line so progress is visible live
        print(*a, flush=True)

    pre_done, pre_failed = reconcile_pending(cp, client, progress)
    # stamp the checkpoint with what this migration ran against, so a later --db switch is
    # visible and --rollback can detect an identity change
    prev_db = cp.get("source_db")
    if prev_db and prev_db != found and (cp["done"] or cp["pending"]):
        print(f"note: checkpoint was built from {prev_db}, now migrating {found} — "
              "shared uids are skipped; --rollback to start over if that is not intended")
    cp["source_db"] = found
    cp["user_id"] = user_id
    save_checkpoint(cp_path, cp)
    skip = set(cp["done"]) | {u for uids in cp["pending"].values() for u in uids}
    planned = plan_conversations(records, props_for, skip_uids=skip)
    header = f"Engram migration — source: {args.source} ({found})"
    print(render_report(planned, _source_lines(adapter), header))
    if not planned["batches"] and not cp["pending"]:
        print("\nNothing to migrate.")
        return 0

    print()
    result = execute(planned, client, user_id, cp, cp_path, log=progress)
    if cp["pending"]:
        progress(f"\n{result['submitted']} conversation(s) submitted — waiting for the "
                 "pipeline to settle …")
    committed, failed_runs = settle(cp, client, cp_path, SETTLE_TIMEOUT, log=progress)
    # runs reconciled at startup (left over from a previous invocation) belong in the
    # totals too — without them a resumed run under-reports what actually happened
    committed += pre_done
    failures = len(result["failed"]) + len(failed_runs) + len(pre_failed)
    print(
        f"\nDone: {result['submitted']} conversations submitted, {committed} committed, "
        f"{failures} failed, {len(cp['pending'])} still in the pipeline "
        f"(checkpoint: {cp_path})"
    )
    if failures:
        return 1
    return 3 if cp["pending"] else 0


if __name__ == "__main__":
    sys.exit(main())
