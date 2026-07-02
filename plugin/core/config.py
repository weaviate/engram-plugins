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


def load_config(cwd):
    """Merge config from the global ~/.engram/config.json and the current directory's .engram.json (cwd
    overrides). `properties` merge key-wise; `search` takes the deepest defined block.

    Dynamic sources ({"from"|"cmd"} objects and cascades) are honored ONLY from the global config,
    which is user-owned. A committed per-directory .engram.json is limited to static literal values
    — a repo you clone must never be able to run a {"cmd"} on your machine via a hook. A dynamic
    value found in a local file is dropped and reported in `warnings`."""
    cfg = {"properties": {}, "search": {}, "warnings": []}
    for i, path in enumerate(_config_chain(cwd)):
        part = _read_json(path)
        is_global = i == 0  # first entry is the global config; the rest are local/committed
        props = part.get("properties")
        if isinstance(props, dict):
            # JSON shape carries the meaning: string = literal, {"from"|"cmd"} = dynamic source,
            # list = cascade. Locally only literals are allowed (dynamic would execute on clone).
            for k, v in props.items():
                if v is None:
                    continue
                if isinstance(v, (dict, list)):
                    if is_global:
                        cfg["properties"][k] = v
                    else:
                        cfg["warnings"].append(
                            f"properties.{k} in {path}: dynamic sources are only allowed in "
                            "~/.engram/config.json — ignored (use a literal here, or move it to global)"
                        )
                else:
                    cfg["properties"][k] = str(v)
        if isinstance(part.get("search"), dict):
            cfg["search"] = part["search"]
    return cfg
