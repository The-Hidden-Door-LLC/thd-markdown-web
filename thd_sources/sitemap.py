"""Bounded, read-only discovery of a site's published HTTPS sitemap."""

from collections import deque
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
from xml.parsers import expat
import zlib

from .core import SourceError

MAX_URLS = 5000
MAX_MAPS = 16
MAX_BODY = 2 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
TIME_BUDGET = 25
USER_AGENT = "THD-Markdown/0.2 (sitemap reader)"


def https_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise SourceError("invalid_url", "Enter an HTTPS website address.")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SourceError("invalid_url", "Website addresses cannot contain control characters.")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError()
        port = parts.port
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if any(char in host for char in " %\\/"):
            raise ValueError()
        if ":" in host:
            host = "[" + host + "]"
        authority = host + (":" + str(port) if port and port != 443 else "")
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="%/?@:!$&'()*+,;=-._~")
        fragment = quote(parts.fragment, safe="%/?@:!$&'()*+,;=-._~")
        return urlunsplit(("https", authority, path, query, fragment))
    except (ValueError, UnicodeError):
        raise SourceError("invalid_url", "Use HTTPS without embedded usernames or passwords.") from None


def origin(url):
    parts = urlsplit(https_url(url))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def site_url(value, site, base):
    """Sitemap links may use legacy HTTP; keep discovery on this HTTPS origin."""
    try:
        candidate = urljoin(base, value.strip())
        if urlsplit(candidate).scheme == "http":
            candidate = "https:" + candidate[5:]
        candidate = https_url(candidate)
        if origin(candidate) != site:
            return None
        return candidate.split("#", 1)[0]
    except (SourceError, ValueError, AttributeError):
        return None


class SiteRedirects(HTTPRedirectHandler):
    def __init__(self, site):
        self.site = site

    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = site_url(new_url, self.site, request.full_url)
        if not target:
            raise SourceError("outside_site", "Sitemap discovery stays on the current HTTPS site.")
        return super().redirect_request(request, fp, code, message, headers, target)


def fetch(url, site, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceError("timeout", "Sitemap discovery reached its time limit.")
    opener = build_opener(SiteRedirects(site))
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,text/plain,*/*;q=0.1"})
        with opener.open(request, timeout=min(5, remaining)) as response:
            chunks, count = [], 0
            while count <= MAX_BODY:
                if time.monotonic() >= deadline:
                    raise SourceError("timeout", "Sitemap discovery reached its time limit.")
                chunk = response.read1(min(65536, MAX_BODY + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            data = b"".join(chunks)
        if len(data) > MAX_BODY:
            raise SourceError("too_large", "A sitemap exceeds the 2 MiB discovery limit.")
        if data.startswith(b"\x1f\x8b"):
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = inflater.decompress(data, MAX_BODY + 1)
            if len(data) > MAX_BODY or inflater.unconsumed_tail:
                raise SourceError("too_large", "An expanded sitemap exceeds the 2 MiB discovery limit.")
            if not inflater.eof or inflater.unused_data:
                raise SourceError("invalid_sitemap", "The compressed sitemap is incomplete or has extra streams.")
        return data
    except HTTPError as error:
        code = "not_found" if error.code == 404 else "access_denied" if error.code in (401, 403) else "read_failed"
        raise SourceError(code, "The site did not provide this sitemap.") from None
    except (URLError, OSError, socket.timeout):
        raise SourceError("connection_failed", "Could not reach the site's sitemap.") from None
    except zlib.error:
        raise SourceError("invalid_sitemap", "The site's compressed sitemap could not be decoded.") from None


def parse_map(data):
    if data.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html")):
        raise SourceError("not_sitemap", "This endpoint returned a web page, not a sitemap.")
    def reject_dtd(*_):
        raise SourceError("invalid_sitemap", "Sitemaps with document type declarations are unsupported.")
    try:
        validation = expat.ParserCreate()
        validation.StartDoctypeDeclHandler = reject_dtd
        validation.EntityDeclHandler = reject_dtd
        validation.Parse(data, True)
        root = ElementTree.fromstring(data)
    except (expat.ExpatError, ElementTree.ParseError, ValueError):
        raise SourceError("invalid_sitemap", "The site did not return a valid XML sitemap.") from None
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("urlset", "sitemapindex"):
        raise SourceError("invalid_sitemap", "Expected an XML sitemap or sitemap index.")
    expected = "url" if kind == "urlset" else "sitemap"
    result = []
    for item in root:
        if item.tag.rsplit("}", 1)[-1] != expected:
            continue
        values = {child.tag.rsplit("}", 1)[-1]: child.text for child in item if "}" not in child.tag or child.tag.startswith("{http://www.sitemaps.org/schemas/sitemap/0.9}")}
        address = values.get("loc")
        if address:
            result.append({"url": address, "lastmod": (values.get("lastmod") or "")[:64]})
    return kind, result


def discover(address, known_sitemaps=()):
    site = origin(address)
    deadline = time.monotonic() + TIME_BUDGET
    queue = deque()
    warnings = []
    checks = []
    uncertain = False
    external = False
    total = 0
    try:
        robots = fetch(site + "/robots.txt", site, deadline)
        total += len(robots)
        for line in robots.decode("utf-8-sig", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "sitemap":
                url = site_url(value.split("#", 1)[0], site, site + "/")
                if url:
                    queue.append(url)
                else:
                    external = True
                    warnings.append("A sitemap on another origin was skipped.")
    except SourceError as error:
        uncertain = error.code != "not_found"
    requested = https_url(address)
    if urlsplit(requested).path.lower().endswith((".xml", ".xml.gz")):
        queue.appendleft(requested)
    for known in known_sitemaps[:MAX_MAPS]:
        target = site_url(known, site, site + "/")
        if target:
            queue.append(target)
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
        queue.append(site + path)
    visited, pages, documents = set(), {}, []
    truncated = False
    while queue:
        if len(visited) >= MAX_MAPS or len(pages) >= MAX_URLS or total >= MAX_TOTAL or time.monotonic() >= deadline:
            truncated = True
            break
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            data = fetch(url, site, deadline)
            total += len(data)
            if total > MAX_TOTAL:
                truncated = True
                break
            kind, entries = parse_map(data)
            documents.append(url)
        except SourceError as error:
            checks.append({"url": urlunsplit((*urlsplit(url)[:3], "", "")), "outcome": error.code})
            if error.code not in ("not_found", "not_sitemap"):
                uncertain = True
                warnings.append(error.code + ": " + str(error))
            continue
        for entry in entries:
            target = site_url(entry["url"], site, url)
            if not target:
                continue
            if kind == "sitemapindex":
                if len(queue) < MAX_MAPS * 4:
                    queue.append(target)
                else:
                    truncated = True
            else:
                pages.setdefault(target, {"url": target, "lastmod": entry["lastmod"]})
                if len(pages) >= MAX_URLS:
                    truncated = True
                    break
    return {"site": site, "pages": sorted(pages.values(), key=lambda item: item["url"]),
            "sitemaps": documents, "truncated": truncated, "warnings": list(dict.fromkeys(warnings))[:8],
            "checks": checks, "missing_sitemap": not documents and not uncertain and not external and not truncated,
            "limits": {"pages": MAX_URLS, "sitemaps": MAX_MAPS, "bytes_per_sitemap": MAX_BODY, "seconds": TIME_BUDGET}}
