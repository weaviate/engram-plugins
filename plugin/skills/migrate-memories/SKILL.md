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
2. **Present the report**: how many memories per scope and day; which source projects
   were skipped because their required scope properties could not be resolved — usually
   the project's directory no longer exists anywhere the CLI looks (offer
   `--map=NAME=owner/repo`, or `--repos-dir DIR` if their repositories live somewhere
   unusual); and the sample item. Topics are not part of the plan, and the source is not
   pre-filtered: Engram's extraction classifies each memory into the group's topics and
   decides what to keep.
3. **Ask the user explicitly whether to proceed.** Executing writes to their Engram cloud
   store — never run `--execute` or `--rollback` without a fresh confirmation from the
   user in this conversation.
4. **Execute**: re-run the same command with `--execute` appended. Conversations are
   submitted in chronological order without waiting on each pipeline run (Engram queues
   internally); the command then waits for the pipeline to settle and reports what
   committed. Exit codes: 0 done, 1 failed submissions/runs, 3 some runs still in the
   pipeline — re-running the same command reconciles and continues safely from the
   checkpoint.
5. **Summarize**: submitted / committed / failed / still in the pipeline, and remind the
   user that a re-run retries only what's missing.

## Options to surface when relevant

- `--limit N --execute` — a small smoke run before committing to a full migration.
- `--repos-dir DIR` — extra directory to search for project repositories (repeatable).
  Rarely needed: project directories are found through Claude Code's own session
  registry, which records every directory the user worked in regardless of layout.
- `--rollback` — deletes every memory the migration created (via the server's per-run
  manifests, so organically stored memories are untouchable) and resets the checkpoint.
  Destructive: treat like `--execute`, explicit confirmation first.
- `--source NAME` / `--db PATH` — other adapters or a non-default store location.

If asked about timestamps: memories carry their original dates as extraction context, so
the memory *content* reflects when things happened — but the `created_at` shown by search
is always the ingestion time; that's a server-side limit.
