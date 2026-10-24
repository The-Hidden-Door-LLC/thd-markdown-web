"""Image-free HTTPS reading: published sitemap -> cached Markdown -> offline view."""

from pathlib import Path
import threading
import ipaddress
import shutil
import socket
import subprocess
from urllib.parse import unquote, urlsplit
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit

from . import sources
from thd_vault import gtk_ui as vault_ui

from .preview import render
from .document import save_document

ROOT = Path(__file__).resolve().parents[1]


def label(text, css=None, wrap=False):
    item = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if css:
        item.add_css_class(css)
    return item


def button(text, callback, icon=None, tooltip=None):
    item = Gtk.Button(label=text, tooltip_text=tooltip)
    if icon:
        item.set_icon_name(icon)
    item.connect("clicked", lambda *_: callback())
    return item


class BrowserWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="THD — Markdown Web", default_width=1440, default_height=900)
        self.set_size_request(360, 360)
        self.closed = False
        self.page_generation = 0
        self.map_generation = 0
        self.map_origin = None
        self.map_loading = False
        self.loading = False
        self.pages = []
        self.current = None
        self.map_result = None
        self.history = []
        self.history_position = -1
        self.cache_stop = threading.Event()
        self.caching = False
        self.last_width = 0
        self.connect("close-request", self.close_requested)
        css = Gtk.CssProvider()
        css.load_from_path(str(ROOT / "data/style.css"))
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        handle = Gtk.WindowHandle()
        heading = Gtk.Box(spacing=6)
        heading.add_css_class("header")
        brand = label("THD  /  Markdown Web", "brand")
        brand.set_ellipsize(3)
        brand.set_hexpand(True)
        heading.append(brand)
        self.sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True, tooltip_text="Show site map")
        heading.append(self.sidebar_toggle)
        heading.append(button("", self.open_vault, "folder-remote-symbolic", "Open a Vault document"))
        heading.append(button("", self.save_vault, "document-send-symbolic", "Save Markdown to Vault"))
        heading.append(button("", self.save_copy, "document-save-symbolic", "Save this page as Markdown"))
        heading.append(button("", self.open_editor, "document-edit-symbolic", "Return to your Markdown editor"))
        heading.append(Gtk.WindowControls(side=Gtk.PackType.END, decoration_layout=":close"))
        handle.set_child(heading)
        self.set_titlebar(handle)

        navigation = Gtk.Box(spacing=6)
        navigation.add_css_class("browser-navigation")
        self.back = button("", lambda: self.history_move(-1), "go-previous-symbolic", "Back · Alt+Left")
        self.forward = button("", lambda: self.history_move(1), "go-next-symbolic", "Forward · Alt+Right")
        self.reload = button("", lambda: self.navigate(self.address.get_text(), refresh=True, remember=False), "view-refresh-symbolic", "Refresh page from the website · Ctrl+R")
        self.address = Gtk.Entry(placeholder_text="Domain or sitemap URL", hexpand=True, tooltip_text="Domain, HTTPS page, or sitemap URL · Ctrl+L")
        self.address.set_width_chars(1)
        self.address.connect("activate", lambda *_: self.navigate(self.address.get_text()))
        for item in (self.back, self.forward, self.reload, self.address):
            navigation.append(item)
        root.append(navigation)
        self.notice = label("", "notice", True)
        self.notice.set_visible(False)
        root.append(self.notice)

        self.overlay = Gtk.Overlay(vexpand=True)
        root.append(self.overlay)
        self.body = Gtk.Box(vexpand=True)
        self.overlay.set_child(self.body)
        self.sidebar = Gtk.ScrolledWindow()
        self.sidebar.set_hexpand(False)
        self.sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar.set_child(self.make_sidebar())
        self.body.append(self.sidebar)
        self.sidebar_toggle.connect("toggled", lambda item: self.sidebar.set_visible(item.get_active()))

        self.web = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral(), hexpand=True, vexpand=True)
        self.web.get_settings().set_enable_javascript(False)
        self.web.get_settings().set_allow_file_access_from_file_urls(False)
        self.web.get_settings().set_allow_universal_access_from_file_urls(False)
        self.web.connect("decide-policy", self.link_policy)
        self.web.connect("permission-request", lambda _, request: (request.deny(), True)[1])
        self.web.connect("web-process-terminated", lambda *_: self.message("The reader stopped. Open a cached page to reload it."))
        self.body.append(self.web)
        self.web.load_html(render("# The web, in words.\n\nEnter a domain or sitemap. Read its pages as Markdown, keep local copies, and leave the images behind.\n"), "about:blank")
        self.status = label("Markdown only · no images · pages cached as you read", "status")
        self.status.set_ellipsize(3)
        root.append(self.status)
        self.findbar = Gtk.Box(spacing=6)
        self.findbar.add_css_class("pane-header")
        self.find_entry = Gtk.Entry(placeholder_text="Find on page", hexpand=True)
        self.find_entry.connect("activate", lambda *_: self.find_next())
        self.findbar.append(self.find_entry)
        self.findbar.append(button("Next", self.find_next))
        self.findbar.append(button("Close", self.close_find))
        self.findbar.set_visible(False)
        root.append(self.findbar)
        self.install_actions()
        self.add_tick_callback(self.resize_tick)
        self.update_history()

    def make_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("sidebar")
        box.set_size_request(280, -1)
        box.append(label("READ THE SITE", "eyebrow"))
        heading = Gtk.Box(spacing=6)
        title = label("Site map", "brand")
        title.set_hexpand(True)
        heading.append(title)
        heading.append(button("", lambda: self.load_sitemap(self.address.get_text(), True), "view-refresh-symbolic", "Refresh sitemap"))
        box.append(heading)
        self.site_label = label("No site open", "muted", True)
        box.append(self.site_label)
        self.map_status = label("Published pages will appear here.", "source-hint", True)
        box.append(self.map_status)
        self.cache_button = button("Cache all pages", self.cache_pages, tooltip="Download sitemap pages as Markdown for offline reading")
        box.append(self.cache_button)
        self.cache_status = label("", "source-hint", True)
        box.append(self.cache_status)
        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter pages…")
        self.filter_entry.set_max_width_chars(22)
        self.filter_entry.connect("search-changed", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        box.append(self.filter_entry)
        self.strings = Gtk.StringList.new([])
        self.filter = Gtk.CustomFilter.new(lambda item: self.filter_entry.get_text().casefold() in unquote(item.get_string()).casefold())
        self.filtered = Gtk.FilterListModel(model=self.strings, filter=self.filter)
        selection = Gtk.SingleSelection(model=self.filtered, autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_page)
        factory.connect("bind", self.bind_page)
        self.page_list = Gtk.ListView(model=selection, factory=factory, single_click_activate=True, vexpand=True)
        self.page_list.add_css_class("site-pages")
        self.page_list.connect("activate", self.activate_page)
        box.append(self.page_list)
        box.append(button("Publish missing-sitemap check", self.publish_check))
        box.append(button("Export Directory of Shame", self.export_directory))
        return box

    def setup_page(self, factory, item):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        row.add_css_class("site-page")
        for css in ("source-name", "source-hint"):
            text = label("", css)
            text.set_ellipsize(3)
            text.set_max_width_chars(29)
            row.append(text)
        item.set_child(row)

    def bind_page(self, factory, item):
        url = item.get_item().get_string()
        parts = urlsplit(url)
        path = unquote(parts.path) or "/"
        row = item.get_child()
        row.get_first_child().set_text(path.rstrip("/").rsplit("/", 1)[-1] or "Home")
        row.get_last_child().set_text(path + ("?" + parts.query if parts.query else ""))
        row.set_tooltip_text(url)

    def activate_page(self, view, position):
        item = self.filtered.get_item(position)
        if item:
            self.navigate(item.get_string())
            if self.get_width() < 1000:
                self.sidebar_toggle.set_active(False)

    def navigate(self, address, refresh=False, remember=True, history_position=None):
        address = address.strip()
        if not address:
            self.message("Enter a domain, HTTPS page, or sitemap URL.")
            return
        self.page_generation += 1
        generation = self.page_generation
        self.map_generation += 1
        if self.map_loading:
            self.map_origin = None
            self.map_loading = False
        # Validation and canonicalization belong to the source child process.
        if address.split("?", 1)[0].split("#", 1)[0].lower().endswith((".xml", ".xml.gz")):
            self.loading = False
            self.address.set_text(address)
            self.load_sitemap(address, refresh, open_first=True)
            return
        self.loading = True
        self.notice.set_visible(False)
        self.address.set_text(address)
        self.status.set_text("Converting page to Markdown…")
        def worker():
            try:
                result, error = sources.request("web", address, refresh=refresh), None
            except Exception as exc:
                result, error = None, str(exc) if isinstance(exc, ValueError) else "The page could not be opened."
            def finished():
                if self.closed or generation != self.page_generation:
                    return False
                self.loading = False
                if error:
                    self.message(error)
                    self.status.set_text("Page not opened · current page preserved")
                    return False
                self.current = result
                self.address.set_text(result["url"])
                self.web.load_html(render(result["markdown"]), "about:blank")
                self.set_title(result["title"] + " — THD Markdown Web")
                label_text = ("Cached" if result["cached"] else "Fetched") + " · " + result["fetched_at"][:19].replace("T", " ") + " UTC · Markdown only"
                self.status.set_text(label_text)
                if result.get("limited_text"):
                    self.message("Little static text was available. This site may depend on JavaScript to expose its content.")
                if history_position is not None:
                    self.history_position = history_position
                elif remember:
                    self.history = self.history[:self.history_position+1]
                    if not self.history or self.history[-1] != result["url"]:
                        self.history.append(result["url"])
                    self.history_position = len(self.history)-1
                self.update_history()
                self.load_sitemap(result["url"])
                return False
            GLib.idle_add(finished)
        threading.Thread(target=worker, daemon=True).start()

    def history_move(self, offset):
        position = self.history_position + offset
        if 0 <= position < len(self.history):
            self.navigate(self.history[position], remember=False, history_position=position)

    def update_history(self):
        self.back.set_sensitive(self.history_position > 0)
        self.forward.set_sensitive(0 <= self.history_position < len(self.history)-1)

    def link_policy(self, web, decision, kind):
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == "about:blank" or uri.startswith("about:blank#"):
                decision.use()
            else:
                decision.ignore()
                self.navigate(uri)
            return True
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            if decision.get_navigation_action().is_user_gesture():
                self.navigate(decision.get_navigation_action().get_request().get_uri())
            return True
        return False

    def load_sitemap(self, address, refresh=False, open_first=False):
        # Do not let an older site's asynchronous result replace the current site.
        try:
            parts = urlsplit(address if "://" in address else "https://" + address)
            if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError()
            parts.port
        except ValueError:
            self.message("Enter a valid HTTPS domain or sitemap address.")
            return
        site = parts.scheme + "://" + parts.netloc
        if site == self.map_origin and not refresh and not open_first:
            return
        self.cache_stop.set()
        self.cache_stop = threading.Event()
        self.caching = False
        self.cache_button.set_label("Cache all pages")
        self.cache_status.set_text("")
        self.map_origin = site
        self.map_generation += 1
        generation = self.map_generation
        navigation_generation = self.page_generation
        self.map_loading = True
        self.map_result = None
        self.pages = []
        self.strings.splice(0, self.strings.get_n_items(), [])
        self.map_status.set_text("Loading site map…")
        self.site_label.set_text(parts.netloc)
        self.filter_entry.set_text("")
        def worker():
            try:
                result, error = sources.request("sitemap", address, refresh=refresh), None
            except Exception as exc:
                result, error = None, str(exc) if isinstance(exc, ValueError) else "Sitemap could not be loaded."
            def finished():
                if self.closed or generation != self.map_generation or navigation_generation != self.page_generation:
                    return False
                self.map_loading = False
                if error:
                    self.map_status.set_text(error)
                    return False
                self.map_result = result
                self.map_origin = result["site"]
                self.pages = result["pages"]
                self.strings.splice(0, self.strings.get_n_items(), [page["url"] for page in self.pages])
                if result["missing_sitemap"]:
                    self.map_status.set_text("No discoverable sitemap. Recorded in the local Directory of Shame export.")
                elif not self.pages:
                    self.map_status.set_text("No pages available. Connection or unsupported sitemap failures stay unclassified.")
                else:
                    suffix = " · partial" if result["truncated"] else ""
                    self.map_status.set_text(f'{len(self.pages):,} pages · {"cached" if result["cached"] else "fetched"} sitemap' + suffix)
                self.map_status.set_tooltip_text("Checked " + result["checked_at"] + "\n" + "\n".join(result.get("warnings", [])))
                if open_first and self.pages:
                    self.navigate(self.pages[0]["url"])
                return False
            GLib.idle_add(finished)
        threading.Thread(target=worker, daemon=True).start()

    def cache_pages(self):
        if self.caching:
            self.cache_stop.set()
            self.cache_status.set_text("Stopping after the current page…")
            return
        if not self.pages:
            self.message("Load a sitemap with pages first.")
            return
        pages = [item["url"] for item in self.pages]
        stop = threading.Event()
        self.cache_stop = stop
        self.caching = True
        self.cache_button.set_label("Stop caching")
        self.cache_status.set_text("Caching pages as Markdown…")
        def progress(text):
            def update():
                if not self.closed and stop is self.cache_stop:
                    self.cache_status.set_text(text)
                return False
            GLib.idle_add(update)
        def worker():
            done, failed = 0, 0
            for url in pages:
                if stop.is_set():
                    break
                try:
                    sources.request("web", url)
                    done += 1
                except Exception:
                    failed += 1
                progress(f"{done} cached · {failed} failed · {len(pages)} pages")
            def finished():
                if not self.closed and stop is self.cache_stop:
                    self.caching = False
                    self.cache_button.set_label("Cache all pages")
                    self.cache_status.set_text(f'{done} cached · {failed} failed' + (" · stopped" if stop.is_set() else " · ready offline"))
                return False
            GLib.idle_add(finished)
        threading.Thread(target=worker, daemon=True).start()

    def publish_check(self):
        if not self.map_result or not self.map_result.get("missing_sitemap"):
            self.message("Only a confirmed missing-sitemap observation can be submitted.")
            return
        host = urlsplit(self.map_result["site"]).hostname
        def worker():
            try:
                if not host or "." not in host or not shutil.which("gh"):
                    raise ValueError()
                addresses = socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError()
                subprocess.run(["gh","workflow","run","pages.yml","--repo","The-Hidden-Door-LLC/thd-markdown-web","--field","domain="+host],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=True)
                text = "Public recheck requested. GitHub Actions will verify the domain and publish the result."
            except (OSError, ValueError, subprocess.SubprocessError):
                text = "Publishing needs GitHub CLI publisher access and a publicly reachable domain."
            def finished():
                if not self.closed:
                    self.message(text)
                return False
            GLib.idle_add(finished)
        threading.Thread(target=worker,daemon=True).start()

    def export_directory(self):
        def worker():
            try:
                result = sources.request("directory")
                text = f'Local export: {result["html"]} · {result["entries"]} entries. Use Publish missing-sitemap check for an independent public recheck.'
            except Exception:
                text = "The directory could not be exported."
            GLib.idle_add(lambda: self.message(text) if not self.closed else False)
        threading.Thread(target=worker, daemon=True).start()

    def open_vault(self):
        def selected(info, data):
            from .document import decode_document
            markdown = decode_document(data)
            self.page_generation += 1
            self.map_generation += 1
            self.cache_stop.set()
            self.current = {"url":"vault://"+info["id"]+"/"+info["filename"], "title":info["title"], "markdown":markdown, "vault":info}
            self.address.set_text(self.current["url"])
            self.web.load_html(render(markdown), "about:blank")
            self.map_status.set_text("Vault document · opened in memory")
            self.map_loading = False
            self.loading = False
            self.pages = []
            self.map_result = None
            self.map_origin = None
            self.strings.splice(0,self.strings.get_n_items(),[])
            self.history = []
            self.history_position = -1
            self.update_history()
            self.status.set_text("Vault · "+info["filename"])
        vault_ui.open_picker(self, {"text/markdown", "text/plain"}, selected)

    def save_vault(self):
        if not self.current:
            return
        current = self.current
        previous = current.get("vault")
        filename = previous["filename"] if previous else (Path(urlsplit(current["url"]).path).stem or "page")+".md"
        def saved(info):
            if self.current is current:
                self.current["vault"] = info
                self.message("Saved Markdown to Vault.")
        vault_ui.save_dialog(self, current["markdown"].encode(), "text/markdown", filename, current["title"], previous, saved)

    def save_copy(self):
        if not self.current:
            return
        current = self.current
        filename = Path(urlsplit(current["url"]).path).stem or "page"
        dialog = Gtk.FileDialog(title="Save Markdown copy", initial_name=filename + ".md")
        def chosen(obj, response):
            try:
                path = obj.save_finish(response).get_path()
                if path:
                    save_document(path, current["markdown"], create_only=True)
                    self.message("Markdown copy saved.")
            except GLib.Error:
                pass
            except (OSError, ValueError):
                self.message("Could not save. Choose an unused filename and a writable folder.")
        dialog.save(self, None, chosen)

    def open_editor(self):
        self.get_application().editor_window().present()

    def message(self, text):
        self.notice.set_text(text)
        self.notice.set_visible(True)

    def install_actions(self):
        callbacks = {"address": lambda: (self.address.grab_focus(), self.address.select_region(0, -1)),
                     "reload": lambda: self.navigate(self.address.get_text(), refresh=True, remember=False),
                     "back": lambda: self.history_move(-1), "forward": lambda: self.history_move(1), "find": self.show_find}
        keys = {"address":"<Primary>l", "reload":"<Primary>r", "back":"<Alt>Left", "forward":"<Alt>Right", "find":"<Primary>f"}
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _, __, cb=callback: cb())
            self.add_action(action)
            self.get_application().set_accels_for_action("win." + name, [keys[name]])

    def show_find(self):
        self.findbar.set_visible(True)
        self.find_entry.grab_focus()

    def find_next(self):
        value = self.find_entry.get_text()
        if value:
            self.web.get_find_controller().search(value, int(WebKit.FindOptions.CASE_INSENSITIVE | WebKit.FindOptions.WRAP_AROUND), 1000)

    def close_find(self):
        self.web.get_find_controller().search_finish()
        self.findbar.set_visible(False)

    def resize_tick(self, *_):
        width = self.get_width()
        if width <= 0 or width == self.last_width:
            return True
        self.last_width = width
        narrow = width < 1000
        overlay = self.sidebar.get_parent() == self.overlay
        if narrow and not overlay:
            self.body.remove(self.sidebar)
            self.sidebar.set_halign(Gtk.Align.START)
            self.sidebar.set_valign(Gtk.Align.FILL)
            self.overlay.add_overlay(self.sidebar)
            self.sidebar_toggle.set_active(False)
        elif not narrow and overlay:
            self.overlay.remove_overlay(self.sidebar)
            self.sidebar.set_halign(Gtk.Align.FILL)
            self.body.prepend(self.sidebar)
            self.sidebar_toggle.set_active(True)
        return True

    def close_requested(self, *_):
        self.closed = True
        self.page_generation += 1
        self.map_generation += 1
        self.cache_stop.set()
        return False
