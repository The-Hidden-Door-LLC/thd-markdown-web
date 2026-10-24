"""Versioned JSON-lines transport. No listening port or background service."""

import json
import sys

from .core import MAX_BYTES, SourceError, providers, read, resolve

MAX_REQUEST = 65536


def dispatch(request):
    request_id = None
    try:
        if not isinstance(request, dict):
            raise SourceError("invalid_request", "A request must be a JSON object.")
        request_id = request.get("id")
        if type(request_id) not in (str, int, type(None)):
            request_id = None
            raise SourceError("invalid_request", "Use a string, integer, or null request ID.")
        if type(request.get("version")) is not int or request["version"] != 1:
            raise SourceError("unsupported_version", "This sidecar supports protocol version 1.")
        method = request.get("method")
        if method == "providers":
            result = providers()
        elif method == "resolve":
            result = resolve(request.get("source")).describe()
        elif method == "read":
            result = read(request.get("source"), request.get("max_bytes", MAX_BYTES))
        elif method == "sitemap":
            from .web import get_site
            result = get_site(request.get("source"), request.get("refresh", False))
        elif method == "web":
            from .web import get_page
            result = get_page(request.get("source"), request.get("refresh", False))
        elif method == "directory":
            from .directory import export
            result = export()
        else:
            raise SourceError("unsupported_method", "Use providers, resolve, read, sitemap, web, or directory.")
        return {"version": 1, "id": request_id, "ok": True, "result": result}
    except SourceError as error:
        return {"version": 1, "id": request_id, "ok": False,
                "error": {"code": error.code, "message": str(error)}}


def main():
    while line := sys.stdin.buffer.readline(MAX_REQUEST + 1):
        if len(line) > MAX_REQUEST:
            response = {"version": 1, "id": None, "ok": False,
                        "error": {"code": "request_too_large", "message": "The request exceeds 64 KiB."}}
            # Discard the rest of this single request, preserving the next line.
            while not line.endswith(b"\n") and line:
                line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        else:
            try:
                request = json.loads(line.decode("utf-8", errors="strict"))
                response = dispatch(request)
            except (ValueError, RecursionError):
                response = {"version": 1, "id": None, "ok": False,
                            "error": {"code": "invalid_json", "message": "The request is not valid JSON."}}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
