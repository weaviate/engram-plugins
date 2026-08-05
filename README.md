# Weaviate Engram Plugins

Persistent, cross-session memory for **Claude Code**, backed by
[Weaviate Engram](https://docs.weaviate.io/engram). Claude remembers your preferences,
decisions, and project context across sessions — and recalls what's relevant before it answers.

- **Recall** — before each answer, relevant memories are fetched and added to the conversation.
- **Store** — after each turn, the exchange is saved so it can be recalled later.

Memory is best-effort: it never blocks or breaks a session. When something needs your attention
(bad key, misconfigured scope), Claude surfaces a short `Engram · …` note at the top of its reply.

## Install

Get your API key by creating a project on https://console.weaviate.cloud/engram.
Then set your API key — in your shell, or a shell profile like `~/.zshenv` so it persists:

```bash
export ENGRAM_API_KEY=...
```

Inside Claude Code CLI session do:

```bash
/plugin marketplace add weaviate/engram-plugins
/plugin install engram@weaviate-engram
/reload-plugins
```

That's it. Memory starts working on your next prompt.

## Identity

When Engram project uses `user_id` to isolate memories, it ties it to:

- Defaults to your `git config user.email`.
- Override it with `ENGRAM_USER_ID` if you want a different identity.

## Configuration (optional)

If you have customized your Engram project (custom topics and property scopes), you may need or want to customize how scopes are resolved.

- **Global** — `~/.engram/config.json`: your defaults, everywhere. Full flexibility, including the
  dynamic sources below.
- **Per-directory** — `.engram.json` in a project: committed, shared with your team. Literals and `from` tokens only — no `cmd` (see below).

### Full custom configuration example

Global `~/.engram/config.json`:

```json
{
  "properties": {
    "codebase": { "from": "git-repo" },
    "chat": { "from": "session_id" }
  },
  "search": {
    "properties": ["codebase"],
    "topics": [
      "tooling_preferences",
      "product_summary",
      { "name": "product_knowledge", "properties": ["product"] },
      { "name": "change_summary", "clear_properties": ["codebase"] }
    ]
  }
}
```

In this example `codebase` resolves from the `git` repository name, and `chat` from the assistant's session id.

Search is broad by default; adding `codebase` as a search property narrows recall to memories for the current codebase (for topics scoped by `codebase`). You can also restrict which topics are searched and adjust the scope per topic — e.g. recall summaries about all products, but full knowledge only about the current one. To make a topic _broader_ than the cross-topic filter, use `clear_properties` — so `change_summary` recalls related changes across codebases.

If you want to configure some static property values inside directory manually, you'll need to do this for every working directory. Per-directory `./.engram.json`:

```json
{
  "properties": {
    "product": "payments"
  }
}
```

E.g. you may configure some `product_knowledge` topic on your Engram project that would accumulate knowledge for every product.

### How a property value resolves

The JSON **shape** decides — no ambiguity:

- **string** → a literal, used verbatim (e.g. `"payments"`).
- **object** → a dynamic source:
  - `{ "from": "<token>" }` — a built-in value. Tokens: `git-repo` (owner-scoped repo name),
    `cwd` (working directory), `session_id`.
  - `{ "cmd": ["prog", "arg", …] }` — output of a command, run directly (no shell).
- **array** → a **cascade**: entries are tried in order, first non-empty wins e.g. `[{ "cmd": ["git", "branch", "--show-current"] }, "fallback"]`.

> **`cmd` sources are honored only in your global `~/.engram/config.json`** — a cloned repo must
> never run a command on your machine. A committed per-directory `.engram.json` may use literals
> and `from` tokens, but a `cmd` there is ignored.

### Search scope

- `search.properties` — restrict recall to memories matching these scope keys.
- `search.topics` — restrict recall to specific memory topics, optionally with their own filters.

### Inferred configuration

With the default Engram project (quickstart) configuration, properties are inferred:

- `repo_name` → `git-repo`, then `cwd` (in that order)
- `session_id` → `session_id` from the assistant

and search is narrowed to the `repo_name` property. You can still use `.engram.json` to change this inferred behaviour.

## Migrating from another memory system

If you used another local memory system before Engram, import its memories. Inside Claude
Code run `/engram:migrate`, or from a shell inside a Claude Code session (the plugin's
`bin/` is on PATH there — in a plain terminal use the script's full path,
`<plugin dir>/bin/engram-migrate`):

```bash
engram-migrate            # dry-run: report of what would be migrated (default)
engram-migrate --execute  # migrate — resumable, safe to interrupt and re-run
```

Supported sources: **claude-mem** (default). The importer is strictly read-only on the
source store and idempotent — a checkpoint in `~/.engram/migrate/` records committed items,
so re-runs only send what's missing. Memories are imported through Engram's pre-extracted
pipeline (no LLM re-extraction) with the original date prefixed to each memory.

Useful flags:

- `--all` — include low-signal record types (transient per-session observations), excluded
  by default to keep recall sharp.
- `--map NAME=owner/repo` — map a source project whose repo can't be inferred from a git
  remote (unmapped projects are skipped and reported, never mis-filed).
- `--project NAME` — migrate a single project; `--limit N` — smoke-test with a few items.
- `--topic-map kind=Topic` — for custom Engram groups; the mapping is validated against
  your group's topics before anything is sent.
- `--property KEY=VALUE` — extra scope property for every batch. Required scope properties
  are checked up front; `session_id` (meaningless for migrated data) is auto-filled with a
  `migration:<source>` marker when your group requires it.
- `--input conversation` — ingest through Engram's extraction pipeline instead of
  pre-extracted storage: notes are grouped per repo and day into conversations whose
  `created_at` tells the extractor when the data is from, so memory content carries real
  dates natively. Slower (LLM extraction, strictly chronological), and the extractor
  routes topics itself. Note: the `created_at` shown by search is always the ingestion
  time — Weaviate does not allow overriding it — in either mode.
- `--rollback` — delete every memory the migration created and reset the checkpoint.
  Exact by construction: it deletes via the server's per-run commit manifests, so
  memories stored organically by the plugin hooks are untouchable.

To add a new source system, implement one adapter module in `plugin/core/migrate/`
(see the package docstring for the small adapter contract) and register it in `sources()`.

## Environment variables

| Variable          | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `ENGRAM_API_KEY`  | Your Engram API key (required).                               |
| `ENGRAM_USER_ID`  | Override your identity (defaults to `git config user.email`). |
| `ENGRAM_BASE_URL` | Point at a different Engram endpoint.                         |
