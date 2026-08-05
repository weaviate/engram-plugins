"""CLI: python -m core.migrate [flags]. Dry-run by default — --execute writes.

Run via bin/engram-migrate (the migrate-memories skill's scripts/migrate.sh delegates
there too), which sets up the plugin venv and data dir. Dry-run needs neither credentials
nor the SDK (it may still fetch the group schema for validation when no cache exists), so
the migration plan can be inspected before any memory content leaves the machine.

Exit codes: 0 done, 1 failed batches, 2 usage error (argparse), 3 incomplete — batches
still pending, re-run to continue/reconcile."""

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

DEFAULT_BATCH_SIZE = 50
# extraction pipelines (conversation) take much longer than pre-extracted storage
WAIT_PRE_EXTRACTED, WAIT_CONVERSATION = 180, 600


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
        # git probe) is worse than an error
        if not k.strip() or not v.strip():
            sys.exit(f"--{flag} {p!r}: name and value must be non-empty")
        out[k.strip()] = v.strip()
    return out


def _schema(execute_mode):
    """The cached/fetched group schema. Dry-run degrades to a warning without one; --execute
    refuses to run — sending batches with unvalidated topics/scope would just fail one by
    one server-side."""
    try:
        from ..scope import scope_schema

        return scope_schema()
    except Exception as e:
        if execute_mode:
            sys.exit(
                f"cannot read the group schema ({e}) — refusing to migrate without "
                "validating topics and required scope properties; retry when the API is "
                "reachable"
            )
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
    if schema is not None and not topics:
        print("note: the group schema lists no topics; mapping not validated")
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
    if "repo_name" in overrides:
        # merged after the per-batch value, an override would silently file the whole
        # store under one repo — the exact mis-filing this tool exists to avoid
        sys.exit("repo_name is resolved per batch — use --map NAME=owner/repo to change it")
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


def _load_checkpoint_or_exit(path):
    try:
        return load_checkpoint(path)
    except ValueError as e:
        sys.exit(str(e))


def _source_lines(adapter, include_all):
    fn = getattr(adapter, "describe_selection", None)  # optional in the adapter contract
    return fn(include_all) if fn else []


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
    if deleted == 0 and gone > 0:
        print(f"\nEvery delete reported the memory as already gone ({gone}). That can mean "
              "an earlier rollback finished — or an identity mismatch. Checkpoint kept; "
              "re-run to confirm or inspect it first.")
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
                    help=f"override kind→topic for custom groups; kinds: {', '.join(KINDS)} "
                         "(pre-extracted mode only)")
    ap.add_argument("--property", action="append", dest="properties", metavar="KEY=VALUE",
                    help="extra scope property attached to every batch (repeatable)")
    ap.add_argument("--repos-dir", action="append",
                    help="extra dir to probe for project repos (default: cwd and its parent)")
    ap.add_argument("--batch-size", type=_positive, default=None,
                    help=f"items per pre-extracted batch (default {DEFAULT_BATCH_SIZE}; "
                         "conversation mode groups per repo and day instead)")
    ap.add_argument("--limit", type=_positive,
                    help="cap the number of not-yet-migrated records (smoke runs)")
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
    if args.input == "conversation":
        # reject rather than silently ignore an explicit flag that has no effect here
        if args.topic_map:
            sys.exit("--topic-map has no effect with --input conversation "
                     "(the extractor routes topics itself)")
        if args.batch_size is not None:
            sys.exit("--batch-size has no effect with --input conversation "
                     "(batches are one conversation per repo and day)")
    batch_size = args.batch_size or DEFAULT_BATCH_SIZE

    if args.rollback:
        return _rollback(args.source)

    adapter = sources()[args.source](args.db)
    found = adapter.available()
    if not found:
        sys.exit(f"{args.source}: no store found at {adapter.db_path} (override with --db)")

    cp_path = checkpoint_path(args.source)
    cp = _load_checkpoint_or_exit(cp_path)
    skip = set(cp["done"]) | {u for uids in cp["pending"].values() for u in uids}

    records = adapter.records(include_all=args.all)
    if args.project:
        wanted = set(args.project)
        records = (r for r in records if r.project in wanted)
    records = list(records)
    if args.limit:
        # limit counts fresh work: slicing before the skip filter would re-select
        # already-migrated records on a resumed store and migrate nothing
        records = [r for r in records if r.uid not in skip][: args.limit]

    cwd = os.getcwd()
    repos_dirs = [os.path.expanduser(d) for d in (args.repos_dir or [])]
    repos_dirs += [os.path.dirname(cwd) or cwd, cwd]
    resolve = repo_resolver(repos_dirs, _parse_kv(args.map, "map"))
    schema = _schema(args.execute)
    # in conversation mode the extractor routes topics itself, so the kind→topic mapping
    # (and its validation) doesn't apply
    topic_map = None
    if args.input == "pre-extracted":
        topic_map = _topic_map(_parse_kv(args.topic_map, "topic-map"), schema)
    batch_props = _batch_properties(schema, args.source, _parse_kv(args.properties, "property"))

    def make_plan(skip_uids):
        if args.input == "conversation":
            return plan_conversations(records, resolve, skip_uids=skip_uids)
        return plan(records, resolve, topic_map, batch_size, skip_uids=skip_uids)

    if not args.execute:
        planned = make_plan(skip)
        header = f"Engram migration (dry run, {args.input}) — source: {args.source} ({found})"
        print(render_report(planned, _source_lines(adapter, args.all), header))
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
    if args.input == "conversation" and cp["pending"]:
        # an earlier-day run is still in flight; submitting later days now would let them
        # overtake it and break the chronological contract
        print(f"{len(cp['pending'])} earlier run(s) still in flight — wait for them to "
              "finish and re-run to continue chronologically.")
        save_checkpoint(cp_path, cp)
        return 3
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
    planned = make_plan(skip)
    header = f"Engram migration ({args.input}) — source: {args.source} ({found})"
    print(render_report(planned, _source_lines(adapter, args.all), header))
    if not planned["batches"]:
        print("\nNothing to migrate.")
        return 0

    print()

    def progress(*a):
        # a migration is watched via tee/pipes where python block-buffers stdout — flush per
        # batch so progress is visible live
        print(*a, flush=True)

    wait = WAIT_CONVERSATION if args.input == "conversation" else WAIT_PRE_EXTRACTED
    result = execute(planned, client, user_id, cp, cp_path, extra_properties=batch_props,
                     log=progress, mode=args.input, wait_timeout=wait)
    print(
        f"\nDone: {result['committed']} batches committed, "
        f"{len(result['failed'])} failed, {result['pending']} still pending "
        f"(checkpoint: {cp_path})"
    )
    if result["failed"]:
        return 1
    return 3 if result["pending"] else 0


if __name__ == "__main__":
    sys.exit(main())
