---
name: migrate-memories
description: >
  Migrate memories from another local memory system into Engram (claude-mem supported
  today). Use this whenever the user wants to import, migrate, transfer, or backfill
  memories from claude-mem or a previous memory tool into Engram, mentions switching
  memory systems, asks to bring old coding-session history into Engram, wants to re-import
  memories with their original dates, or wants to roll back / undo a previous Engram
  migration — even if they don't say the word "migrate".
---

# Migrate memories into Engram

Everything runs through one CLI bundled with the plugin. It is safe by design: dry-run by
default, strictly read-only on the source store, and resumable — a local checkpoint records
every migrated item, so interrupted or repeated runs never send anything twice.

Invoke it through the wrapper in this skill's `scripts/` directory (next to this SKILL.md).
It is self-locating — no environment variables or harness-specific paths needed:

```bash
bash <this skill's directory>/scripts/migrate.sh [flags]
```

## Flow

1. **Dry-run first, always.** Run with the user's flags but WITHOUT `--execute` or
   `--rollback`, even if the user included them — those only run after step 3. The dry-run
   writes nothing and shows exactly what would migrate, so the user decides from facts.
2. **Present the report**: how many memories per topic and repo; which source projects were
   skipped because no git remote was found (offer `--map NAME=owner/repo` for ones worth
   keeping); that low-signal observation types are excluded by default (`--all` includes
   them); and the sample item.
3. **Ask the user explicitly whether to proceed.** Executing writes to their Engram cloud
   store — never run `--execute` or `--rollback` without a fresh confirmation from the
   user in this conversation.
4. **Execute**: re-run the same command with `--execute` appended. Batches submit serially
   and each waits for the Engram pipeline to commit, so large stores take a while — that is
   expected, not a hang. Exit codes: 0 done, 1 some batches failed, 3 incomplete (batches
   still pending) — re-running the same command continues safely from the checkpoint.
5. **Summarize**: committed / failed / still pending, and remind the user that a re-run
   retries only what's missing.

## Options to surface when relevant

- `--limit N --execute` — a small smoke run before committing to a full migration.
- `--input conversation` — re-import through Engram's extraction pipeline with
  original-date context (slower, strictly chronological, the extractor routes topics).
  Warn the user: the search-visible `created_at` timestamp is still the ingestion time in
  either mode — that's a server-side limit; only the memory *content* carries real dates.
- `--rollback` — deletes every memory the migration created (via the server's per-run
  manifests, so organically stored memories are untouchable) and resets the checkpoint.
  Destructive: treat like `--execute`, explicit confirmation first.
- `--source NAME` / `--db PATH` — other adapters or a non-default store location.
