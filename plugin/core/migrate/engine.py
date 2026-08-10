"""Engine: turn source Records into committed Engram memories.

Records are grouped into chronological conversations (one per source project and day) and
submitted through Engram's extraction pipeline with the original timestamps as context —
Engram classifies each memory into the group's topics itself, so the migration works with
any topic setup. Submission is strictly serial, earliest to latest (the API contract for
conversation input), waited on (up to a timeout) before the next is sent, with the
checkpoint written around every step so an interrupted migration resumes instead of
duplicating. One window is inherently open: a crash between the server accepting an add
and the pending entry being persisted resubmits that batch on resume — closing it needs
server-side idempotency keys.

Planning (no network) is separated from execution so --dry-run and tests exercise the full
mapping without credentials or the SDK."""

import json
import os

from ..util import git_repo


def repo_resolver(repos_dirs, overrides):
    """project-name → owner/repo resolver. --map overrides win; otherwise probe each repos
    dir for a same-named child git repo — and the dir itself when its basename matches, since
    sources record the workspace root as a project too. Memoized: resolution shells out to
    git per project."""
    cache = {}

    def resolve(project):
        if project in cache:
            return cache[project]
        repo = overrides.get(project)
        if not repo and project:
            for base in repos_dirs:
                candidates = [os.path.join(base, project)]
                if os.path.basename(os.path.normpath(base)) == project:
                    candidates.insert(0, base)
                for c in candidates:
                    if os.path.isdir(c):
                        repo = git_repo(c)
                        if repo:
                            break
                if repo:
                    break
        cache[project] = repo
        return repo

    return resolve


def _bucket(records, resolve, skip_uids, require_repo):
    """Shared planning pass: resolve each record's repo and drop already-migrated records.

    With require_repo (the group scopes memories by repo_name), records whose project can't
    be resolved are excluded and counted — a wrong repo_name would make them unrecallable,
    which is worse than absent. Without it the group has no repo_name property, nothing can
    be mis-filed, and every record is kept; the resolved repo (or the project name) is then
    only a grouping label."""
    kept, mapping, skipped, project_counts = [], {}, {}, {}
    already = 0
    for rec in records:
        if rec.uid in skip_uids:
            already += 1
            continue
        project = rec.project or "(none)"
        repo = resolve(rec.project) if rec.project else None
        mapping[project] = repo
        if require_repo and not repo:
            skipped[project] = skipped.get(project, 0) + 1
            continue
        project_counts[project] = project_counts.get(project, 0) + 1
        kept.append((repo or project, rec))
    return kept, mapping, skipped, project_counts, already


# One (day, label) group becomes one conversation; a prolific day is chunked so a single
# submission can't grow unbounded. Consecutive chunks of the same group stay adjacent, so
# chronological order is preserved.
MAX_CONVERSATION_MESSAGES = 100


def plan_conversations(records, resolve, skip_uids=frozenset(), require_repo=True):
    """One batch per (day, label), each becoming a single ConversationInput whose
    created_at tells the extraction pipeline when the notes are from. Items are
    (uid, created_at, content); Engram routes topics itself. Batches are ordered
    earliest-to-latest at day granularity (within one day, labels submit in name order;
    items inside a group are time-sorted): the API contract requires importing in
    chronological order, so execution is strictly serial and aborts (resumable) rather
    than skip ahead past an unfinished or failed run. Records without a usable date are
    excluded and counted — they cannot be placed in the chronological stream."""
    kept, mapping, skipped, project_counts, already = _bucket(
        records, resolve, skip_uids, require_repo
    )
    groups = {}
    undated = 0
    for label, rec in kept:
        day = (rec.created_at or "")[:10]
        if len(day) != 10 or not day[:4].isdigit():
            undated += 1
            project = rec.project or "(none)"
            project_counts[project] -= 1
            if not project_counts[project]:
                project_counts.pop(project)
            continue
        groups.setdefault((day, label), []).append((rec.uid, rec.created_at, rec.content))
    batches = []
    for day, label in sorted(groups):
        items = sorted(groups[(day, label)], key=lambda it: it[1])
        for i in range(0, len(items), MAX_CONVERSATION_MESSAGES):
            batches.append((label, items[i : i + MAX_CONVERSATION_MESSAGES]))
    return {
        "batches": batches,
        "mapping": mapping,
        "skipped": skipped,
        "project_counts": project_counts,
        "items": sum(len(b) for _, b in batches),
        "already": already,
        "undated": undated,
        "sample": batches[0][1][0] if batches else None,
    }


def render_report(planned, source_lines, header):
    label_count = len({label for label, _ in planned["batches"]})
    project_counts = planned["project_counts"]
    lines = [header, "", "Source:"]
    lines += [f"  {line}" for line in source_lines]
    lines += ["", "Repo mapping:"]
    for project in sorted(planned["mapping"]):
        repo = planned["mapping"][project]
        if repo and project_counts.get(project):
            lines.append(f"  {project} -> {repo}  ({project_counts.get(project, 0)} items)")
    if planned["skipped"]:
        lines.append("  skipped — no git remote found; use --map NAME=owner/repo:")
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
    """Re-check runs left pending by a previous interrupted/timed-out invocation: committed
    runs mark their uids done, failed runs release them for resubmission, running ones stay
    reserved (their uids are excluded from this pass). A run the server no longer knows
    (404) releases its uids too — keeping it reserved would wedge them forever."""
    for run_id in list(cp["pending"]):
        try:
            rs = client.runs.get(run_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                log(f"pending run {run_id} unknown to the server; its items will be resubmitted")
                cp["pending"].pop(run_id)
            else:
                log(f"pending run {run_id}: status check failed ({e}); keeping reserved")
            continue
        state, err = run_state(rs)
        if state == "done":
            for uid in cp["pending"].pop(run_id):
                cp["done"][uid] = run_id
        elif state == "failed":
            log(f"pending run {run_id} failed ({err}); its items will be resubmitted")
            cp["pending"].pop(run_id)


def _build_input(label, items):
    # Lazy import: only execution needs the SDK, keeping dry-run and tests dependency-free.
    from engram import ConversationInput, MessageInput

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


def execute(planned, client, user_id, cp, cp_path, wait_timeout=600, log=print,
            extra_properties=None, attach_repo=True):
    """Submit every conversation and wait each run to a terminal state. Returns per-batch
    outcome counts; anything not marked done is picked up by the next invocation via the
    checkpoint. Conversations must land chronologically (the API contract: import earliest
    to latest), so a run still unfinished — or failed — at wait time aborts the loop
    (resumable) rather than letting later days overtake it.

    repo_name is attached per batch only when the group configures it (attach_repo);
    sending a property the group doesn't know would be rejected server-side."""
    failures, committed, waiting = [], 0, 0
    total = len(planned["batches"])
    for i, (label, items) in enumerate(planned["batches"], 1):
        properties = {"repo_name": label} if attach_repo else {}
        properties.update(extra_properties or {})
        try:
            run = client.memories.add(
                _build_input(label, items),
                user_id=user_id,
                properties=properties or None,
            )
        except Exception as e:
            # a rejected/unreachable submit is a failed batch, not a crashed migration
            failures.append((label, None, str(e)))
            log(f"[{i}/{total}] {label}: submit FAILED — {e}")
            log("stopping here to preserve chronological order — fix and re-run")
            break
        uids = [u for u, _, _ in items]
        cp["pending"][run.run_id] = uids
        save_checkpoint(cp_path, cp)

        state, err = _wait(client, run.run_id, wait_timeout)
        if state == "done":
            for uid in uids:
                cp["done"][uid] = run.run_id
            cp["pending"].pop(run.run_id)
            committed += 1
            log(f"[{i}/{total}] {label}: {len(uids)} memories committed (run {run.run_id})")
        elif state == "failed":
            cp["pending"].pop(run.run_id)
            failures.append((label, run.run_id, err))
            log(f"[{i}/{total}] {label}: FAILED (run {run.run_id}) — {err}")
            # a failed day is chronologically unfinished: resubmitting it after later
            # days landed would break the earliest-to-latest contract
            log("stopping here to preserve chronological order — fix and re-run")
            save_checkpoint(cp_path, cp)
            break
        else:
            waiting += 1
            detail = f" ({err})" if err else ""
            log(
                f"[{i}/{total}] {label}: not finished after {wait_timeout}s{detail} "
                f"(run {run.run_id}); left pending — re-run later to reconcile"
            )
            log("stopping here to preserve chronological order — re-run to continue")
            save_checkpoint(cp_path, cp)
            break
        save_checkpoint(cp_path, cp)
    return {"committed": committed, "failed": failures, "pending": waiting}


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


def _wait(client, run_id, timeout):
    try:
        rs = client.runs.wait(run_id, timeout=timeout, interval=1.0)
    except Exception as e:
        # timeout or an API error — leave the run pending for the next invocation, but
        # surface the reason: "auth failed" must not read as "pipeline is slow"
        return "running", str(e)
    return run_state(rs)
