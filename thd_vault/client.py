"""Bounded local document bridge; the Vault app alone owns keys and cloud access."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import uuid

MAX_BYTES = 16 * 1024 * 1024
MAX_FRAME = 24 * 1024 * 1024
MEDIA_TYPES = {"text/markdown", "text/plain", "image/png", "image/jpeg", "image/webp"}


class VaultError(ValueError):
    def __init__(self, message, code="vault_error"):
        super().__init__(message)
        self.code = code


def socket_path():
    if os.environ.get("HDV_SOCKET"):
        return Path(os.environ["HDV_SOCKET"])
    if os.environ.get("HDV_USER_DATA"):
        return Path(os.environ["HDV_USER_DATA"]) / "hdv.sock"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Hidden Door Vault/hdv.sock"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Hidden Door Vault/hdv.sock"


def call(action, payload=None):
    path = socket_path()
    try:
        info = path.lstat()
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise VaultError("The Vault socket must be in a directory owned by you and not writable by others.", "unsafe_socket")
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise VaultError("The Vault connection must be an owner-only local socket.", "unsafe_socket")
        request = {"id":1, "action":action, "payload":{**(payload or {}), "autoUnlock":False}}
        wire = json.dumps(request, ensure_ascii=True).encode() + b"\n"
        if len(wire) > MAX_FRAME:
            raise VaultError("The Vault request exceeds its document limit.", "too_large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(45)
            channel.connect(str(path))
            if sys.platform.startswith("linux") and hasattr(socket,"SO_PEERCRED"):
                _, peer_uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize("3i")))
                if peer_uid != os.getuid():
                    raise VaultError("The Vault connection belongs to another user.", "unsafe_socket")
            elif sys.platform == "darwin":
                try:
                    library = ctypes.CDLL(None, use_errno=True)
                    check_peer = library.getpeereid
                    check_peer.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
                    check_peer.restype = ctypes.c_int
                    peer_uid, peer_gid = ctypes.c_uint(), ctypes.c_uint()
                    if check_peer(channel.fileno(), ctypes.byref(peer_uid), ctypes.byref(peer_gid)) != 0:
                        raise OSError(ctypes.get_errno(), "Peer ownership check failed")
                    if peer_uid.value != os.getuid():
                        raise VaultError("The Vault connection belongs to another user.", "unsafe_socket")
                except (AttributeError, OSError):
                    raise VaultError("A verified Vault peer is required on this platform.", "unsafe_socket") from None
            else:
                raise VaultError("A verified Vault peer is required on this platform.", "unsafe_socket")
            channel.sendall(wire)
            chunks, size = [], 0
            while size <= MAX_FRAME:
                chunk = channel.recv(min(65536, MAX_FRAME + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if b"\n" in chunk:
                    break
        if size > MAX_FRAME:
            raise VaultError("The Vault returned too much data.", "invalid_response")
        response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
        if not isinstance(response, dict) or response.get("id") != 1 or type(response.get("ok")) is not bool:
            raise VaultError("Save outcome is unknown. Check the Vault before trying again." if action == "documents-write" else "The Vault returned an invalid response.", "unknown_outcome" if action == "documents-write" else "invalid_response")
        if not response["ok"]:
            message = str(response.get("error", "")).lower()
            error_code = response.get("code")
            typed = {
                "commit_unconfirmed": ("Save outcome is unknown. Refresh the Vault before trying again.", "unknown_outcome"),
                "revision_conflict": ("The Vault document changed. Reopen it or save a new copy.", "conflict"),
                "vault_locked": ("Open Hidden Door Vault and unlock it first.", "locked"),
                "storage_not_configured": ("Configure document storage in the Vault app first.", "not_configured"),
                "auth_required": ("The Vault's Google sign-in needs attention.", "auth_required"),
                "access_denied": ("The Vault's Google account cannot access its attachment storage.", "access_denied"),
            }
            if isinstance(error_code,str) and error_code in typed:
                raise VaultError(*typed[error_code])
            if "lock" in message:
                raise VaultError("Open Hidden Door Vault and unlock it first.", "locked")
            if action == "documents-write" and any(word in message for word in ("unconfirmed", "unknown outcome", "outcome is unknown")):
                raise VaultError("Save outcome is unknown. Refresh the Vault before trying again.", "unknown_outcome")
            if any(word in message for word in ("conflict", "revision", "changed")):
                raise VaultError("The Vault document changed. Reopen it or save a new copy.", "conflict")
            if "not configured" in message or ("configure" in message and "bucket" in message):
                raise VaultError("Configure document storage in the Vault app first.", "not_configured")
            raise VaultError("The Vault could not complete this document request.")
        return response.get("data")
    except (FileNotFoundError, ConnectionRefusedError):
        raise VaultError("Start Hidden Door Vault and enable its local bridge.", "not_running") from None
    except (TimeoutError, OSError, json.JSONDecodeError, UnicodeError):
        text = "Save outcome is unknown. Check the Vault before trying again." if action == "documents-write" else "The Vault connection did not complete."
        raise VaultError(text, "unknown_outcome" if action == "documents-write" else "connection_failed") from None


def metadata(value):
    if not isinstance(value, dict):
        raise VaultError("Invalid Vault document metadata.", "invalid_response")
    try:
        if str(uuid.UUID(value["id"], version=4)) != value["id"]:
            raise ValueError()
        if not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 200:
            raise ValueError()
        filename = value["filename"]
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or filename in (".", "..") or any(c in filename for c in ("/", "\\", "\x00")):
            raise ValueError()
        if value["mediaType"] not in MEDIA_TYPES or type(value["size"]) is not int or not 0 <= value["size"] <= MAX_BYTES:
            raise ValueError()
        if not isinstance(value["revision"], str) or not 1 <= len(value["revision"]) <= 256:
            raise ValueError()
        if not isinstance(value["sha256"], str) or len(value["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in value["sha256"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise VaultError("Invalid Vault document metadata.", "invalid_response") from None
    return {key:value[key] for key in ("id", "title", "filename", "mediaType", "size", "sha256", "revision")}


def ready():
    state = call("status")
    if not isinstance(state, dict):
        raise VaultError("The Vault returned an invalid status.", "invalid_response")
    if not state.get("unlocked"):
        raise VaultError("Open Hidden Door Vault and unlock it first.", "locked")
    capability = state.get("documents", {})
    if not isinstance(capability, dict) or capability.get("supported") is not True:
        raise VaultError("Update or configure the Vault app to enable document storage.", "unsupported")


def list_documents(search=""):
    if not isinstance(search, str) or len(search) > 200:
        raise VaultError("Use a search of 200 characters or fewer.")
    ready()
    values = call("documents-list", {"search":search})
    if not isinstance(values, list) or len(values) > 1000:
        raise VaultError("Invalid Vault document list.", "invalid_response")
    return [metadata(value) for value in values]


def read_document(identifier):
    ready()
    result = call("documents-read", {"id":identifier})
    info = metadata(result)
    if info["id"] != identifier:
        raise VaultError("The Vault returned a different document.", "invalid_response")
    try:
        data = base64.b64decode(result["contentBase64"], validate=True)
    except (ValueError, TypeError, KeyError):
        raise VaultError("Invalid Vault document bytes.", "invalid_response") from None
    if len(data) != info["size"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise VaultError("The Vault document failed its integrity check.", "invalid_response")
    return info, data


def write_document(data, title, filename, media_type, previous=None):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES or media_type not in MEDIA_TYPES:
        raise VaultError("Choose a supported document or image up to 16 MiB.", "too_large")
    if not isinstance(title,str) or not 1 <= len(title.strip()) <= 200 or not isinstance(filename,str) or not 1 <= len(filename.strip()) <= 255:
        raise VaultError("Choose a valid title and filename.")
    title, filename = title.strip(), filename.strip()
    ready()
    payload = {"title":title, "filename":filename, "mediaType":media_type, "contentBase64":base64.b64encode(data).decode()}
    if previous is not None:
        info = metadata(previous)
        payload.update(id=info["id"], expectedRevision=info["revision"])
    try:
        result = metadata(call("documents-write", payload))
    except VaultError as error:
        if error.code == "invalid_response":
            raise VaultError("Save outcome is unknown. Check the Vault before trying again.", "unknown_outcome") from None
        raise
    if previous is not None and (result["id"] != payload["id"] or result["revision"] == payload["expectedRevision"]):
        raise VaultError("Save outcome is unknown: the Vault returned an unexpected document revision.", "unknown_outcome")
    if result["title"] != title or result["filename"] != filename or result["mediaType"] != media_type:
        raise VaultError("Save outcome is unknown: the Vault returned different document metadata.", "unknown_outcome")
    if result["size"] != len(data) or result["sha256"] != hashlib.sha256(data).hexdigest():
        raise VaultError("Save outcome is unknown: the Vault receipt did not match the document.", "unknown_outcome")
    return result


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_FRAME+1)
        if len(raw) > MAX_FRAME:
            raise VaultError("Request exceeds the document limit.", "too_large")
        request = json.loads(raw)
        action = request.get("action")
        payload = request.get("payload", {})
        if action == "status":
            ready()
            data = {"unlocked":True, "documents":{"supported":True}}
        elif action == "documents-list":
            data = list_documents(payload.get("search", ""))
        elif action == "documents-read":
            info, body = read_document(payload.get("id"))
            data = {**info, "contentBase64":base64.b64encode(body).decode()}
        elif action == "documents-write":
            previous = payload.get("previous")
            if previous is None and "id" in payload:
                # Native clients can send a previously validated metadata object.
                previous = payload.get("metadata")
                if not previous:
                    raise VaultError("Use the complete previous metadata when updating.")
            body = base64.b64decode(payload.get("contentBase64", ""), validate=True)
            data = write_document(body, payload.get("title"), payload.get("filename"), payload.get("mediaType"), previous)
        else:
            raise VaultError("Unsupported document bridge action.")
        result = {"ok":True, "data":data}
    except VaultError as error:
        result = {"ok":False, "error":str(error), "code":error.code}
    except (ValueError, TypeError, AttributeError, KeyError):
        result = {"ok":False, "error":"Invalid document request.", "code":"invalid_request"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True)+"\n")


if __name__ == "__main__":
    main()
