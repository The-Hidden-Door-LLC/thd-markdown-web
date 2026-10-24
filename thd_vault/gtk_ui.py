"""Native document picker and save dialog for the local Vault bridge."""

import threading
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
from . import client


def background(parent, function, success, failure):
    def worker():
        try:
            result, error = function(), None
        except client.VaultError as exc:
            result, error = None, str(exc)
        except Exception:
            result, error = None, "The Vault operation could not complete."
        def finish():
            if getattr(parent, "closed", False):
                return False
            failure(error) if error else success(result)
            return False
        GLib.idle_add(finish)
    threading.Thread(target=worker, daemon=True).start()


class VaultPicker(Gtk.Window):
    def __init__(self, parent, media_types, on_open):
        super().__init__(title="Open from Vault", transient_for=parent, modal=True, default_width=560, default_height=460)
        self.closed = False
        self.media_types = media_types
        self.on_open = on_open
        self.items = []
        self.connect("close-request", self.closing)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.set_child(box)
        self.search = Gtk.SearchEntry(placeholder_text="Find a Vault document…")
        self.search.connect("search-changed", lambda *_: self.show_items())
        box.append(self.search)
        self.status = Gtk.Label(label="Connecting to Vault…", xalign=0, wrap=True)
        box.append(self.status)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", self.open_row)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.list)
        box.append(scroll)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.reload())
        box.append(refresh)
        self.reload()

    def closing(self, *_):
        self.closed = True
        return False

    def reload(self):
        self.status.set_text("Connecting to Vault…")
        def loaded(items):
            self.items = [item for item in items if item["mediaType"] in self.media_types]
            self.show_items()
        background(self, client.list_documents, loaded, self.status.set_text)

    def show_items(self):
        while child := self.list.get_first_child():
            self.list.remove(child)
        needle = self.search.get_text().casefold()
        count = 0
        for item in self.items:
            if needle not in (item["title"]+" "+item["filename"]).casefold():
                continue
            row = Gtk.ListBoxRow()
            row.metadata = item
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            title = Gtk.Label(label=item["title"], xalign=0, ellipsize=3)
            content.append(title)
            content.append(Gtk.Label(label=f'{item["filename"]} · {item["size"]:,} bytes', xalign=0, ellipsize=3))
            row.set_child(content)
            self.list.append(row)
            count += 1
        self.status.set_text(f"{count} matching documents. Double-click one to open." if count else "No matching documents in the unlocked Vault.")

    def open_row(self, listing, row):
        listing.set_sensitive(False)
        self.status.set_text("Opening encrypted document…")
        def loaded(result):
            try:
                self.on_open(*result)
            except (ValueError, UnicodeError):
                self.status.set_text("This document is not supported by this editor.")
                listing.set_sensitive(True)
                return
            self.close()
        def failed(text):
            self.status.set_text(text)
            listing.set_sensitive(True)
        background(self, lambda: client.read_document(row.metadata["id"]), loaded, failed)


def open_picker(parent, media_types, on_open):
    dialog = VaultPicker(parent, media_types, on_open)
    dialog.present()
    return dialog


def save_dialog(parent, data, media_type, filename, title, previous, on_saved):
    dialog = Gtk.Dialog(title="Save to Vault", transient_for=parent, modal=True, use_header_bar=True)
    dialog.closed = False
    dialog.saving = False
    dialog.connect("close-request", lambda *_: True if dialog.saving else setattr(dialog, "closed", True) or False)
    cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    save = dialog.add_button("Save", Gtk.ResponseType.ACCEPT)
    content = dialog.get_content_area()
    content.set_spacing(10)
    for method in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
        getattr(content, method)(16)
    content.append(Gtk.Label(label="Title", xalign=0))
    name = Gtk.Entry(text=title[:200], max_length=200)
    content.append(name)
    content.append(Gtk.Label(label="Filename", xalign=0))
    file = Gtk.Entry(text=filename[:255], max_length=255)
    content.append(file)
    copy = Gtk.CheckButton(label="Save a new Vault item", active=previous is None)
    copy.set_visible(previous is not None)
    content.append(copy)
    status = Gtk.Label(label="The Vault encrypts the document before storage.", xalign=0, wrap=True)
    content.append(status)
    def respond(obj, response):
        if obj.saving:
            return
        if response != Gtk.ResponseType.ACCEPT:
            obj.closed = True
            obj.close()
            return
        filename_value, title_value = file.get_text().strip(), name.get_text().strip()
        if not title_value or not filename_value or any(c in filename_value for c in ("/", "\\", "\x00")):
            status.set_text("Choose a title and a filename without folders.")
            return
        obj.saving = True
        save.set_sensitive(False)
        cancel.set_sensitive(False)
        status.set_text("Saving encrypted document…")
        def saved(metadata):
            obj.saving = False
            on_saved(metadata)
            obj.closed = True
            obj.close()
        def failed(text):
            obj.saving = False
            cancel.set_sensitive(True)
            save.set_sensitive(True)
            status.set_text(text)
        target_metadata = None if copy.get_active() else dict(previous) if previous else None
        background(obj, lambda: client.write_document(data, title_value, filename_value, media_type, target_metadata), saved, failed)
    dialog.connect("response", respond)
    dialog.present()
    return dialog
