# Engram × Hermes Agent

A [Hermes Agent](https://hermes-agent.nousresearch.com) memory provider backed by
[Weaviate Engram](https://docs.weaviate.io/engram) — persistent, cross-session memory with
server-side extraction and scoped recall.

## Install

```bash
bash hermes/install.sh
```

The installer auto-detects where your Hermes looks for providers:

- **`$HERMES_HOME/plugins/engram/`** (user-installed providers, survives `hermes update`) —
  preferred when supported;
- **`<hermes checkout>/plugins/memory/engram/`** (bundled) — fallback for older versions,
  or forced with `--bundled`.

Flags: `--link` symlinks instead of copying (development — edits here go live);
pass a checkout path or set `HERMES_REPO` if auto-detection fails.

Then configure:

```bash
hermes memory setup    # choose "engram", paste your API key
```

Get a key at https://console.weaviate.cloud/engram. Memory works from the first turn.

**Config reference, identity rules, tools, privacy:** see [`engram/README.md`](engram/README.md).

## Layout

```
hermes/
├── install.sh        # installer (copy/symlink into the right plugin location)
├── engram/           # the provider package — self-contained, upstream-PR-ready
│   ├── __init__.py   #   EngramMemoryProvider + register()
│   ├── plugin.yaml   #   metadata + pip_dependencies (weaviate-engram)
│   └── README.md     #   setup + config reference
└── tests/            # pytest suite — no network, no real SDK (stubbed)
```

The provider package is deliberately self-contained (no imports from this repo) so it can be
dropped into a `NousResearch/hermes-agent` PR at `plugins/memory/engram/` unchanged.

## Development

```bash
bash hermes/install.sh --link       # live-edit against your Hermes install
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest hermes/tests -v
```

The tests stub the Hermes ABC, `hermes_constants`, and the Engram SDK, and load the provider by
file path — mirroring Hermes' own discovery, so they double as a drop-in compatibility check.
