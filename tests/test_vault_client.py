import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from thd_vault import client


class VaultDocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vault.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.path.chmod(0o600)
        self.server.listen()
        self.server.settimeout(.1)
        self.stopped = threading.Event()
        self.body = b"# A synthetic document\n"
        self.info = {"id":"12345678-1234-4123-8123-123456789abc", "title":"Synthetic document", "filename":"document.md", "mediaType":"text/markdown", "size":len(self.body), "sha256":hashlib.sha256(self.body).hexdigest(), "revision":"revision-one"}
        self.unlocked = True
        self.supported = True
        self.received = []
        self.force_error = None
        def run():
            while not self.stopped.is_set():
                try:
                    channel, _ = self.server.accept()
                except (OSError, TimeoutError):
                    continue
                with channel:
                    wire = channel.makefile("rb").readline(client.MAX_FRAME+1)
                    if not wire:
                        continue
                    request = json.loads(wire)
                    self.received.append(request)
                    action, payload = request["action"], request["payload"]
                    error = None
                    if action == "status":
                        result = {"unlocked":self.unlocked, "documents":{"supported":self.supported}, "project":"not-returned-to-consumers"}
                    elif not self.unlocked:
                        result, error = None, "Vault is locked"
                    elif action == "documents-list":
                        result = [self.info]
                    elif action == "documents-read":
                        result = {**self.info,"contentBase64":base64.b64encode(self.body).decode()}
                    elif action == "documents-write":
                        if payload.get("id") and payload.get("expectedRevision") != self.info["revision"]:
                            result, error = None, "Revision conflict"
                        else:
                            self.body = base64.b64decode(payload["contentBase64"])
                            self.info = {**self.info, "title":payload["title"], "filename":payload["filename"], "mediaType":payload["mediaType"], "size":len(self.body), "sha256":hashlib.sha256(self.body).hexdigest(), "revision":"revision-two"}
                            result = self.info
                    else:
                        result, error = None, "Unsupported action"
                    reply = {"id":request["id"],"ok":error is None,"data":result,"error":error}
                    if self.force_error and action != "status":
                        reply.update(ok=False,error=self.force_error[1],code=self.force_error[0])
                    channel.sendall(json.dumps(reply).encode()+b"\n")
        self.thread = threading.Thread(target=run,daemon=True)
        self.thread.start()
        def stop():
            self.stopped.set()
            self.server.close()
            self.thread.join(1)
        self.addCleanup(stop)
        self.env = patch.dict(os.environ,{"HDV_SOCKET":str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_list_returns_document_metadata_only(self):
        result = client.list_documents()
        self.assertEqual(result,[self.info])
        self.assertTrue(all(request["payload"]["autoUnlock"] is False for request in self.received))
        self.assertNotIn("contentBase64",result[0])
        self.assertNotIn("project",result[0])

    def test_read_checks_byte_integrity(self):
        metadata, body = client.read_document(self.info["id"])
        self.assertEqual(body,self.body)
        self.assertEqual(metadata["sha256"],hashlib.sha256(body).hexdigest())

    def test_save_new_document_uses_no_implicit_update(self):
        result = client.write_document(b"# New\n","New","new.md","text/markdown")
        payload = self.received[-1]["payload"]
        self.assertNotIn("id",payload)
        self.assertNotIn("expectedRevision",payload)
        self.assertEqual(result["sha256"],hashlib.sha256(b"# New\n").hexdigest())

    def test_update_requires_the_previous_revision(self):
        old = dict(self.info)
        client.write_document(b"# New\n","New","new.md","text/markdown",old)
        with self.assertRaises(client.VaultError) as caught:
            client.write_document(b"# Stale\n","Stale","new.md","text/markdown",old)
        self.assertEqual(caught.exception.code,"conflict")
        self.assertEqual(self.body,b"# New\n")

    def test_locked_vault_never_reads_or_writes(self):
        self.unlocked = False
        with self.assertRaises(client.VaultError) as caught:
            client.read_document(self.info["id"])
        self.assertEqual(caught.exception.code,"locked")
        self.assertEqual([item["action"] for item in self.received],["status"])

    def test_old_vault_version_does_not_receive_document_content(self):
        self.supported = False
        with self.assertRaises(client.VaultError) as caught:
            client.write_document(b"text","Document","doc.md","text/markdown")
        self.assertEqual(caught.exception.code,"unsupported")
        self.assertEqual([item["action"] for item in self.received],["status"])

    def test_socket_permissions_are_checked(self):
        self.path.chmod(0o666)
        with self.assertRaises(client.VaultError) as caught:
            client.ready()
        self.assertEqual(caught.exception.code,"unsafe_socket")
        self.assertEqual(self.received,[])

    def test_changed_bytes_fail_hash_verification(self):
        self.body = b"Changed without metadata"
        with self.assertRaises(client.VaultError) as caught:
            client.read_document(self.info["id"])
        self.assertEqual(caught.exception.code,"invalid_response")

    def test_read_receipt_must_match_requested_document(self):
        requested=self.info["id"]
        self.info={**self.info,"id":"87654321-1234-4123-8123-123456789abc"}
        with self.assertRaises(client.VaultError) as caught:
            client.read_document(requested)
        self.assertEqual(caught.exception.code,"invalid_response")

    def test_update_receipt_must_match_target_document(self):
        old=dict(self.info)
        self.info={**self.info,"id":"87654321-1234-4123-8123-123456789abc"}
        with self.assertRaises(client.VaultError) as caught:
            client.write_document(b"Updated","Document","document.md","text/markdown",old)
        self.assertEqual(caught.exception.code,"unknown_outcome")

    def test_unsupported_peer_platform_fails_before_request(self):
        with patch.object(client.sys,"platform","unsupported-platform"):
            with self.assertRaises(client.VaultError) as caught:
                client.ready()
        self.assertEqual(caught.exception.code,"unsafe_socket")
        self.assertEqual(self.received,[])

    def test_malformed_post_write_receipt_is_unknown_outcome(self):
        with patch.object(client,"metadata",side_effect=client.VaultError("invalid", "invalid_response")):
            with self.assertRaises(client.VaultError) as caught:
                client.write_document(b"New bytes","Document","document.md","text/markdown")
        self.assertEqual(caught.exception.code,"unknown_outcome")
        self.assertEqual(self.body,b"New bytes")

    def test_typed_server_outcomes_override_sanitized_attachment_message(self):
        for code,expected in (("commit_unconfirmed","unknown_outcome"),("revision_conflict","conflict"),("vault_locked","locked"),("storage_not_configured","not_configured")):
            self.force_error=(code,"An encrypted attachment was uploaded, but its metadata was not confirmed.")
            with self.assertRaises(client.VaultError) as caught:
                client.write_document(b"Bytes","Document","document.md","text/markdown")
            self.assertEqual(caught.exception.code,expected)

    def test_invalid_name_metadata_is_rejected(self):
        for value in ("../secret.md", "folder/name.md", "folder\\name.md", ".", ".."):
            with self.assertRaises(client.VaultError):
                client.metadata({**self.info,"filename":value})


if __name__ == "__main__":
    unittest.main()
