---
description: Import Claude Code's local file-based memories into Engram, tagged with each source project's git origin repo
---

# Import Claude Code memories into Engram

Claude Code keeps per-project memory files under `~/.claude/projects/*/memory/*.md`; they are
siloed to the project they were written in. This command imports them into Engram — tagged with
each source project's git origin repo (`repo_name`, the same scope property the store hook uses)
— so they become recallable from any project.

`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` below are substituted by the plugin loader;
they must be passed explicitly because the Bash tool does not inherit them.

## Steps

1. Dry run first, to see what would be imported:

   ```bash
   env CLAUDE_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}" CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" \
     bash "${CLAUDE_PLUGIN_ROOT}/hooks/with-venv.sh" -m core.tools.import_memories --dry-run
   ```

2. Give the user a short summary of the plan: how many memories, grouped by repo/project, and
   any projects whose path could not be resolved (those import without a repo tag). If the dry
   run finds nothing new, report that and stop.

3. Run the real import — the same command without `--dry-run` — and report the outcome:
   imported / already imported / failed counts, plus any per-file failures verbatim.

## Notes

- The importer records imported files (by content hash) in the plugin data dir, so re-running
  only picks up new or changed memory files. `--force` re-imports everything — warn the user
  that this can create duplicate memories in Engram before using it.
- Imported memories are copies: the local files remain and still load in their own projects.
  If the user asks about cleanup, they can delete a project's `memory/` files themselves — do
  not delete them as part of this command.
- If the importer reports a missing API key, the fix is `export ENGRAM_API_KEY=...` (ideally in
  a shell profile such as `~/.zshenv`). If it reports a missing identity, set `git config
  user.email` or `ENGRAM_USER_ID`.
