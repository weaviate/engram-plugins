"""Engine: turn source Records into committed Engram memories.

Planning (pure, no network) is separated from execution so --dry-run and tests exercise the
full mapping without credentials or the SDK. Scope properties are call-level in the add API,
so batches are grouped per resolved repo_name; each batch is one pre-extracted memories.add
run, polled to a terminal state before the next is sent (self-throttling), with the
checkpoint written around every step so an interrupted migration resumes instead of
duplicating."""

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
    kept, mapping, skipped = [], {}, {}
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
        kept.append((repo, rec))
    return kept, mapping, skipped, already


def _result(batches, mapping, skipped, by_topic, already):
    return {
        "batches": batches,
        "mapping": mapping,
        "skipped": skipped,
        "by_topic": by_topic,
        "items": sum(len(b) for _, b in batches),
        "already": already,
        "sample": batches[0][1][0] if batches else None,
    }


def plan(records, resolve, topic_map, batch_size, skip_uids=frozenset()):
    """Pre-extracted mode: per-repo batches of (uid, topic, content) items, the [date]
    prefix carrying the original date inside the content."""
    kept, mapping, skipped, already = _bucket(records, resolve, skip_uids)
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
    return _result(batches, mapping, skipped, by_topic, already)


def plan_conversations(records, resolve, skip_uids=frozenset()):
    """Conversation mode: one batch per (day, repo), each becoming a single ConversationInput
    whose created_at tells the extraction pipeline when the notes are from. Items are
    (uid, created_at, content) — raw content, no [date] prefix (created_at replaces it) and
    no topic (the extractor routes topics itself). Batches are ordered earliest-to-latest
    globally: the API contract requires importing in chronological order, so execution is
    strictly serial and aborts (resumable) rather than skip ahead past an unfinished run."""
    kept, mapping, skipped, already = _bucket(records, resolve, skip_uids)
    groups = {}
    for repo, rec in kept:
        day = (rec.created_at or "")[:10] or "0000-00-00"
        groups.setdefault((day, repo), []).append((rec.uid, rec.created_at or "", rec.content))
    batches = []
    for day, repo in sorted(groups):
        batches.append((repo, sorted(groups[(day, repo)], key=lambda it: it[1])))
    return _result(batches, mapping, skipped, {}, already)


def render_report(planned, source_lines, header):
    counts = {}
    for repo, items in planned["batches"]:
        counts[repo] = counts.get(repo, 0) + len(items)
    lines = [header, "", "Source:"]
    lines += [f"  {line}" for line in source_lines]
    lines += ["", "Repo mapping:"]
    for project in sorted(planned["mapping"]):
        repo = planned["mapping"][project]
        if repo:
            lines.append(f"  {project} -> {repo}  ({counts.get(repo, 0)} items)")
    if planned["skipped"]:
        lines.append("  skipped — no git remote found; use --map NAME=owner/repo:")
        for project in sorted(planned["skipped"]):
            lines.append(f"    {project}  ({planned['skipped'][project]} items)")
    lines += [
        "",
        f"Plan: {planned['items']} memories in {len(planned['batches'])} batches "
        f"across {len(counts)} repos",
        "  by topic: "
        + (
            ", ".join(f"{t} {n}" for t, n in sorted(planned["by_topic"].items()))
            or "none"
        ),
    ]
    if planned["already"]:
        lines.append(f"  already migrated (checkpoint): {planned['already']}")
    if planned["sample"]:
        uid, topic, content = planned["sample"]
        preview = content if len(content) <= 400 else content[:400] + "…"
        lines += ["", f"Sample item [{topic}, {uid}]:", f"  {preview}"]
    return "\n".join(lines)


def checkpoint_path(source_name):
    return os.path.expanduser(f"~/.engram/migrate/{source_name}.json")


def load_checkpoint(path):
    try:
        with open(path) as f:
            cp = json.load(f)
    except FileNotFoundError:
        return {"done": {}, "pending": {}}
    return {"done": cp.get("done", {}), "pending": cp.get("pending", {})}


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
    reserved (their uids are excluded from this pass)."""
    for run_id in list(cp["pending"]):
        try:
            rs = client.runs.get(run_id)
        except Exception as e:
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
    latest), so a run still unfinished at wait_timeout aborts the loop — resumable — rather
    than letting later days overtake it."""
    failures, committed, waiting = [], 0, 0
    total = len(planned["batches"])
    for i, (repo, items) in enumerate(planned["batches"], 1):
        run = client.memories.add(
            _build_input(mode, repo, items),
            user_id=user_id,
            properties={"repo_name": repo, **(extra_properties or {})},
        )
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
        else:
            waiting += 1
            log(
                f"[{i}/{total}] {repo}: still running after {wait_timeout}s "
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
    Returns (deleted, gone, fetch_errors); the caller clears the checkpoint only when
    fetch_errors is empty, so an unreachable manifest keeps its uids retryable."""
    run_ids = sorted(set(cp["done"].values()) | set(cp["pending"]))
    deleted, gone, fetch_errors = 0, 0, []
    for i, rid in enumerate(run_ids, 1):
        try:
            ops = client.runs.get(rid).committed_operations
        except Exception as e:
            fetch_errors.append((rid, str(e)))
            log(f"[{i}/{len(run_ids)}] run {rid}: manifest fetch failed ({e}) — kept for retry")
            continue
        ids = [op.memory_id for op in (ops.created if ops else [])]
        for mid in ids:
            try:
                client.memories.delete(mid, user_id=user_id)
                deleted += 1
            except Exception as e:
                if getattr(e, "status_code", None) == 404:
                    gone += 1
                else:
                    raise
        log(f"[{i}/{len(run_ids)}] run {rid}: {len(ids)} memories deleted")
    return deleted, gone, fetch_errors


def _wait(client, run_id, timeout):
    try:
        rs = client.runs.wait(run_id, timeout=timeout, interval=1.0)
    except Exception:
        # timeout or a transient API error — leave the run pending for the next invocation
        return "running", None
    return run_state(rs)
