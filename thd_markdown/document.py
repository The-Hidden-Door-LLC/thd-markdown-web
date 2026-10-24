"""Document identity, strict decoding, and conflict-aware local saves."""

import hashlib
import os
import stat
from pathlib import Path
import tempfile

MAX_DOCUMENT = 2 * 1024 * 1024


def decode_document(data):
    if len(data) > MAX_DOCUMENT:
        raise ValueError("Markdown documents must be 2 MiB or smaller.")
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("This file contains binary data. Choose a UTF-8 text document.")
    return text


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_document(path, text, expected_hash=None, create_only=False):
    """Atomically replace a local file after checking for external changes.

    The comparison detects ordinary concurrent edits, but does not lock out a
    writer racing between the comparison and rename. Save As uses a hard link
    to refuse collisions atomically.
    """
    path = Path(path).absolute()
    data = text.encode("utf-8")
    if len(data) > MAX_DOCUMENT:
        raise ValueError("Markdown documents must be 2 MiB or smaller.")
    if path.is_symlink():
        raise ValueError("Saving over a symbolic link is disabled. Save a new copy instead.")
    if expected_hash is not None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("The original is no longer a regular file. Save a new copy instead.")
                current = stream.read(MAX_DOCUMENT + 1)
        except FileNotFoundError:
            raise ValueError("The original file was removed. Save a new copy instead.") from None
        if digest(current) != expected_hash:
            raise ValueError("The file changed outside the editor. Save a new copy to keep both versions.")
    descriptor, temporary = tempfile.mkstemp(prefix=".thd-markdown-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            os.link(temporary, path)
        else:
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        return digest(data)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
