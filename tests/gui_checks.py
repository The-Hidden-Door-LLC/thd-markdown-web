"""Real GTK/WebKit integration checks on an isolated X11 display."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11, GLib
from PIL import ImageGrab

from thd_markdown.app import Application, WELCOME, Gtk


def pump(seconds=.2):
    until = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < until:
        while context.pending():
            context.iteration(False)
        time.sleep(.01)


def wait_for(test, timeout=12):
    until = time.monotonic() + timeout
    while not test() and time.monotonic() < until:
        pump(.05)
    assert test(), "Timed out waiting for application state"


checks = []
def check(condition, name):
    assert condition, name
    checks.append(name)
    print("PASS:", name, flush=True)


qa = Path("qa")
qa.mkdir(exist_ok=True)
app = Application()
app.register(None)
app.activate()
window = app.get_active_window()
pump(1.5)
wid = str(GdkX11.X11Surface.get_xid(window.get_surface()))


def xdo(*args):
    subprocess.run(["xdotool", *args], check=True, timeout=5)
    pump(.2)


def snapshot(name):
    geometry = subprocess.check_output(["xdotool", "getwindowgeometry", "--shell", wid], text=True)
    values = dict(line.split("=", 1) for line in geometry.splitlines())
    x, y, width, height = (int(values[key]) for key in ("X", "Y", "WIDTH", "HEIGHT"))
    ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).crop((x, y, x + width, y + height)).save(qa / name)


check(window.buffer.get_language().get_id() == "markdown", "Markdown syntax language is active")
check(not window.web.get_settings().get_enable_javascript(), "Preview JavaScript is disabled")
check(window.web.get_network_session().is_ephemeral(), "Preview uses an ephemeral session")
check(window.get_width() >= 1300, "Desktop layout at 1440 pixels")
check(window.sidebar.get_visible() and window.web.get_visible(), "Sources, editor, and preview visible on desktop")
snapshot("desktop.png")

xdo("windowsize", wid, "650", "780")
xdo("windowsize", wid, "375", "780")
pump(.5)
check(window.get_width() <= 375, "Window fits 375 pixels without a forced minimum width")
check(not window.sidebar.get_visible() and not window.web.get_visible(), "Narrow layout prioritizes writing")
check(window.view_select.get_selected() == 0, "Narrow mode selector accurately shows Write")
snapshot("narrow-write.png")
window.view_select.set_selected(2)
pump(.3)
check(window.web.get_visible() and not window.edit_scroll.get_visible(), "Narrow Read mode shows the reader")
snapshot("narrow-read.png")
window.sources_button.set_active(True)
pump(.2)
check(window.get_width() <= 375, "Narrow Sources opens as an overlay without widening the window")
snapshot("narrow-sources.png")
window.sources_button.set_active(False)
xdo("windowsize", wid, "1440", "900")
window.view_select.set_selected(1)
pump(.3)

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "notes.md"
    original = "# A local document\n\nKeep this source intact.\n"
    path.write_text(original)
    window.load_source(str(path))
    wait_for(lambda: not window.busy)
    check(window.text() == original and window.local_path == str(path), "Local document loads through the real sidecar process")
    check(not window.buffer.get_modified(), "Loaded document starts clean")

    window.editor.grab_focus()
    window.buffer.place_cursor(window.buffer.get_end_iter())
    xdo("windowfocus", "--sync", wid)
    xdo("type", "--clearmodifiers", "New words")
    check(window.text().endswith("New words") and window.buffer.get_modified(), "Real keyboard input edits the document")
    xdo("key", "--clearmodifiers", "ctrl+s")
    check(path.read_text().endswith("New words") and not window.buffer.get_modified(), "Ctrl+S saves the local file and clears dirty state")

    xdo("type", "--clearmodifiers", "!")
    xdo("key", "--clearmodifiers", "ctrl+z")
    check(not window.text().endswith("!"), "Keyboard Undo restores text")
    xdo("key", "--clearmodifiers", "ctrl+shift+z")
    check(window.text().endswith("!"), "Keyboard Redo restores the edit")
    window.show_find()
    window.find_entry.set_text("local document")
    window.find_next()
    selection = window.buffer.get_selection_bounds()
    check(bool(selection) and window.buffer.get_text(*selection, True) == "local document", "Find selects a real document match")
    window.findbar.set_visible(False)

    before = window.text()
    path.write_text("External edit")
    check(not window.write_to(str(path), window.source_hash), "External edit blocks Save")
    check(path.read_text() == "External edit" and window.text() == before, "Save conflict preserves both disk and editor text")

    window.load_source("s3://example-bucket/notes.md")
    wait_for(lambda: not window.busy)
    check(window.text() == before and window.buffer.get_modified(), "Failed S3 load preserves dirty document")
    check("not connected" in window.notice.get_text(), "AWS stub reports an explicit error")

    copy = Path(directory) / "copy.md"
    check(window.write_to(str(copy), create_only=True), "Save a local copy succeeds")
    check(copy.read_text() == before and window.local_path == str(copy), "Saved copy becomes the local editing target")
    window.buffer.insert_at_cursor("unsaved")
    before_discard = window.text()
    window.new_document()
    pump(.3)
    # Escape cancels the actual GTK discard dialog.
    dialogs = [top for top in Gtk.Window.list_toplevels() if top is not window and top.get_visible()]
    check(bool(dialogs), "Unsaved New opens a discard confirmation")
    xdo("windowfocus", "--sync", str(GdkX11.X11Surface.get_xid(dialogs[-1].get_surface())))
    xdo("key", "Escape")
    pump(.3)
    check(window.text() == before_discard and window.buffer.get_modified(), "Canceling discard preserves unsaved text")

window._set_document(WELCOME)
pump(.5)
snapshot("desktop-final.png")
window.finish_close()
pump(.2)
(qa / "gui-results.json").write_text(json.dumps({"passed":len(checks), "checks":checks}, indent=2) + "\n")
print(f"ALL {len(checks)} GUI CHECKS PASSED")
