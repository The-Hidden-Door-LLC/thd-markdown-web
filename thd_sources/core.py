from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import os
import re
import stat
from urllib.parse import quote, unquote, urlsplit

MAX_BYTES = 16 * 1024 * 1024
MAX_ADDRESS = 8192


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Source:
    provider: str
    uri: str
    path: str
    bucket: str = ""

    def describe(self) -> dict:
        return asdict(self)


_PROVIDERS = (
    {"id": "local", "label": "This computer", "scheme": "file",
     "state": "available", "can_read": True, "can_write": False},
    {"id": "gcs", "label": "Google Cloud Storage", "scheme": "gs",
     "state": "planned", "can_read": False, "can_write": False},
    {"id": "dropbox", "label": "Dropbox", "scheme": "dropbox",
     "state": "planned", "can_read": False, "can_write": False},
    {"id": "s3", "label": "AWS S3", "scheme": "s3",
     "state": "stub", "can_read": False, "can_write": False},
)


def providers() -> list[dict]:
    from .remote import gcloud_path, dropbox_path
    result = [dict(provider) for provider in _PROVIDERS]
    for provider, executable in ((result[1], gcloud_path()), (result[2], dropbox_path())):
        provider.update(state="available" if executable else "not_installed", can_read=bool(executable))
    return result


def _invalid(message: str = "Use an absolute file path, file://, gs://, dropbox:///, or s3:// address."):
    raise SourceError("invalid_source", message)


def _check_text(value: str):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _invalid("Source addresses cannot contain control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid("Source addresses must be valid UTF-8.")


def _source(provider: str, uri: str, path: str, bucket: str = "") -> Source:
    # A returned URI must remain usable by the same resolver after encoding.
    if len(uri) > MAX_ADDRESS:
        _invalid("The encoded source address exceeds 8192 characters.")
    return Source(provider, uri, path, bucket)


def resolve(address: str) -> Source:
    if not isinstance(address, str) or not address or len(address) > MAX_ADDRESS:
        _invalid()
    _check_text(address)
    if address.startswith("/"):
        return _source("local", "file://" + quote(address, safe="/"), address)
    if re.search(r"%(?![0-9a-fA-F]{2})", address):
        _invalid("Percent escapes must contain two hexadecimal digits.")
    try:
        parts = urlsplit(address)
        path = unquote(parts.path, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        _invalid()
    _check_text(path)
    if "?" in address or "#" in address:
        _invalid("Encode literal ? and # characters in paths; query strings and fragments are not supported.")
    scheme = parts.scheme.lower()
    if scheme not in {"file", "gs", "dropbox", "s3"}:
        raise SourceError("unsupported_provider", "That source provider is not supported.")
    if scheme in {"file", "dropbox"}:
        if parts.netloc or not path.startswith("/"):
            _invalid("Use file:///absolute/path or dropbox:///folder/file.")
        provider = "local" if scheme == "file" else "dropbox"
        return _source(provider, scheme + "://" + quote(path, safe="/"), path)
    # Keep object keys literal: a/../b is a different remote object from b.
    bucket = parts.netloc
    bucket_pattern = r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]" if scheme == "s3" else r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]"
    if not re.fullmatch(bucket_pattern, bucket):
        _invalid("Supply a bucket name without credentials, ports, or account information.")
    key = path[1:] if path.startswith("/") else path
    uri = scheme + "://" + bucket + ("/" + quote(key, safe="/") if path else "")
    return _source("gcs" if scheme == "gs" else "s3", uri, key, bucket)


def _read_local(source: Source, limit: int) -> bytes:
    descriptor = None
    try:
        # O_NONBLOCK prevents a selected FIFO from hanging before fstat rejects it.
        descriptor = os.open(source.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceError("not_a_file", "Choose a regular file.")
        if before.st_size > limit:
            raise SourceError("too_large", "The file exceeds the requested size limit.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(data) > limit:
            raise SourceError("too_large", "The file exceeds the requested size limit.")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SourceError("source_changed", "The file changed while loading. Try again.")
        return data
    except FileNotFoundError:
        raise SourceError("not_found", "The selected file no longer exists.") from None
    except PermissionError:
        raise SourceError("access_denied", "This process cannot read the selected file.") from None
    except OSError:
        raise SourceError("read_failed", "The selected file could not be read.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read(address: str, max_bytes: int = MAX_BYTES) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise SourceError("invalid_limit", "Choose a byte limit between 1 and 16777216.")
    source = resolve(address)
    provider = next(item for item in providers() if item["id"] == source.provider)
    if not provider["can_read"]:
        raise SourceError("provider_not_connected", provider["label"] + " is not connected in this build.")
    if source.provider == "local":
        data = _read_local(source, max_bytes)
    else:
        from .remote import read_gcs, read_dropbox
        data = (read_gcs if source.provider == "gcs" else read_dropbox)(source, max_bytes)
    return {
        "source": source.describe(),
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "cached": False,
    }
