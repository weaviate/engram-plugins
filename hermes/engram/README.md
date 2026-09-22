# Engram Memory Provider for Hermes Agent

Persistent, cross-session memory backed by [Weaviate Engram](https://docs.weaviate.io/engram).
Conversation turns are sent to Engram for server-side extraction; relevant memories are recalled
before each turn and searchable on demand.

| | |
| --- | --- |
| **Best for** | Hands-off memory — Engram handles extraction and organization automatically |
| **Requires** | `pip install weaviate-engram` (auto-installed) + API key |
| **Data storage** | Engram Cloud |
| **Cost** | Engram pricing (cloud) |

## Setup

Get an API key at https://console.weaviate.cloud/engram, then:

```bash
hermes memory setup    # select "engram", paste the key
```

Or manually:

```bash
hermes config set memory.provider engram
echo "ENGRAM_API_KEY=your-key" >> ~/.hermes/.env
```

**Tools (2):** `engram_search` (semantic search over the user's memories),
`engram_add` (store a durable fact the moment the user states one).

## How it works

- **Recall** — before each turn, a background `memories.search` runs against the current prompt;
  results are injected as context. The agent can also search on demand with `engram_search`.
- **Store** — after each turn, the exchange is sent to `memories.add` in a daemon thread (never
  blocks a response), tagged with `session_id` as the scope property.
- **Mirroring** — writes to Hermes' built-in `MEMORY.md` / `USER.md` are mirrored to Engram
  (`add` and `replace` actions). `remove` is **not** propagated — Engram is append-only from this
  provider's side, so a built-in deletion can't be undone remotely.
- **Fail-open** — a missing key, missing identity, or a down API disables memory for the session;
  it never breaks a conversation. After 5 consecutive API failures, calls pause for 120s
  (circuit breaker).

## Identity

Memories are isolated per `user_id`, resolved in this order:

1. `user_id` in `$HERMES_HOME/engram.json` (or `ENGRAM_USER_ID` env) — operator-configured,
   applies uniformly across every gateway (CLI, Telegram, Discord, …).
2. The gateway-native user id (Telegram numeric id, Discord snowflake, …).
3. `git config user.email`.

There is deliberately **no shared default**: a non-unique id would commingle different people's
memories with no way to un-mix them later. If no identity resolves, the provider stays disabled
and logs why.

## Config

Secret — in `$HERMES_HOME/.env` or the environment:

| Variable | Purpose |
| --- | --- |
| `ENGRAM_API_KEY` | Your Engram API key (required). |

Non-secret — `$HERMES_HOME/engram.json` (written by `hermes memory setup`; env vars read as
fallback):

| Key | Env var | Default | Description |
| --- | --- | --- | --- |
| `user_id` | `ENGRAM_USER_ID` | — | Canonical user identifier (see Identity above). |
| `base_url` | `ENGRAM_BASE_URL` | `https://api.engram.weaviate.io` | Endpoint override (dev/self-hosted). |

## Privacy

Off-device data: conversation turns (user + assistant text), facts stored via `engram_add`, and
mirrored built-in memory writes are sent to Engram Cloud for extraction and storage. Tool calls
and tool results are **not** forwarded. Recall queries (the user's current prompt) are sent on
search.
