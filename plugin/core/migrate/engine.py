"""Engine: turn source Records into committed Engram memories.

Records are grouped into chronological conversations (one per source project and day) and
submitted through Engram's extraction pipeline with the original timestamps as context —
Engram classifies each memory into the group's topics itself, so the migration works with
any topic setup.

Scope properties per conversation come from core.scope.resolve_scope on the source
project's directory — the same resolution the store hook uses for realtime adds — so the
migration follows the same configuration files and conventions (see __main__).

Conversations are submitted in chronological order without waiting on each pipeline run:
Engram queues internally, so arrival order is submission order. Run ids go to the
checkpoint and settle() polls them afterwards to record what committed. The checkpoint is
written around every step so an interrupted migration resumes instead of duplicating. One
window is inherently open: a crash between the server accepting an add and the pending
entry being persisted resubmits that batch on resume — closing it needs server-side
idempotency keys."""

import json
import os
import time

from engram import ConversationInput, MessageInput


def project_dir_finder(repos_dirs):
    """project name → existing directory, or None. Probes each repos dir for a same-named
    child — and the dir itself when its basename matches, since sources record the
    workspace root as a project too. Memoized: probing hits the filesystem per project."""
    cache = {}

    def find(project):
        if project in cache:
            return cache[project]
        found = None
        for base in repos_dirs:
            candidates = [os.path.join(base, project)]
            if os.path.basename(os.path.normpath(base)) == project:
                candidates.insert(0, base)
            for c in candidates:
                if os.path.isdir(c):
                    found = c
                    break
            if found:
                break
        cache[project] = found
        return found

    return find


# One (day, project) group becomes one conversation; a prolific day is chunked so a single
# submission can't grow unbounded. Consecutive chunks of the same group stay adjacent, so
# chronological order is preserved.
MAX_CONVERSATION_MESSAGES = 100


def plan_conversations(records, props_for, skip_uids=frozenset()):
    """One batch per (day, project), each becoming a single ConversationInput whose
    created_at tells the extraction pipeline when the notes are from. Items are
    (uid, created_at, content); Engram routes topics itself. Batches are ordered
    earliest-to-latest at day granularity and submitted in that order.

    props_for(project) supplies the batch's scope properties (resolved like the store
    hook's); None means the group's required properties can't be satisfied for that
    project — those records are excluded and counted, because wrong scoping would make
    them unrecallable. Records without a usable date are excluded too: they cannot be
    placed in the chronological stream."""
    groups, mapping, skipped, project_counts = {}, {}, {}, {}
    props_by_project = {}
    already = undated = 0
    for rec in records:
        if rec.uid in skip_uids:
            already += 1
            continue
        project = rec.project or "(none)"
        if project not in props_by_project:
            props_by_project[project] = props_for(rec.project) if rec.project else None
        props = props_by_project[project]
        if props is None:
            mapping[project] = None
            skipped[project] = skipped.get(project, 0) + 1
            continue
        mapping[project] = props.get("repo_name") or project
        day = (rec.created_at or "")[:10]
        if len(day) != 10 or not day[:4].isdigit():
            undated += 1
            continue
        project_counts[project] = project_counts.get(project, 0) + 1
        groups.setdefault((day, project), []).append((rec.uid, rec.created_at, rec.content))
    batches = []
    for day, project in sorted(groups):
        items = sorted(groups[(day, project)], key=lambda it: it[1])
        for i in range(0, len(items), MAX_CONVERSATION_MESSAGES):
            batches.append(
                (mapping[project], props_by_project[project],
                 items[i : i + MAX_CONVERSATION_MESSAGES])
            )
    return {
        "batches": batches,
        "mapping": mapping,
        "skipped": skipped,
        "project_counts": project_counts,
        "items": sum(len(b) for _, _, b in batches),
        "already": already,
        "undated": undated,
        "sample": batches[0][2][0] if batches else None,
    }


def render_report(planned, source_lines, header):
    label_count = len({label for label, _, _ in planned["batches"]})
    project_counts = planned["project_counts"]
    lines = [header, "", "Source:"]
    lines += [f"  {line}" for line in source_lines]
    lines += ["", "Scope mapping:"]
    for project in sorted(planned["mapping"]):
        label = planned["mapping"][project]
        if label and project_counts.get(project):
            lines.append(f"  {project} -> {label}  ({project_counts.get(project, 0)} items)")
    if planned["skipped"]:
        lines.append(
            "  skipped — the group's required scope properties could not be resolved "
            "(--map=NAME=owner/repo supplies repo_name):"
        )
        for project in sorted(planned["skipped"]):
            note = "  — no project recorded; --map cannot recover these" if project == "(none)" else ""
            lines.append(f"    {project}  ({planned['skipped'][project]} items){note}")
    lines += [
        "",
        f"Plan: {planned['items']} memories in {len(planned['batches'])} conversations "
        f"across {label_count} projects (Engram classifies topics itself)",
    ]
    if planned["already"]:
        # pending (in-flight, unconfirmed) uids are counted here too — the checkpoint
        # reserves them; reconcile on --execute settles which ones actually committed
        lines.append(f"  recorded in checkpoint (migrated or in flight): {planned['already']}")
    if planned.get("undated"):
        lines.append(
            f"  undated records excluded: {planned['undated']} — the chronological "
            "conversation stream needs created_at"
        )
    if planned["sample"]:
        uid, ts, content = planned["sample"]
        preview = content if len(content) <= 400 else content[:400] + "…"
        lines += ["", f"Sample item ({uid}, {ts}):", f"  {preview}"]
    return "\n".join(lines)


def checkpoint_path(source_name):
    return os.path.expanduser(f"~/.engram/migrate/{source_name}.json")


def load_checkpoint(path):
    """Load the resume state. A damaged checkpoint raises ValueError with the file named
    (mirroring config._read_json) instead of a bare traceback — the CLI turns it into a
    clean exit telling the user to fix or delete the file. Extra keys (source_db, user_id
    identity stamps) are preserved so a later save doesn't drop them."""
    try:
        with open(path) as f:
            cp = json.load(f)
    except FileNotFoundError:
        return {"done": {}, "pending": {}}
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"corrupt checkpoint {path}: {e} — fix or delete it to reset") from e
    if not isinstance(cp, dict) or not isinstance(cp.get("done", {}), dict) \
            or not isinstance(cp.get("pending", {}), dict):
        raise ValueError(f"corrupt checkpoint {path}: expected JSON object with done/pending maps")
    return {**cp, "done": cp.get("done", {}), "pending": cp.get("pending", {})}


def save_checkpoint(path, cp):
    # atomic write: a migration killed mid-save must not corrupt the resume state
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cp, f)
    os.replace(tmp, path)


def run_state(rs):
    """Collapse a RunStatus into done/failed/running. The status vocabulary isn't pinned by
    the SDK, so both spellings of each terminal state are accepted; an error field is
    terminal regardless of status."""
    status = (getattr(rs, "status", "") or "").lower()
    error = getattr(rs, "error", None)
    if error or status in ("failed", "error", "errored", "cancelled", "canceled"):
        return "failed", error or status
    if status in ("completed", "complete", "succeeded", "success"):
        return "done", None
    return "running", None


def reconcile_pending(cp, client, log):
    """Re-check the checkpoint's pending runs: committed runs mark their uids done, failed
    runs release them for resubmission, running ones stay reserved. A run the server no
    longer knows (404) releases its uids too — keeping it reserved would wedge them
    forever. Returns (committed run count, failed/released run ids) for this pass."""
    done, failed = 0, []
    for run_id in list(cp["pending"]):
        try:
            rs = client.runs.get(run_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                log(f"pending run {run_id} unknown to the server; its items will be resubmitted")
                cp["pending"].pop(run_id)
                failed.append(run_id)
            else:
                log(f"pending run {run_id}: status check failed ({e}); keeping reserved")
            continue
        state, err = run_state(rs)
        if state == "done":
            for uid in cp["pending"].pop(run_id):
                cp["done"][uid] = run_id
            done += 1
        elif state == "failed":
            log(f"run {run_id} failed ({err}); its items will be resubmitted on a re-run")
            cp["pending"].pop(run_id)
            failed.append(run_id)
    return done, failed


def _build_input(label, items):
    first_ts = items[0][1]
    msgs = [
        MessageInput(
            role="system",
            content=(
                f"Archived coding-session notes from {label}, recorded on "
                f"{first_ts[:10] or 'an unknown date'}. Each user message is one "
                "summarized note from that day."
            ),
        )
    ]
    for _, ts, content in items:
        msgs.append(MessageInput(role="user", content=content, created_at=ts or None))
    return ConversationInput(messages=msgs, created_at=first_ts or None)


def execute(planned, client, user_id, cp, cp_path, log=print):
    """Submit every conversation in chronological order without waiting on the pipeline —
    Engram queues internally, so arrival order is submission order. An immediate add error
    stops the loop: that conversation never queued, and submitting later days past it
    would break the earliest-to-latest contract. Run ids go to the checkpoint; settle()
    polls them afterwards."""
    failures, submitted = [], 0
    total = len(planned["batches"])
    for i, (label, props, items) in enumerate(planned["batches"], 1):
        try:
            run = client.memories.add(
                _build_input(label, items),
                user_id=user_id,
                properties=props or None,
            )
        except Exception as e:
            failures.append((label, None, str(e)))
            log(f"[{i}/{total}] {label}: submit FAILED — {e}")
            log("stopping here to preserve chronological order — fix and re-run")
            break
        cp["pending"][run.run_id] = [u for u, _, _ in items]
        save_checkpoint(cp_path, cp)
        submitted += 1
        log(f"[{i}/{total}] {label}: {len(items)} memories submitted (run {run.run_id})")
    return {"submitted": submitted, "failed": failures}


def settle(cp, client, cp_path, timeout, interval=5, log=print):
    """Poll the checkpoint's pending runs until all reach a terminal state or the timeout
    passes. Purely observational — submission is never gated on it. Returns (committed run
    count, failed run ids); anything still pending stays reserved and reconciles on the
    next invocation."""
    deadline = time.monotonic() + timeout
    committed, failed = 0, []
    last = None
    while cp["pending"]:
        done_now, failed_now = reconcile_pending(cp, client, log)
        committed += done_now
        failed += failed_now
        save_checkpoint(cp_path, cp)
        remaining = len(cp["pending"])
        if not remaining or time.monotonic() >= deadline:
            break
        if remaining != last:
            log(f"{remaining} run(s) still in the pipeline …")
            last = remaining
        time.sleep(interval)
    return committed, failed


def rollback(cp, client, user_id, log=print):
    """Delete every memory the migration created, using the server's own run manifests:
    each checkpoint run_id → runs.get().committed_operations.created → memory ids. Exact by
    construction — memories from runs the migrator never submitted are untouchable here.
    Idempotent: a 404 on delete means an earlier (interrupted) rollback already got it.

    Returns (deleted, gone, errors). Errors collect unreachable manifests, runs still in
    flight (their manifest is a moving target — deleting from it would orphan whatever
    commits after the read), and non-404 delete failures; the caller clears the checkpoint
    only when errors is empty, so anything undeleted stays retryable."""
    run_ids = sorted(set(cp["done"].values()) | set(cp["pending"]))
    deleted, gone, errors = 0, 0, []
    for i, rid in enumerate(run_ids, 1):
        try:
            rs = client.runs.get(rid)
        except Exception as e:
            errors.append((rid, str(e)))
            log(f"[{i}/{len(run_ids)}] run {rid}: manifest fetch failed ({e}) — kept for retry")
            continue
        if run_state(rs)[0] == "running":
            errors.append((rid, "still running"))
            log(f"[{i}/{len(run_ids)}] run {rid}: still running — retry rollback once it finishes")
            continue
        ops = rs.committed_operations
        ids = [op.memory_id for op in (ops.created if ops else [])]
        failed_delete = None
        for mid in ids:
            try:
                client.memories.delete(mid, user_id=user_id)
                deleted += 1
            except Exception as e:
                if getattr(e, "status_code", None) == 404:
                    gone += 1
                else:
                    failed_delete = f"delete {mid} failed: {e}"
                    break
        if failed_delete:
            errors.append((rid, failed_delete))
            log(f"[{i}/{len(run_ids)}] run {rid}: {failed_delete} — kept for retry")
        else:
            log(f"[{i}/{len(run_ids)}] run {rid}: {len(ids)} memories removed")
    return deleted, gone, errors
