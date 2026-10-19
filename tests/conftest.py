import os
import tempfile
from pathlib import Path

# The modules read their paths from the environment at import, so point them at
# a scratch directory before any test imports them.
_tmp = Path(tempfile.mkdtemp(prefix="colony-test-"))
(_tmp / "vault").mkdir()
for key, value in {
    "INDEX_DB": _tmp / "index.db",
    "AUTH_DB": _tmp / "auth.db",
    "SCOUT_DB": _tmp / "scout.db",
    "MIRROR_DIR": _tmp / "drive",
    "VAULT_DIR": _tmp / "vault",
    "AUDIT_LOG": _tmp / "audit.jsonl",
    "OLLAMA_URL": "http://127.0.0.1:9",
}.items():
    os.environ[key] = str(value)
os.environ.pop("WAKE_MQTT_HOST", None)
