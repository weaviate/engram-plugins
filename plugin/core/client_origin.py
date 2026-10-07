import json
import os

_MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", ".claude-plugin", "plugin.json"
)


def _plugin_version():
    try:
        with open(_MANIFEST) as f:
            return json.load(f).get("version", "unknown")
    except Exception:
        return "unknown"


def client_origin_header(assistant: str = "claude") -> dict[str, str]:
    """The assistant whose session made the call. The default covers the migration CLI and the
    schema fetch, which run outside any one assistant's hooks."""
    return {"X-Engram-Client": f"{assistant}-plugin/{_plugin_version()}"}
