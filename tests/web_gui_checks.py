"""Isolated TLS fixture proves sitemap -> Markdown cache -> offline GTK reading."""

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time

import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11, GLib
from PIL import ImageGrab
from thd_markdown.app import Application


def pump(seconds=.15):
    until = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < until:
        while context.pending():
            context.iteration(False)
        time.sleep(.01)


def wait_for(predicate, seconds=15):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        pump(.05)
    assert predicate(), "Application state timed out"


checks = []
def check(value, text):
    assert value, text
    checks.append(text)
    print("PASS:", text, flush=True)


qa = Path("qa")
qa.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    certificate, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Trust only the synthetic fixture in child processes; production uses normal TLS trust.
    os.environ.update(SSL_CERT_FILE=str(certificate), XDG_CACHE_HOME=str(directory / "cache"), XDG_DATA_HOME=str(directory / "data"))
    requests = []
    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                data, kind = ("Sitemap: " + site + "/sitemap.xml\n").encode(), "text/plain"
            elif self.path == "/sitemap.xml":
                data, kind = ('<sitemapindex><sitemap><loc>' + site + '/pages.xml.gz</loc></sitemap></sitemapindex>').encode(), "application/xml"
            elif self.path == "/pages.xml.gz":
                data, kind = gzip.compress(('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>' + site + '/</loc></url><url><loc>' + site + '/article</loc></url></urlset>').encode()), "application/xml"
            elif self.path in ("/", "/article"):
                title = "A readable web" if self.path == "/" else "The second page"
                data, kind = (f'<html><head><title>{title}</title><style>body{{background:red}}</style></head><body><nav>Discard this navigation</nav><main><h1>{title}</h1><p>Clear words, local reading, and a useful sitemap. This paragraph has enough text to exercise the reader without a limited-content warning.</p><h2>Words first</h2><p>Follow the <a href="/article">second page</a> or go <a href="/">home</a>.</p><ul><li>Readable headings</li><li>Working links</li><li>Offline Markdown</li></ul><img src="/tracker.png"><script>document.body.textContent="SCRIPT RAN"</script></main></body></html>').encode(), "text/html"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    site = "https://127.0.0.1:" + str(server.server_address[1])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    app = Application()
    app.register(None)
    editor = app.editor_window()
    editor.buffer.insert_at_cursor("My unsaved draft")
    draft = editor.text()
    browser = app.browser_window()
    browser.present()
    resources = []
    browser.web.connect("resource-load-started", lambda web, resource, request: resources.append(request.get_uri()))
    browser.navigate(site + "/sitemap.xml")
    wait_for(lambda: browser.current is not None and not browser.loading and not browser.map_loading)
    pump(.7)
    check(len(browser.pages) == 2 and len(browser.map_result["sitemaps"]) == 2, "Explicit sitemap resolves index and compressed child into page navigation")
    check("# A readable web" in browser.current["markdown"], "HTTPS HTML is converted to real Markdown before rendering")
    check("[second page](" + site + "/article)" in browser.current["markdown"], "Relative links resolve to HTTPS source addresses")
    check("tracker" not in browser.current["markdown"] and "SCRIPT RAN" not in browser.current["markdown"], "HTML images and scripts are removed")
    check("/tracker.png" not in requests, "Embedded image bytes are never requested")
    check(not browser.web.get_settings().get_enable_javascript(), "Reader JavaScript stays disabled")
    check(all(uri.startswith("about:blank") for uri in resources), "WebKit renders only offline Markdown HTML")
    check(editor.text() == draft and editor.buffer.get_modified(), "Browsing preserves the editor's unsaved draft")

    browser.cache_pages()
    wait_for(lambda: not browser.caching)
    check("2 cached" in browser.cache_status.get_text(), "Cache all pages creates the offline Markdown collection")
    wid = str(GdkX11.X11Surface.get_xid(browser.get_surface()))
    def screenshot(name):
        geometry = dict(line.split("=", 1) for line in subprocess.check_output(["xdotool", "getwindowgeometry", "--shell", wid], text=True).splitlines())
        x, y, width, height = (int(geometry[field]) for field in ("X", "Y", "WIDTH", "HEIGHT"))
        ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).crop((x, y, x+width, y+height)).save(qa / name)
    screenshot("web-desktop.png")
    for width in (650, 375):
        subprocess.run(["xdotool", "windowsize", wid, str(width), "780"], check=True)
        pump(.3)
    check(browser.get_width() <= 375, "Web reader fits a 375 pixel window")
    browser.sidebar_toggle.set_active(True)
    pump(.2)
    check(browser.get_width() <= 375, "Sitemap overlays the narrow reader without widening it")
    screenshot("web-narrow-sitemap.png")
    browser.sidebar_toggle.set_active(False)
    screenshot("web-narrow-read.png")

    server.shutdown()
    server.server_close()
    before = len(requests)
    browser.navigate(site + "/article")
    wait_for(lambda: not browser.loading and browser.current["url"].endswith("/article"))
    check(browser.current["cached"] and len(requests) == before, "Cached page opens while the HTTPS server is offline")
    browser.history_move(-1)
    wait_for(lambda: not browser.loading and browser.current["url"] == site + "/")
    check(browser.current["cached"], "Back navigation uses the local Markdown cache")
    browser.navigate("https://[")
    wait_for(lambda: not browser.loading)
    check(browser.current["url"] == site + "/" and browser.notice.get_visible(), "Malformed addresses show an error and preserve the page")
    browser.close()
    editor.buffer.set_modified(False)
    editor.close()
    pump(.2)
(qa / "web-gui-results.json").write_text(json.dumps({"passed":len(checks), "checks":checks}, indent=2) + "\n")
print(f"ALL {len(checks)} WEB GUI CHECKS PASSED")
