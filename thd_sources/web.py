"""HTTPS to image-free Markdown, with explicit local page and sitemap caching."""

import hashlib
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import SourceError
from .sitemap import https_url, discover, MAX_BODY
from .store import cache_path, read_json, write_json, now


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return super().redirect_request(request, fp, code, message, headers, https_url(new_url))


def fetch_page(url):
    try:
        opener = build_opener(HttpsRedirects())
        request = Request(url, headers={"User-Agent":"THD-Markdown/0.2 (text-only reader)", "Accept":"text/html,text/markdown,text/plain;q=0.9"})
        deadline = time.monotonic() + 20
        with opener.open(request, timeout=8) as response:
            content_type = response.headers.get_content_type()
            if content_type not in ("text/html", "application/xhtml+xml", "text/markdown", "text/plain"):
                raise SourceError("unsupported_content", "This reader accepts HTML, Markdown, and plain text pages. Images are not loaded.")
            encoding = response.headers.get_content_charset() or "utf-8"
            chunks, length = [], 0
            while length <= MAX_BODY:
                if time.monotonic() >= deadline:
                    raise SourceError("timeout", "The page exceeded its loading time limit.")
                chunk = response.read1(min(65536, MAX_BODY + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            data = b"".join(chunks)
            final_url = https_url(response.geturl())
        if len(data) > MAX_BODY:
            raise SourceError("too_large", "The page exceeds the 2 MiB reader limit.")
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeError):
            raise SourceError("invalid_encoding", "The page's declared text encoding could not be decoded.") from None
        return final_url, text, content_type
    except HTTPError as error:
        code = "not_found" if error.code == 404 else "access_denied" if error.code in (401,403) else "read_failed"
        raise SourceError(code, "The website did not provide this page.") from None
    except (URLError, OSError, socket.timeout):
        raise SourceError("connection_failed", "The HTTPS page could not be reached. Cached pages remain available.") from None


def convert_html(text, url):
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError:
        raise SourceError("converter_missing", "Install the Sources project's pinned converter requirements.") from None
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else urlsplit(url).netloc
    for tag in soup.find_all(("script", "style", "iframe", "object", "embed", "img", "picture", "svg", "canvas", "audio", "video", "form", "button", "input", "select", "textarea")):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in main.find_all(("nav", "header", "footer", "aside")):
        tag.decompose()
    for anchor in main.find_all("a", href=True):
        raw = anchor["href"]
        if raw.startswith("#"):
            continue
        target = urljoin(url, raw)
        if target.startswith("http://"):
            target = "https://" + target[7:]
        try:
            anchor["href"] = https_url(target)
        except SourceError:
            anchor.unwrap()
    markdown = markdownify(str(main), heading_style="ATX", bullets="-", strip=["img"], autolinks=False).strip()
    if not markdown.startswith("# "):
        markdown = "# " + title.replace("\n", " ") + "\n\n" + markdown
    return title, markdown + "\n"


def get_page(address, refresh=False):
    if type(refresh) is not bool:
        raise SourceError("invalid_request", "refresh must be true or false.")
    url = https_url(address).split("#", 1)[0]
    path = cache_path("pages", url)
    cached = read_json(path)
    valid = cached and cached.get("schema_version") == 1 and cached.get("requested_url") == url
    valid = valid and all(isinstance(cached.get(key), str) for key in ("url", "title", "markdown", "fetched_at", "sha256"))
    if valid:
        try:
            valid = https_url(cached["url"]) == cached["url"] and hashlib.sha256(cached["markdown"].encode()).hexdigest() == cached["sha256"]
        except SourceError:
            valid = False
    if valid and not refresh:
        return {**cached, "cached":True}
    final_url, text, content_type = fetch_page(url)
    if content_type in ("text/html", "application/xhtml+xml"):
        try:
            title, markdown = convert_html(text, final_url)
        except RecursionError:
            raise SourceError("conversion_failed", "This page's nesting exceeds the converter's supported depth.") from None
    else:
        title, markdown = urlsplit(final_url).path.rsplit("/", 1)[-1] or urlsplit(final_url).netloc, text
    if len(markdown.encode()) > MAX_BODY:
        raise SourceError("too_large", "The converted Markdown exceeds the 2 MiB reader limit.")
    result = {"schema_version":1, "url":final_url, "requested_url":url, "title":title, "markdown":markdown, "fetched_at":now(),
              "sha256":hashlib.sha256(markdown.encode()).hexdigest(), "cached":False,
              "limited_text":len(markdown.split()) < 20}
    write_json(path, result)
    if final_url != url:
        write_json(cache_path("pages", final_url), {**result, "requested_url":final_url})
    return result


def get_site(address, refresh=False):
    if type(refresh) is not bool:
        raise SourceError("invalid_request", "refresh must be true or false.")
    from .sitemap import origin
    from .directory import observe
    canonical = https_url(address)
    key = canonical if urlsplit(canonical).path.lower().endswith((".xml", ".xml.gz")) else origin(address)
    path = cache_path("sites", key)
    previous = read_json(path)
    valid = previous and previous.get("schema_version") == 1 and previous.get("lookup_key") == key and previous.get("site") == origin(address)
    valid = valid and all(isinstance(previous.get(field), list) for field in ("pages", "sitemaps", "checks", "warnings"))
    valid = valid and isinstance(previous.get("checked_at"), str) and type(previous.get("missing_sitemap")) is bool and type(previous.get("truncated")) is bool
    if valid:
        from .sitemap import site_url
        valid = all(isinstance(page, dict) and isinstance(page.get("url"), str) and isinstance(page.get("lastmod"), str)
                    and site_url(page["url"], previous["site"], previous["site"]) == page["url"] for page in previous["pages"])
        valid = valid and all(isinstance(value, str) for value in previous["warnings"])
        valid = valid and all(isinstance(value, str) and site_url(value, previous["site"], previous["site"]) == value for value in previous["sitemaps"])
    if valid and not refresh:
        return {**previous, "cached":True}
    root_key = origin(address)
    root_record = read_json(cache_path("sites", root_key))
    known = root_record.get("sitemaps", []) if root_record and root_record.get("site") == root_key else []
    if not isinstance(known, list):
        known = []
    result = discover(address, [url for url in known if isinstance(url, str)])
    result.update(schema_version=1, lookup_key=key, checked_at=now(), cached=False)
    result["directory"] = observe(result)
    # A network outage must not replace a previously usable offline site map.
    if result["sitemaps"] or result["missing_sitemap"]:
        write_json(path, result)
    if result["sitemaps"] and key != root_key:
        write_json(cache_path("sites", root_key), {**result, "lookup_key":root_key})
    return result
