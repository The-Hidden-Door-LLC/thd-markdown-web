"""Private, atomic JSON storage for cached Markdown and discovery evidence."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def now():
    return datetime.now(timezone.utc).isoformat()


def cache_path(kind, key):
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thd-markdown/web" / kind
    return root / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def data_root():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "thd-markdown/directory"


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".thd-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            return None
        result = json.loads(data)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, RecursionError):
        return None
