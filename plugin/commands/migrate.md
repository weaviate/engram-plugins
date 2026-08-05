---
description: Migrate memories from another local memory system (claude-mem) into Engram
---

Migrate the user's existing memories into Engram with the plugin's migration CLI. It is
read-only on the source store and resumable (checkpoint in `~/.engram/migrate/`).

1. Dry-run first (never skip this). If the user's arguments contain `--execute` or
   `--rollback`, REMOVE them for this step — they are applied only after the user confirms
   in step 3:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/bin/engram-migrate" $ARGUMENTS   # minus --execute/--rollback
   ```

2. Present the report to the user: how many memories per topic and repo, which projects
   were skipped because no git remote was found (offer `--map NAME=owner/repo` for the ones
   worth keeping), and the sample item. Mention that low-signal observation types are
   excluded by default and `--all` includes them.

3. Ask the user explicitly whether to proceed. Do not migrate without their confirmation.

4. On confirmation, re-run the same command with `--execute` appended. Batches are
   submitted serially and each waits for the Engram pipeline to commit, so this can take a
   while for large stores — that is expected.

5. Report the final summary (committed / failed / still pending). If batches failed or are
   pending, tell the user a re-run of the same command will retry only what's missing.

Two further modes, only when the user asks for them:

- `--input conversation` — ingest via the extraction pipeline with real date context
  (slower, strictly chronological, extractor routes topics). Warn the user that the
  search-visible `created_at` is still the ingestion time in either mode.
- `--rollback` — deletes every memory the migration created (via run manifests) and resets
  the checkpoint. Destructive: always get explicit confirmation first.
