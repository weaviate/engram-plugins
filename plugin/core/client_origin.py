import glob
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # tests load this module by path, where a relative import has no package
    from .assistant import Assistant

_PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _plugin_version(manifest_dir: str) -> str:
    for manifest in sorted(glob.glob(os.path.join(_PLUGIN_ROOT, manifest_dir, "plugin.json"))):
        try:
            with open(manifest) as f:
                return str(json.load(f).get("version", "unknown"))
        except Exception:
            continue
    return "unknown"


def client_origin_header(assistant: "Assistant | None" = None) -> dict[str, str]:
    """The migration CLI runs under an assistant it cannot identify, hence the fallback."""
    name = assistant.NAME if assistant else "engram"
    manifest_dir = assistant.MANIFEST_DIR if assistant else ".*-plugin"
    return {"X-Engram-Client": f"{name}-plugin/{_plugin_version(manifest_dir)}"}
