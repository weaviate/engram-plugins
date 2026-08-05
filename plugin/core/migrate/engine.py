"""Engine: turn source Records into committed Engram memories.

Planning (pure, no network) is separated from execution so --dry-run and tests exercise the
full mapping without credentials or the SDK. Scope properties are call-level in the add API,
so batches are grouped per resolved repo_name; each batch is one memories.add run, waited on
(up to a timeout) before the next is sent, with the checkpoint written around every step so
an interrupted migration resumes instead of duplicating. One window is inherently open: a
crash between the server accepting an add and the pending entry being persisted resubmits
that batch on resume — closing it needs server-side idempotency keys."""

import json
import os

from ..util import git_repo

# kind → topic in Engram's default code-assisting group. Session summaries and task records
# map to TaskStatus, not SessionHistory: SessionHistory is bounded and session_id-scoped, so
# a dead session's entries could never be recalled from a new session. Users with custom
# groups remap per kind via --topic-map; the CLI validates the mapping against the live
# group schema before running.
KIND_TO_TOPIC = {
    "architecture": "DomainAndArchitecture",
    "process": "Processes",
    "task": "TaskStatus",
    "preference": "DeveloperPreferences",
}


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


def _dated(rec):
    """[YYYY-MM-DD] prefix from the source timestamp — pre-extracted items carry no
    created_at field, so the original date survives inside the content."""
    d = (rec.created_at or "")[:10]
    return f"[{d}] {rec.content}" if len(d) == 10 and d[:4].isdigit() else rec.content


def _bucket(records, resolve, skip_uids):
    """Shared planning pass: resolve each record's repo, drop unresolvable and
    already-migrated records. Records whose project can't be resolved are excluded and
    counted — a wrong repo_name would make them unrecallable, which is worse than absent."""
    kept, mapping, skipped, project_counts = [], {}, {}, {}
    already = 0
    for rec in records:
        if rec.uid in skip_uids:
            already += 1
            continue
        project = rec.project or "(none)"
        repo = resolve(rec.project) if rec.project else None
        mapping[project] = repo
        if not repo:
            skipped[project] = skipped.get(project, 0) + 1
            continue
        project_counts[project] = project_counts.get(project, 0) + 1
        kept.append((repo, rec))
    return kept, mapping, skipped, project_counts, already


def _result(batches, mapping, skipped, project_counts, by_topic, already, undated=0):
    return {
        "batches": batches,
        "mapping": mapping,
        "skipped": skipped,
        "project_counts": project_counts,
        "by_topic": by_topic,
        "items": sum(len(b) for _, b in batches),
        "already": already,
        "undated": undated,
        "sample": batches[0][1][0] if batches else None,
    }


def plan(records, resolve, topic_map, batch_size, skip_uids=frozenset()):
    """Pre-extracted mode: per-repo batches of (uid, topic, content) items, the [date]
    prefix carrying the original date inside the content."""
    kept, mapping, skipped, project_counts, already = _bucket(records, resolve, skip_uids)
    groups, by_topic = {}, {}
    for repo, rec in kept:
        topic = topic_map[rec.kind]
        groups.setdefault(repo, []).append((rec.uid, topic, _dated(rec)))
        by_topic[topic] = by_topic.get(topic, 0) + 1
    batches = []
    for repo in sorted(groups):
        items = groups[repo]
        for i in range(0, len(items), batch_size):
            batches.append((repo, items[i : i + batch_size]))
    return _result(batches, mapping, skipped, project_counts, by_topic, already)


# One (day, repo) group becomes one conversation; a prolific day is chunked so a single
# submission can't grow unbounded. Consecutive chunks of the same group stay adjacent, so
# chronological order is preserved.
MAX_CONVERSATION_MESSAGES = 100


def plan_conversations(records, resolve, skip_uids=frozenset()):
    """Conversation mode: one batch per (day, repo), each becoming a single ConversationInput
    whose created_at tells the extraction pipeline when the notes are from. Items are
    (uid, created_at, content) — raw content, no [date] prefix (created_at replaces it) and
    no topic (the extractor routes topics itself). Batches are ordered earliest-to-latest at
    day granularity (within one day, repos submit in name order; items inside a group are
    time-sorted): the API contract requires importing in chronological order, so execution
    is strictly serial and aborts (resumable) rather than skip ahead past an unfinished or
    failed run. Records without a usable date are excluded and counted — they cannot be
    placed in the chronological stream (pre-extracted mode carries them fine)."""
    kept, mapping, skipped, project_counts, already = _bucket(records, resolve, skip_uids)
    groups = {}
    undated = 0
    for repo, rec in kept:
        day = (rec.created_at or "")[:10]
        if len(day) != 10 or not day[:4].isdigit():
            undated += 1
            project = rec.project or "(none)"
            project_counts[project] -= 1
            if not project_counts[project]:
                project_counts.pop(project)
            continue
        groups.setdefault((day, repo), []).append((rec.uid, rec.created_at, rec.content))
    batches = []
    for day, repo in sorted(groups):
        items = sorted(groups[(day, repo)], key=lambda it: it[1])
        for i in range(0, len(items), MAX_CONVERSATION_MESSAGES):
            batches.append((repo, items[i : i + MAX_CONVERSATION_MESSAGES]))
    return _result(batches, mapping, skipped, project_counts, {}, already, undated=undated)


def render_report(planned, source_lines, header):
    repo_count = len({repo for repo, _ in planned["batches"]})
    project_counts = planned["project_counts"]
    lines = [header, "", "Source:"]
    lines += [f"  {line}" for line in source_lines]
    lines += ["", "Repo mapping:"]
    for project in sorted(planned["mapping"]):
        repo = planned["mapping"][project]
        if repo:
            lines.append(f"  {project} -> {repo}  ({project_counts.get(project, 0)} items)")
    if planned["skipped"]:
        lines.append("  skipped — no git remote found; use --map NAME=owner/repo:")
        for project in sorted(planned["skipped"]):
            note = "  — no project recorded; --map cannot recover these" if project == "(none)" else ""
            lines.append(f"    {project}  ({planned['skipped'][project]} items){note}")
    lines += [
        "",
        f"Plan: {planned['items']} memories in {len(planned['batches'])} batches "
        f"across {repo_count} repos",
    ]
    if planned["by_topic"]:
        lines.append(
            "  by topic: "
            + ", ".join(f"{t} {n}" for t, n in sorted(planned["by_topic"].items()))
        )
    if planned["already"]:
        # pending (in-flight, unconfirmed) uids are counted here too — the checkpoint
        # reserves them; reconcile on --execute settles which ones actually committed
        lines.append(f"  recorded in checkpoint (migrated or in flight): {planned['already']}")
    if planned.get("undated"):
        lines.append(
            f"  undated records excluded: {planned['undated']} — conversation mode needs "
            "created_at; migrate them with --input pre-extracted"
        )
    if planned["sample"]:
        uid, tag, content = planned["sample"]  # tag: topic (pre-extracted) or timestamp
        preview = content if len(content) <= 400 else content[:400] + "…"
        lines += ["", f"Sample item ({uid}, {tag}):", f"  {preview}"]
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


def _build_input(mode, repo, items):
    # Lazy imports: only execution needs the SDK, keeping dry-run and tests dependency-free.
    if mode == "conversation":
        from engram import ConversationInput, MessageInput

        first_ts = items[0][1]
        msgs = [
            MessageInput(
                role="system",
                content=(
                    f"Archived coding-session notes for repository {repo}, recorded on "
                    f"{first_ts[:10] or 'an unknown date'}. Each user message is one "
                    "summarized note from that day."
                ),
            )
        ]
        for _, ts, content in items:
            msgs.append(MessageInput(role="user", content=content, created_at=ts or None))
        return ConversationInput(messages=msgs, created_at=first_ts or None)
    from engram import PreExtractedInput, PreExtractedItem

    return PreExtractedInput(items=[PreExtractedItem(content=c, topic=t) for _, t, c in items])


def execute(planned, client, user_id, cp, cp_path, wait_timeout=180, log=print,
            extra_properties=None, mode="pre-extracted"):
    """Submit every batch and wait each run to a terminal state. Returns per-batch outcome
    counts; anything not marked done (failed or still running at timeout) is picked up by
    the next invocation via the checkpoint.

    Conversation mode must land chronologically (the API contract: import earliest-to-
    latest), so a run still unfinished — or failed — at wait time aborts the loop
    (resumable) rather than letting later days overtake it."""
    failures, committed, waiting = [], 0, 0
    total = len(planned["batches"])
    for i, (repo, items) in enumerate(planned["batches"], 1):
        try:
            run = client.memories.add(
                _build_input(mode, repo, items),
                user_id=user_id,
                properties={"repo_name": repo, **(extra_properties or {})},
            )
        except Exception as e:
            # a rejected/unreachable submit is a failed batch, not a crashed migration
            failures.append((repo, None, str(e)))
            log(f"[{i}/{total}] {repo}: submit FAILED — {e}")
            if mode == "conversation":
                log("stopping here to preserve chronological order — re-run to continue")
                break
            continue
        uids = [u for u, _, _ in items]
        cp["pending"][run.run_id] = uids
        save_checkpoint(cp_path, cp)

        state, err = _wait(client, run.run_id, wait_timeout)
        if state == "done":
            for uid in uids:
                cp["done"][uid] = run.run_id
            cp["pending"].pop(run.run_id)
            committed += 1
            log(f"[{i}/{total}] {repo}: {len(uids)} memories committed (run {run.run_id})")
        elif state == "failed":
            cp["pending"].pop(run.run_id)
            failures.append((repo, run.run_id, err))
            log(f"[{i}/{total}] {repo}: FAILED (run {run.run_id}) — {err}")
            if mode == "conversation":
                # a failed day is chronologically unfinished: resubmitting it after later
                # days landed would break the earliest-to-latest contract
                log("stopping here to preserve chronological order — fix and re-run")
                save_checkpoint(cp_path, cp)
                break
        else:
            waiting += 1
            detail = f" ({err})" if err else ""
            log(
                f"[{i}/{total}] {repo}: not finished after {wait_timeout}s{detail} "
                f"(run {run.run_id}); left pending — re-run later to reconcile"
            )
            if mode == "conversation":
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
