"""Native acceptance probe; uses only a synthetic owner-only Unix socket."""

import importlib.util
from pathlib import Path
import time

from thd_markdown.app import Application, Gtk, GLib
from thd_vault.gtk_ui import VaultPicker

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('fixture',root/'tests/test_vault_client.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
server=fixture.VaultDocumentTests('test_list_returns_document_metadata_only');server.setUp()
def pump(seconds=.1):
    end=time.monotonic()+seconds
    context=GLib.MainContext.default()
    while time.monotonic()<end:
        while context.pending():context.iteration(False)
        time.sleep(.01)
def wait(predicate):
    end=time.monotonic()+8
    while not predicate() and time.monotonic()<end:pump()
    assert predicate(),'GUI state did not settle'
def dialog(title):
    return next((item for item in Gtk.Window.list_toplevels() if item.get_visible() and item.get_title()==title),None)
try:
    app=Application();app.register(None);app.activate();editor=app.get_active_window();pump(.3)
    editor.open_vault()
    picker=dialog('Open from Vault');assert isinstance(picker,VaultPicker)
    wait(lambda:bool(picker.items))
    picker.list.emit('row-activated',picker.list.get_row_at_index(0))
    wait(lambda:editor.source and editor.source['provider']=='vault')
    assert editor.text()==server.body.decode() and editor.local_path is None
    print('PASS: native Vault picker opens document bytes in memory')
    editor.buffer.insert_at_cursor('Updated ')
    submitted=editor.text()
    editor.save_vault();save=dialog('Save to Vault');assert save
    save.response(Gtk.ResponseType.ACCEPT)
    wait(lambda:editor.vault_document['revision']=='revision-two')
    assert server.body.decode()==submitted and not editor.buffer.get_modified()
    print('PASS: native save updates Vault by revision and clears only confirmed dirty content')
    editor.buffer.insert_at_cursor('Conflict ')
    dirty=editor.text();server.info={**server.info,'revision':'external-revision'}
    editor.save_vault();save=dialog('Save to Vault');save.response(Gtk.ResponseType.ACCEPT)
    wait(lambda:not save.saving)
    assert editor.text()==dirty and editor.buffer.get_modified()
    save.response(Gtk.ResponseType.CANCEL);pump(.1)
    print('PASS: revision conflict/cancel preserves unsaved editor text')
    editor.buffer.set_modified(False);server.unlocked=False
    editor.open_vault();picker=dialog('Open from Vault')
    wait(lambda:'unlock' in picker.status.get_text().lower())
    assert not picker.items
    picker.close();server.unlocked=True
    print('PASS: locked Vault shows unlock-app message and no document list')
    browser=app.browser_window();browser.present();pump(.2)
    browser.open_vault();picker=dialog('Open from Vault');wait(lambda:bool(picker.items))
    picker.list.emit('row-activated',picker.list.get_row_at_index(0))
    wait(lambda:browser.current and browser.current.get('vault'))
    assert browser.current['markdown']==server.body.decode()
    assert not browser.web.get_settings().get_enable_javascript()
    print('PASS: Markdown Web reads a Vault document without live-page navigation')
    browser.close();editor.buffer.set_modified(False);editor.close();pump(.2)
finally:
    server.doCleanups()
