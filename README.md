# Weaviate Engram Integrations

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
/plugin marketplace add weaviate/engram-integrations
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

## Environment variables

| Variable          | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `ENGRAM_API_KEY`  | Your Engram API key (required).                               |
| `ENGRAM_USER_ID`  | Override your identity (defaults to `git config user.email`). |
| `ENGRAM_BASE_URL` | Point at a different Engram endpoint.                         |
