"""CLI: python -m core.migrate [flags]. Dry-run by default — --execute writes.

Run via bin/engram-migrate (or the /engram:migrate command), which sets up the plugin venv
and data dir. Dry-run needs neither credentials nor the SDK, so the migration can be
inspected before anything leaves the machine."""

import argparse
import os
import sys

from . import KINDS, sources
from .engine import (
    KIND_TO_TOPIC,
    checkpoint_path,
    execute,
    load_checkpoint,
    plan,
    plan_conversations,
    reconcile_pending,
    render_report,
    repo_resolver,
    rollback,
    save_checkpoint,
)


def _parse_kv(pairs, flag):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"--{flag} expects NAME=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _schema():
    """The cached/fetched group schema, or None. Best-effort: with no cached schema and no
    API access (e.g. plain dry-run) validation degrades to a warning."""
    try:
        from ..scope import scope_schema

        return scope_schema()
    except Exception as e:
        print(f"note: couldn't read the group schema ({e}); topics and scope not validated")
        return None


def _topic_map(overrides, schema):
    """Default mapping + --topic-map overrides, validated against the live group schema so a
    typo'd or missing topic fails before any batch is sent."""
    unknown = sorted(set(overrides) - set(KINDS))
    if unknown:
        sys.exit(f"--topic-map kinds {unknown} not in {list(KINDS)}")
    mapping = {**KIND_TO_TOPIC, **overrides}
    topics = [
        t.get("topic_name")
        for t in (schema or {}).get("raw", {}).get("topics", [])
        if t.get("topic_name")
    ]
    missing = sorted(set(mapping.values()) - set(topics))
    if topics and missing:
        sys.exit(
            f"topics {missing} don't exist in your Engram group (available: {topics}). "
            "Remap with --topic-map kind=Topic."
        )
    return mapping


def _batch_properties(schema, source_name, overrides):
    """Scope properties attached to every batch besides repo_name — the server rejects a
    write missing any property the group requires. session_id is auto-filled with a
    migration marker: a migrated memory has no live session, and recall doesn't filter on
    it. Any other required property must be given explicitly (--property KEY=VALUE) —
    inventing a value would file memories under a scope recall filters would never match."""
    props = {}
    required = (schema or {}).get("properties", [])
    if "session_id" in required:
        props["session_id"] = f"migration:{source_name}"
    props.update(overrides)
    missing = [p for p in required if p not in props and p != "repo_name"]
    if missing:
        sys.exit(
            f"your Engram group requires scope properties {missing} — "
            "provide them with --property KEY=VALUE"
        )
    return props


def _rollback(source_name):
    from ..client import get_client, get_user_id

    client = get_client()
    if client is None:
        sys.exit("ENGRAM_API_KEY not set — cannot roll back.")
    cp_path = checkpoint_path(source_name)
    cp = load_checkpoint(cp_path)
    runs = set(cp["done"].values()) | set(cp["pending"])
    if not runs:
        print("Nothing to roll back — the checkpoint records no migration runs.")
        return 0
    print(f"Rolling back {len(runs)} migration runs "
          f"({len(cp['done'])} migrated items) …", flush=True)

    def progress(*a):
        print(*a, flush=True)

    deleted, gone, fetch_errors = rollback(cp, client, get_user_id(), log=progress)
    if fetch_errors:
        # keep the checkpoint: unreachable manifests mean undeleted memories, and clearing
        # would orphan them with no record left to retry from
        print(f"\nDeleted {deleted} memories ({gone} already gone); "
              f"{len(fetch_errors)} run manifests unreachable — checkpoint kept, re-run "
              "--rollback to retry.")
        return 1
    save_checkpoint(cp_path, {"done": {}, "pending": {}})
    print(f"\nDeleted {deleted} memories ({gone} already gone). Checkpoint reset — "
          "a new migration will start from scratch.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="engram-migrate",
        description="Migrate memories from another local memory system into Engram.",
    )
    ap.add_argument("--source", default="claude-mem", choices=sorted(sources()))
    ap.add_argument("--db", help="override the source's default store location")
    ap.add_argument("--all", action="store_true", help="include low-signal record types")
    ap.add_argument("--project", action="append", help="migrate only this source project (repeatable)")
    ap.add_argument("--map", action="append", metavar="NAME=owner/repo",
                    help="repo for a project the git probe can't resolve (repeatable)")
    ap.add_argument("--topic-map", action="append", metavar="KIND=Topic",
                    help=f"override kind→topic for custom groups; kinds: {', '.join(KINDS)}")
    ap.add_argument("--property", action="append", dest="properties", metavar="KEY=VALUE",
                    help="extra scope property attached to every batch (repeatable)")
    ap.add_argument("--repos-dir", action="append",
                    help="extra dir to probe for project repos (default: cwd and its parent)")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--limit", type=int, help="cap the number of records (smoke runs)")
    ap.add_argument("--input", choices=["pre-extracted", "conversation"],
                    default="pre-extracted",
                    help="ingestion path: pre-extracted stores composed notes verbatim with "
                         "a [date] prefix; conversation re-extracts them with created_at "
                         "date context (slower, chronological, extractor routes topics)")
    ap.add_argument("--execute", action="store_true",
                    help="actually migrate (default is a dry-run report)")
    ap.add_argument("--rollback", action="store_true",
                    help="delete every memory this migration created (via run manifests) "
                         "and reset the checkpoint")
    args = ap.parse_args()
    if args.rollback and args.execute:
        sys.exit("--rollback and --execute are mutually exclusive")

    if args.rollback:
        return _rollback(args.source)

    adapter = sources()[args.source](args.db)
    found = adapter.available()
    if not found:
        sys.exit(f"{args.source}: no store found at {adapter.db_path} (override with --db)")

    records = adapter.records(include_all=args.all)
    if args.project:
        wanted = set(args.project)
        records = (r for r in records if r.project in wanted)
    records = list(records)
    if args.limit:
        records = records[: args.limit]

    cwd = os.getcwd()
    repos_dirs = (args.repos_dir or []) + [os.path.dirname(cwd) or cwd, cwd]
    resolve = repo_resolver(repos_dirs, _parse_kv(args.map, "map"))
    schema = _schema()
    # in conversation mode the extractor routes topics itself, so the kind→topic mapping
    # (and its validation) doesn't apply
    topic_map = None
    if args.input == "pre-extracted":
        topic_map = _topic_map(_parse_kv(args.topic_map, "topic-map"), schema)
    batch_props = _batch_properties(schema, args.source, _parse_kv(args.properties, "property"))

    def make_plan(skip):
        if args.input == "conversation":
            return plan_conversations(records, resolve, skip_uids=skip)
        return plan(records, resolve, topic_map, args.batch_size, skip_uids=skip)

    cp_path = checkpoint_path(args.source)
    cp = load_checkpoint(cp_path)

    if not args.execute:
        skip = set(cp["done"]) | {u for uids in cp["pending"].values() for u in uids}
        planned = make_plan(skip)
        header = f"Engram migration (dry run, {args.input}) — source: {args.source} ({found})"
        print(render_report(planned, adapter.describe_selection(args.all), header))
        if batch_props:
            print(f"\nScope properties on every batch: {batch_props}")
        print("\nDry run — nothing written. Add --execute to migrate.")
        return 0

    from ..client import get_client, get_user_id

    client = get_client()
    if client is None:
        sys.exit("ENGRAM_API_KEY not set — cannot migrate.")
    user_id = get_user_id()
    if not user_id:
        sys.exit("no stable identity — set git user.email or ENGRAM_USER_ID first.")

    reconcile_pending(cp, client, print)
    save_checkpoint(cp_path, cp)
    skip = set(cp["done"]) | {u for uids in cp["pending"].values() for u in uids}
    planned = make_plan(skip)
    header = f"Engram migration ({args.input}) — source: {args.source} ({found})"
    print(render_report(planned, adapter.describe_selection(args.all), header))
    if not planned["batches"]:
        print("\nNothing to migrate.")
        return 0

    print()

    def progress(*a):
        # a migration is watched via tee/pipes where python block-buffers stdout — flush per
        # batch so progress is visible live
        print(*a, flush=True)

    # extraction pipelines (conversation) take much longer than pre-extracted storage
    wait = 600 if args.input == "conversation" else 180
    result = execute(planned, client, user_id, cp, cp_path, extra_properties=batch_props,
                     log=progress, mode=args.input, wait_timeout=wait)
    print(
        f"\nDone: {result['committed']} batches committed, "
        f"{len(result['failed'])} failed, {result['pending']} still pending "
        f"(checkpoint: {cp_path})"
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
