"""The editor talks to the Sources child process, never directly to providers."""

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .document import MAX_DOCUMENT, decode_document

SIDECAR = Path(__file__).resolve().parents[1]


def request(method, source=None, **options):
    payload = {"version": 1, "id": 1, "method": method, **options}
    if source is not None:
        payload.update(source=source, max_bytes=MAX_DOCUMENT)
    try:
        response = subprocess.run(
            [str(SIDECAR / ".venv/bin/python") if (SIDECAR / ".venv/bin/python").exists() else sys.executable, "-m", "thd_sources"], cwd=SIDECAR,
            input=json.dumps(payload) + "\n", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=85, check=True,
        )
        result = json.loads(response.stdout)
        if not result["ok"]:
            raise ValueError(result["error"]["message"])
        return result["result"]
    except (OSError, subprocess.SubprocessError, KeyError, json.JSONDecodeError):
        raise ValueError("Sources could not respond. Check the sidecar installation and connection.") from None


def load(source):
    result = request("read", source)
    data = base64.b64decode(result["content"], validate=True)
    if len(data) != result["size"] or hashlib.sha256(data).hexdigest() != result["sha256"]:
        raise ValueError("The loaded document failed its integrity check.")
    return decode_document(data), result
