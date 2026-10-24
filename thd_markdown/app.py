"""Native GTK editor, offline WebKit reader, and shared Sources UI."""

from pathlib import Path
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GtkSource", "5")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkSource, WebKit

from .document import save_document
from .preview import render
from . import sources
from thd_vault import gtk_ui as vault_ui


ROOT = Path(__file__).resolve().parents[1]
WELCOME = """# A little room to think.

Write in Markdown. Read it beautifully.

THD Markdown keeps your words close: a quiet editor, a live preview,
and your documents wherever you keep them.

## Start with a thought

- Open a file from **this computer**.
- Paste a `gs://` or `dropbox:///` address into **Sources**.
- Make something new, then **save a local copy**.

> A good tool should leave room for the work.

## A few useful marks

Use **bold**, *italics*, and ~~a second thought~~. Tables and fenced code
blocks work, too.

```python
def begin():
    return "One clear sentence."
```

| Shortcut | Action |
| --- | --- |
| Ctrl + O | Open a local file |
| Ctrl + S | Save |
| Ctrl + Shift + S | Save a new copy |
| Ctrl + F | Find in your document |
| Ctrl + Z | Undo |
"""


def label(text, css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, tooltip=None):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda *_: callback())
    if tooltip:
        widget.set_tooltip_text(tooltip)
    return widget


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="THD Markdown", default_width=1440, default_height=900)
        self.set_size_request(360, 360)
        self.source = None
        self.local_path = None
        self.source_hash = None
        self.busy = False
        self.closed = False
        self.preview_timer = 0
        self.mode = "Split"
        self.connect("close-request", self.close_requested)

        css = Gtk.CssProvider()
        css.load_from_path(str(ROOT / "data/style.css"))
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", True)

        handle = Gtk.WindowHandle()
        header = Gtk.Box(spacing=6)
        header.add_css_class("header")
        handle.set_child(header)
        self.set_titlebar(handle)
        self.brand = label("THD  /  Markdown", "brand")
        self.brand.set_ellipsize(3)
        self.brand.set_halign(Gtk.Align.CENTER)
        self.brand.set_hexpand(True)
        self.new_button = button("New", self.new_document, "New document · Ctrl+N")
        self.open_button = button("Open", self.open_local, "Open local file · Ctrl+O")
        self.new_button.set_icon_name("document-new-symbolic")
        self.open_button.set_icon_name("document-open-symbolic")
        header.append(self.new_button)
        header.append(self.open_button)
        header.append(self.brand)
        self.save_button = button("Save", self.save, "Save · Ctrl+S")
        self.save_button.add_css_class("suggested-action")
        menu = Gio.Menu()
        for title, action in (("Open from Vault…", "win.vault-open"), ("Save to Vault…", "win.vault-save"), ("Browse a website…", "win.browser"), ("Save a new copy…", "win.save-as"), ("Find…", "win.find"), ("Undo", "win.undo"), ("Redo", "win.redo")):
            menu.append(title, action)
        more = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        self.sources_button = Gtk.ToggleButton(label="Sources", active=True)
        self.sources_button.set_icon_name("sidebar-show-symbolic")
        self.sources_button.set_tooltip_text("Show or hide Sources")
        header.append(self.sources_button)
        header.append(more)
        header.append(self.save_button)
        self.window_controls = Gtk.WindowControls(side=Gtk.PackType.END, decoration_layout=":close")
        header.append(self.window_controls)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        self.notice = label("", "notice", wrap=True)
        self.notice.set_visible(False)
        root.append(self.notice)
        self.overlay = Gtk.Overlay(vexpand=True)
        root.append(self.overlay)
        self.body = Gtk.Box(vexpand=True)
        self.overlay.set_child(self.body)
        self.sidebar_content = self.make_sidebar()
        self.sidebar = Gtk.ScrolledWindow()
        self.sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar.set_child(self.sidebar_content)
        self.body.append(self.sidebar)
        self.sources_button.connect("toggled", lambda toggle: self.sidebar.set_visible(toggle.get_active()))

        workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.body.append(workspace)
        toolbar = Gtk.Box(spacing=12)
        toolbar.add_css_class("pane-header")
        self.doc_label = label("Untitled.md", "source-name")
        self.doc_label.set_hexpand(True)
        self.doc_label.set_ellipsize(3)
        toolbar.append(self.doc_label)
        self.view_select = Gtk.DropDown.new_from_strings(["Write", "Split", "Read"])
        self.view_select.set_selected(1)
        self.view_select.connect("notify::selected", self.change_mode)
        toolbar.append(self.view_select)
        workspace.append(toolbar)

        self.findbar = Gtk.Box(spacing=8)
        self.findbar.add_css_class("pane-header")
        self.find_entry = Gtk.Entry(placeholder_text="Find in document", hexpand=True)
        self.find_entry.connect("activate", lambda *_: self.find_next())
        self.findbar.append(self.find_entry)
        self.findbar.append(button("Next", self.find_next))
        self.findbar.append(button("Close", lambda: self.findbar.set_visible(False)))
        self.findbar.set_visible(False)
        workspace.append(self.findbar)

        self.panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=True)
        self.panes.set_position(480)
        self.panes.set_shrink_start_child(True)
        self.panes.set_shrink_end_child(True)
        workspace.append(self.panes)
        self.buffer = GtkSource.Buffer()
        self.buffer.set_language(GtkSource.LanguageManager.get_default().get_language("markdown"))
        self.buffer.set_highlight_syntax(True)
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark")
        if scheme:
            self.buffer.set_style_scheme(scheme)
        self.editor = GtkSource.View.new_with_buffer(self.buffer)
        self.editor.set_show_line_numbers(True)
        self.editor.set_auto_indent(True)
        self.editor.set_tab_width(4)
        self.editor.set_insert_spaces_instead_of_tabs(True)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_top_margin(24)
        self.editor.set_bottom_margin(60)
        self.editor.set_left_margin(20)
        self.editor.set_right_margin(20)
        self.edit_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.edit_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.edit_scroll.set_child(self.editor)
        self.panes.set_start_child(self.edit_scroll)

        self.web = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral())
        self.web.get_settings().set_enable_javascript(False)
        self.web.get_settings().set_allow_file_access_from_file_urls(False)
        self.web.get_settings().set_allow_universal_access_from_file_urls(False)
        self.web.connect("decide-policy", self.preview_navigation)
        self.web.connect("permission-request", lambda _, request: (request.deny(), True)[1])
        self.web.connect("create", lambda *_: None)
        self.web.connect("web-process-terminated", lambda *_: self.message("The preview stopped. Edit the document to reload it."))
        self.panes.set_end_child(self.web)

        self.status = label("", "status")
        self.status.set_ellipsize(3)
        self.status.set_size_request(0, -1)
        root.append(self.status)
        self.buffer.connect("changed", self.changed)
        self.buffer.connect("modified-changed", lambda *_: self.update_status())
        self.buffer.connect("mark-set", lambda *_: self.update_status())
        self._set_document(WELCOME)
        self.install_actions()
        self.last_width = 0
        self.add_tick_callback(self.check_width)
        self.connect("map", lambda *_: self.responsive_layout())

    def make_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.add_css_class("sidebar")
        box.set_size_request(258, -1)
        box.append(label("YOUR LIBRARY", "eyebrow"))
        box.append(label("Sources", "brand"))
        box.append(label("Open a document from wherever it lives.", "muted", True))
        self.address = Gtk.Entry(placeholder_text="Paste a path or source URI")
        self.address.set_tooltip_text("/path/notes.md · gs://bucket/notes.md · dropbox:///Notes/notes.md")
        self.address.connect("activate", lambda *_: self.open_address())
        box.append(self.address)
        self.load_button = button("Open source", self.open_address)
        box.append(self.load_button)
        self.spinner = Gtk.Spinner()
        box.append(self.spinner)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(self.cards)
        for title, hint, uri in (
            ("The web, in Markdown", "Domain or HTTPS sitemap · no images", "https://"),
            ("This computer", "Local Markdown and text files", None),
            ("Google Cloud Storage", "gs://bucket/document.md", "gs://"),
            ("Dropbox", "dropbox:///folder/document.md", "dropbox:///"),
            ("AWS S3", "Planned · adapter not connected", "s3://"),
        ):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            card.add_css_class("source-card")
            card.append(label(title, "source-name"))
            card.append(label(hint, "source-hint", True))
            card.append(button("Choose file…" if uri is None else "Use address", self.open_local if uri is None else lambda u=uri: self.fill_address(u)))
            self.cards.append(card)
        spacer = Gtk.Box(vexpand=True)
        box.append(spacer)
        box.append(label("Cloud originals stay untouched.\nSave your edits to this computer.", "source-hint", True))
        return box

    def fill_address(self, prefix):
        self.address.set_text(prefix)
        self.address.grab_focus()
        self.address.set_position(-1)

    def install_actions(self):
        callbacks = {
            "vault-open": self.open_vault, "vault-save": self.save_vault,
            "browser": lambda: self.get_application().browser_window().present(),
            "new": self.new_document, "open": self.open_local, "save": self.save,
            "save-as": lambda: self.save(True), "find": self.show_find,
            "undo": lambda: self.buffer.undo() if self.buffer.get_can_undo() and not self.busy else None,
            "redo": lambda: self.buffer.redo() if self.buffer.get_can_redo() and not self.busy else None,
        }
        accelerators = {"vault-open":"<Primary><Alt>o", "vault-save":"<Primary><Alt>s","browser":"<Primary><Shift>l", "new": "<Primary>n", "open": "<Primary>o", "save": "<Primary>s",
                        "save-as": "<Primary><Shift>s", "find": "<Primary>f",
                        "undo": "<Primary>z", "redo": "<Primary><Shift>z"}
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _, __, cb=callback: cb())
            self.add_action(action)
            self.get_application().set_accels_for_action("win." + name, [accelerators[name]])

    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def _set_document(self, text, result=None):
        self.document_generation = getattr(self,"document_generation",0)+1
        self.source = result["source"] if result else None
        self.local_path = self.source["path"] if self.source and self.source["provider"] == "local" else None
        self.source_hash = result["sha256"] if result else None
        self.vault_document = result.get("vault") if result else None
        self.buffer.begin_irreversible_action()
        self.buffer.set_text(text)
        self.buffer.end_irreversible_action()
        self.buffer.set_modified(False)
        self.notice.set_visible(False)
        self.update_status()
        self.refresh_preview()

    def changed(self, *_):
        self.update_status()
        if self.preview_timer:
            GLib.source_remove(self.preview_timer)
        self.preview_timer = GLib.timeout_add(220, self.refresh_preview)

    def refresh_preview(self):
        self.preview_timer = 0
        self.web.load_html(render(self.text()), "about:blank")
        return False

    def preview_navigation(self, web, decision, kind):
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == "about:blank" or uri.startswith("about:blank#"):
                decision.use()
            else:
                decision.ignore()
                self.message("Links stay in the document. Copy a link to open it in your browser.")
            return True
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    def update_status(self):
        if not hasattr(self, "buffer") or not hasattr(self, "status"):
            return
        name = Path(self.source["path"]).name if self.source else "Untitled.md"
        dirty = " •" if self.buffer.get_modified() else ""
        self.doc_label.set_text(name + dirty)
        self.set_title(name + dirty + " — THD Markdown")
        iterator = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        provider = self.source["provider"].upper() if self.source else "NEW DOCUMENT"
        self.status.set_text(f"{provider}    ·    {len(self.text().split()):,} words    ·    Ln {iterator.get_line()+1}, Col {iterator.get_line_offset()+1}    ·    UTF-8")
        self.doc_label.set_tooltip_text(self.source["uri"] if self.source else "New document")

    def message(self, text):
        self.notice.set_text(text)
        self.notice.set_visible(True)

    def guard(self, callback):
        if self.busy:
            return
        if not self.buffer.get_modified():
            callback()
            return
        dialog = Gtk.AlertDialog(message="Discard unsaved changes?", detail="Save your document first if you want to keep your edits.", buttons=["Cancel", "Discard changes"], cancel_button=0, default_button=0)
        def chosen(obj, response):
            try:
                if obj.choose_finish(response) == 1:
                    callback()
            except GLib.Error:
                pass
        dialog.choose(self, None, chosen)

    def new_document(self):
        self.guard(lambda: self._set_document(""))

    def open_local(self):
        if self.busy:
            return
        dialog = Gtk.FileDialog(title="Open a Markdown document")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        markdown = Gtk.FileFilter(name="Markdown and text")
        for pattern in ("*.md", "*.markdown", "*.txt", "*.mdown"):
            markdown.add_pattern(pattern)
        filters.append(markdown)
        any_file = Gtk.FileFilter(name="All files")
        any_file.add_pattern("*")
        filters.append(any_file)
        dialog.set_filters(filters)
        def chosen(obj, response):
            try:
                path = obj.open_finish(response).get_path()
                if path:
                    self.guard(lambda: self.load_source(path))
            except GLib.Error:
                pass
        dialog.open(self, None, chosen)

    def open_address(self):
        address = self.address.get_text().strip()
        if address.lower().startswith("https://"):
            browser = self.get_application().browser_window()
            browser.present()
            browser.navigate(address)
            return
        if not address:
            self.message("Paste an absolute local path, gs:// object, or dropbox:/// file address.")
            return
        self.guard(lambda: self.load_source(address))

    def set_busy(self, busy):
        self.busy = busy
        self.editor.set_editable(not busy)
        for control in (self.new_button, self.open_button, self.save_button, self.load_button, self.address):
            control.set_sensitive(not busy)
        self.spinner.set_spinning(busy)

    def load_source(self, address):
        if self.busy:
            return
        self.set_busy(True)
        self.message("Opening source…")
        def worker():
            try:
                text, result = sources.load(address)
                error = None
            except Exception as exc:
                text, result, error = None, None, str(exc) if isinstance(exc, ValueError) else "The source could not be opened."
            def finished():
                if self.closed:
                    return False
                self.set_busy(False)
                if error:
                    self.message(error)
                else:
                    self._set_document(text, result)
                    self.address.set_text(result["source"]["uri"])
                return False
            GLib.idle_add(finished)
        threading.Thread(target=worker, daemon=True).start()

    def save(self, save_as=False):
        if self.vault_document and not save_as:
            self.save_vault()
            return
        if self.busy:
            return
        if self.local_path and not save_as:
            self.write_to(self.local_path, self.source_hash)
            return
        dialog = Gtk.FileDialog(title="Save a new local copy", initial_name=Path(self.source["path"]).name if self.source else "Untitled.md")
        def chosen(obj, response):
            try:
                path = obj.save_finish(response).get_path()
                if path:
                    self.write_to(path, create_only=True)
            except GLib.Error:
                pass
        dialog.save(self, None, chosen)

    def write_to(self, path, expected_hash=None, create_only=False):
        try:
            new_hash = save_document(path, self.text(), expected_hash, create_only)
        except FileExistsError:
            self.message("That filename already exists. Choose a new name to keep both copies.")
            return False
        except (OSError, ValueError) as error:
            self.message(str(error) if isinstance(error, ValueError) else "Could not save this document. Check the destination and available space.")
            return False
        self.vault_document = None
        self.local_path = str(Path(path).absolute())
        self.source_hash = new_hash
        self.source = {"provider": "local", "path": self.local_path, "uri": Path(self.local_path).as_uri(), "bucket": ""}
        self.buffer.set_modified(False)
        self.address.set_text(self.source["uri"])
        self.message("Saved to this computer.")
        self.update_status()
        return True

    def open_vault(self):
        def selected(info, data):
            from .document import decode_document
            text = decode_document(data)
            self._set_document(text, {"source":{"provider":"vault", "path":info["filename"], "uri":"vault://"+info["id"]+"/"+info["filename"], "bucket":""}, "sha256":info["sha256"], "vault":info})
        self.guard(lambda: vault_ui.open_picker(self, {"text/markdown", "text/plain"}, selected))

    def save_vault(self):
        if self.busy:
            return
        body = self.text().encode("utf-8")
        filename = self.vault_document["filename"] if self.vault_document else Path(self.source["path"]).name if self.source else "Untitled.md"
        title = self.vault_document["title"] if self.vault_document else Path(filename).stem
        generation = self.document_generation
        def saved(info):
            if self.document_generation == generation:
                self.vault_document = info
                self.local_path = None
                self.source_hash = info["sha256"]
                self.source = {"provider":"vault", "path":info["filename"], "uri":"vault://"+info["id"]+"/"+info["filename"], "bucket":""}
                if self.text().encode("utf-8") == body:
                    self.buffer.set_modified(False)
                self.update_status()
                self.message("Saved to Vault.")
        vault_ui.save_dialog(self, body, "text/markdown", filename, title, self.vault_document, saved)

    def show_find(self):
        self.findbar.set_visible(True)
        self.find_entry.grab_focus()

    def find_next(self):
        needle = self.find_entry.get_text()
        if not needle:
            return
        position = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        result = position.forward_search(needle, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
        if not result:
            result = self.buffer.get_start_iter().forward_search(needle, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
        if result:
            self.buffer.select_range(result[1], result[0])
            self.editor.scroll_to_iter(result[0], .1, False, 0, 0)
        else:
            self.message("No matches in this document.")

    def change_mode(self, *_):
        self.mode = ["Write", "Split", "Read"][self.view_select.get_selected()]
        self.apply_mode()

    def apply_mode(self):
        narrow = self.get_width() < 800
        self.edit_scroll.set_visible(self.mode != "Read")
        self.web.set_visible(self.mode == "Read" or (self.mode == "Split" and not narrow))

    def responsive_layout(self, *_):
        if self.get_width() <= 0:
            return
        narrow = self.get_width() < 1000
        if self.get_width() < 800 and self.mode == "Split":
            self.view_select.set_selected(0)
        in_overlay = self.sidebar.get_parent() == self.overlay
        if narrow and not in_overlay:
            self.body.remove(self.sidebar)
            self.sidebar.set_halign(Gtk.Align.START)
            self.sidebar.set_valign(Gtk.Align.FILL)
            self.overlay.add_overlay(self.sidebar)
            self.sources_button.set_active(False)
        elif not narrow and in_overlay:
            self.overlay.remove_overlay(self.sidebar)
            self.sidebar.set_halign(Gtk.Align.FILL)
            self.body.prepend(self.sidebar)
            self.sources_button.set_active(True)
        elif narrow and self.last_width == 0:
            self.sources_button.set_active(False)
        self.brand.set_visible(self.get_width() >= 650)
        self.apply_mode()

    def check_width(self, *_):
        width = self.get_width()
        if width != self.last_width:
            self.last_width = width
            self.responsive_layout()
        return True

    def close_requested(self, *_):
        if self.busy:
            self.message("A source is still opening. Wait for it to finish before closing.")
            return True
        if self.buffer.get_modified():
            self.guard(self.finish_close)
            return True
        self.closed = True
        return False

    def finish_close(self):
        self.buffer.set_modified(False)
        self.close()


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.the_hidden_door_llc.thd_markdown", flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = Window(self)
        window.present()

    def editor_window(self):
        return next((window for window in self.get_windows() if isinstance(window, Window)), None) or Window(self)

    def browser_window(self):
        from .browser import BrowserWindow
        return next((window for window in self.get_windows() if isinstance(window, BrowserWindow)), None) or BrowserWindow(self)

    def do_open(self, files, count, hint):
        if files:
            uri = files[0].get_uri()
            if uri.lower().startswith("https://"):
                window = self.browser_window()
                window.present()
                window.navigate(uri)
            else:
                window = self.editor_window()
                window.present()
                window.guard(lambda: window.load_source(uri))


def main():
    return Application().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
