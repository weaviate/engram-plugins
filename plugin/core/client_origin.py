import glob
import json
import os

_PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _plugin_version(manifest_dir):
    for manifest in sorted(glob.glob(os.path.join(_PLUGIN_ROOT, manifest_dir, "plugin.json"))):
        try:
            with open(manifest) as f:
                return json.load(f).get("version", "unknown")
        except Exception:
            continue
    return "unknown"


def client_origin_header(assistant=None):
    """The migration CLI runs under an assistant it cannot identify, hence the fallback."""
    name = assistant.NAME if assistant else "engram"
    manifest_dir = assistant.MANIFEST_DIR if assistant else ".*-plugin"
    return {"X-Engram-Client": f"{name}-plugin/{_plugin_version(manifest_dir)}"}
