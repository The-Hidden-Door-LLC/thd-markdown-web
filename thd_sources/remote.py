"""Read-only adapters using existing machine credentials and provider boundaries."""

import json
import os
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def _error(code, message):
    from .core import SourceError
    raise SourceError(code, message)


def gcloud_path():
    return shutil.which("gcloud")


def dropbox_path():
    return "configured" if os.environ.get("DROPBOX_ACCESS_TOKEN") else None


def _run(command, payload=None, timeout=30):
    try:
        return subprocess.run(command, input=payload, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=True).stdout
    except subprocess.TimeoutExpired:
        _error("timeout", "The source service took too long to respond. Try again.")
    except (OSError, subprocess.CalledProcessError):
        _error("auth_required", "The source service is unavailable or its existing machine sign-in needs attention.")


def _google_get(url, token, limit):
    try:
        with urlopen(Request(url, headers={"Authorization": "Bearer " + token}), timeout=25) as response:
            length = int(response.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError()
            if length > limit:
                _error("too_large", "The object exceeds the requested size limit.")
            data = response.read(limit + 1)
        if len(data) > limit:
            _error("too_large", "The object exceeds the requested size limit.")
        return data
    except HTTPError as error:
        code = {401: "auth_required", 403: "access_denied", 404: "not_found", 412: "source_changed"}.get(error.code, "read_failed")
        message = {
            "auth_required": "Google's existing sign-in needs attention.",
            "access_denied": "The current Google identity cannot read this object.",
            "not_found": "The selected Google object was not found.",
            "source_changed": "The object changed while loading. Try again.",
        }.get(code, "Google Cloud Storage could not read this object.")
        _error(code, message)
    except (URLError, TimeoutError, OSError):
        _error("connection_failed", "Could not reach Google Cloud Storage.")
    except ValueError:
        _error("invalid_response", "Google returned invalid response headers.")


def read_gcs(source, limit):
    executable = gcloud_path()
    if not executable:
        _error("provider_not_connected", "Google Cloud CLI is not installed.")
    if not source.path:
        _error("not_a_file", "Choose an object path inside the bucket.")
    # Use the configured gcloud identity without switching accounts or writing credentials.
    token = _run([executable, "auth", "print-access-token", "--quiet"]).strip()
    if not token or any(c.isspace() for c in token):
        _error("auth_required", "Google's existing sign-in needs attention.")
    endpoint = "https://storage.googleapis.com/storage/v1/b/" + quote(source.bucket, safe="") + "/o/" + quote(source.path, safe="")
    try:
        metadata = json.loads(_google_get(endpoint + "?fields=size,generation", token, 65536))
        if int(metadata["size"]) > limit:
            _error("too_large", "The object exceeds the requested size limit.")
        generation = str(metadata["generation"])
        if not generation.isdecimal():
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        _error("invalid_response", "Google returned invalid object metadata.")
    # Pin the generation so a metadata/content race cannot silently select another revision.
    payload = _google_get(endpoint + "?" + urlencode({"alt": "media", "generation": generation}), token, limit)
    if len(payload) != int(metadata["size"]):
        _error("source_changed", "The returned object size differs from its metadata. Try again.")
    return payload



def read_dropbox(source, limit):
    token = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
    if not token or any(c.isspace() for c in token):
        _error("auth_required", "Configure a Dropbox access token in your environment.")
    arguments = json.dumps({"path":source.path}, ensure_ascii=True)
    request = Request("https://content.dropboxapi.com/2/files/download", method="POST", headers={"Authorization":"Bearer "+token, "Dropbox-API-Arg":arguments})
    try:
        with urlopen(request, timeout=25) as response:
            metadata = json.loads(response.headers.get("Dropbox-API-Result", "{}"))
            size = metadata.get("size")
            if type(size) is not int or size < 0:
                _error("invalid_response", "Dropbox returned invalid file metadata.")
            if size > limit:
                _error("too_large", "The Dropbox file exceeds the requested size limit.")
            data = response.read(limit+1)
        if len(data) > limit:
            _error("too_large", "The Dropbox file exceeds the requested size limit.")
        if len(data) != size:
            _error("source_changed", "The returned file size differs from its metadata.")
        return data
    except HTTPError as error:
        code = "auth_required" if error.code == 401 else "access_denied" if error.code == 403 else "read_failed"
        error.close()
        _error(code, "Dropbox could not read this file. Check its address and your authorization.")
    except (ValueError, TypeError, AttributeError):
        _error("invalid_response", "Dropbox returned invalid file metadata.")
    except (URLError, TimeoutError, OSError):
        _error("connection_failed", "Could not reach Dropbox.")
