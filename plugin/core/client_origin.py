import json
import os

_MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", ".claude-plugin", "plugin.json"
)


def _platform():
    return "claude"


def _plugin_version():
    try:
        with open(_MANIFEST) as f:
            return json.load(f).get("version", "unknown")
    except Exception:
        return "unknown"


def client_origin_header():
    return {"X-Engram-Client": f"{_platform()}-plugin/{_plugin_version()}"}
