"""Config from JSON files: ~/.engram/config.json (global) plus the current directory's
.engram.json. Holds scope `properties` (how each value resolves) and `search` (which
topics/properties to filter). No env vars, no shell quoting — structured config lives in files."""

import json
import os

# The global (user-owned) config. A ~/.engram/ dir rather than a bare dotfile keeps it extensible;
# it holds your defaults, overridden by the current directory's .engram.json.
USER_CONFIG_PATH = "~/.engram/config.json"


def user_config_path():
    return USER_CONFIG_PATH


def _read_json(path):
    """Read a JSON object from a file. Missing → {}. Malformed JSON raises with the file named, so
    the hook errors out clearly instead of silently ignoring a broken config."""
    try:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def _config_chain(cwd):
    """Config in increasing-precedence order: the global user config, then the .engram.json in the
    current directory (cwd overrides the global defaults)."""
    return [user_config_path(), os.path.join(os.path.realpath(cwd), ".engram.json")]


def _contains_cmd(value):
    """True if a property value uses a {"cmd": ...} source anywhere (including inside a cascade).
    A cmd runs an arbitrary command, so it's refused from committed/local config."""
    if isinstance(value, dict):
        return "cmd" in value
    if isinstance(value, list):
        return any(_contains_cmd(el) for el in value)
    return False


def load_config(cwd):
    """Merge config from the global ~/.engram/config.json and the current directory's .engram.json
    (cwd overrides). `properties` merge key-wise; `search` takes the deepest defined block.

    `cmd` sources run an arbitrary command, so they're honored ONLY from the global config
    (user-owned) — a repo you clone must never run a command on your machine via a hook. Literals
    and `from` tokens (fixed built-in lookups, no arbitrary execution) are fine in a committed
    .engram.json. A `cmd` found in a local file is dropped and reported in `warnings`."""
    cfg = {"properties": {}, "search": {}, "warnings": []}
    for i, path in enumerate(_config_chain(cwd)):
        part = _read_json(path)
        is_global = i == 0  # first entry is the global config; the rest are local/committed
        props = part.get("properties")
        if isinstance(props, dict):
            # JSON shape carries the meaning: string = literal, {"from"|"cmd"} = dynamic source,
            # list = cascade. Only `cmd` is refused locally (it would execute on clone).
            for k, v in props.items():
                if v is None:
                    continue
                if isinstance(v, (dict, list)):
                    if not is_global and _contains_cmd(v):
                        cfg["warnings"].append(
                            f'properties.{k} in {path}: a "cmd" source is only allowed in '
                            "~/.engram/config.json — ignored (a cloned repo must not run commands)"
                        )
                        continue
                    cfg["properties"][k] = v
                else:
                    cfg["properties"][k] = str(v)
        if isinstance(part.get("search"), dict):
            cfg["search"] = part["search"]
    return cfg
