import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from thd_sources import SourceError, providers, read, resolve
from thd_sources.__main__ import dispatch, MAX_REQUEST
from thd_sources.core import MAX_BYTES


class SourceTests(unittest.TestCase):
    def assert_code(self, code, function, *args):
        with self.assertRaises(SourceError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_provider_identity_survives_resolution(self):
        google = resolve("gs://example-bucket/notes.md")
        aws = resolve("s3://example-bucket/notes.md")
        self.assertEqual(google.provider, "gcs")
        self.assertEqual(aws.provider, "s3")
        self.assertNotEqual(google.uri, aws.uri)

    def test_unicode_and_percent_decode_once(self):
        source = resolve("s3://example-bucket/caf%C3%A9/%2520%23%3F.md")
        self.assertEqual(source.path, "café/%20#?.md")
        self.assertEqual(resolve(source.uri), source)

    def test_remote_dot_segments_and_slashes_are_literal(self):
        source = resolve("s3://example-bucket/a/../b//c")
        self.assertEqual(source.path, "a/../b//c")
        self.assertEqual(source.uri, "s3://example-bucket/a/../b//c")

    def test_local_path_is_literal(self):
        source = resolve("/tmp/a%20b #?.md")
        self.assertEqual(source.path, "/tmp/a%20b #?.md")
        self.assertEqual(resolve(source.uri), source)

    def test_dropbox_is_accountless(self):
        source = resolve("dropbox:///Notes/first%20draft.md")
        self.assertEqual(source.provider, "dropbox")
        self.assertEqual(source.path, "/Notes/first draft.md")
        self.assert_code("invalid_source", resolve, "dropbox://account/Notes/file.md")

    def test_bucket_roots_resolve(self):
        self.assertEqual(resolve("s3://example-bucket").path, "")
        self.assertEqual(resolve("gs://example-bucket/").uri, "gs://example-bucket/")

    def test_encoding_cannot_produce_an_unusable_address(self):
        for address in ("/" + "é" * 1364, "gs://example-bucket/" + "é" * 1362,
                        "dropbox:///" + "é" * 1363, "s3://example-bucket/" + "é" * 1362):
            with self.subTest(address=address[:30]):
                source = resolve(address)
                self.assertEqual(resolve(source.uri), source)
                self.assert_code("invalid_source", resolve, address + "é" * 10)

    def test_invalid_addresses(self):
        invalid = [None, 17, "", "/" * 8193, "/tmp/\x00a", "s3://u:p@example-bucket/file",
                   "s3://example-bucket:443/file", "s3://example-bucket/file?token=x",
                   "s3://example-bucket/file#rev", "s3://example-bucket/a%2",
                   "s3://example-bucket/%FF", "s3://example-bucket/%00",
                   "file://remote/etc/passwd", "gs:///file", "s3://EXAMPLE/file", "/tmp/\ud800"]
        for address in invalid:
            with self.subTest(address=repr(address)):
                self.assert_code("invalid_source", resolve, address)

    def test_unknown_provider(self):
        self.assert_code("unsupported_provider", resolve, "https://example.com/a")
        self.assert_code("unsupported_provider", resolve, "relative.md")

    def test_stubs_never_attempt_a_local_read(self):
        with patch("thd_sources.core._read_local", side_effect=AssertionError("fallback")), \
                patch("thd_sources.remote.gcloud_path", return_value=None), \
                patch("thd_sources.remote.dropbox_path", return_value=None):
            for uri in ("s3://example-bucket/a", "gs://example-bucket/a", "dropbox:///a"):
                with self.subTest(uri=uri):
                    self.assert_code("provider_not_connected", read, uri)

    def test_capabilities_cannot_be_mutated_by_consumer(self):
        first = providers()
        first[-1]["can_read"] = True
        self.assertFalse(providers()[-1]["can_read"])
        self.assertEqual(providers()[-1]["state"], "stub")
        self.assertTrue(all(not row["can_write"] for row in providers()))

    def test_binary_bytes_hash_and_unchanged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.bin"
            payload = b"\x00\xff\x89PNG\r\n"
            path.write_bytes(payload)
            result = read(str(path), len(payload))
            self.assertEqual(base64.b64decode(result["content"]), payload)
            self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(result["size"], len(payload))
            self.assertFalse(result["cached"])
            self.assertEqual(path.read_bytes(), payload)
            self.assert_code("too_large", read, str(path), len(payload) - 1)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile() as file:
            self.assertEqual(read(file.name)["content"], "")

    def test_missing_directory_fifo_and_device(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assert_code("not_found", read, directory + "/absent")
            self.assert_code("not_a_file", read, directory)
            fifo = directory + "/fifo"
            os.mkfifo(fifo)
            self.assert_code("not_a_file", read, fifo)
            self.assert_code("not_a_file", read, "/dev/null")

    def test_size_limits_reject_wrong_types_and_values(self):
        for limit in (True, False, None, "2", 1.5, 0, -1, MAX_BYTES + 1):
            with self.subTest(limit=limit):
                self.assert_code("invalid_limit", read, "/unused", limit)

    def test_access_error_is_sanitized(self):
        with patch("thd_sources.core.os.open", side_effect=PermissionError("private details")):
            response = dispatch({"version": 1, "method": "read", "source": "/somewhere"})
        self.assertEqual(response["error"]["code"], "access_denied")
        self.assertNotIn("private details", json.dumps(response))

    def test_detects_file_change_during_read(self):
        with tempfile.NamedTemporaryFile() as file:
            file.write(b"abc")
            file.flush()
            before = os.stat(file.name)
            after = SimpleNamespace(st_size=before.st_size, st_mtime_ns=before.st_mtime_ns + 1,
                                    st_ctime_ns=before.st_ctime_ns)
            with patch("thd_sources.core.os.fstat", side_effect=[before, after]):
                self.assert_code("source_changed", read, file.name)


class TransportTests(unittest.TestCase):
    def run_sidecar(self, payload):
        result = subprocess.run([sys.executable, "-m", "thd_sources"], input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=Path(__file__).resolve().parents[1], timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_multiple_requests_and_recovery(self):
        responses = self.run_sidecar(
            b'{"version":1,"id":"a","method":"providers"}\n'
            b'bad json\n'
            b'{"version":1,"id":2,"method":"read","source":"s3://example-bucket/a"}\n')
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["id"], "a")
        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[1]["error"]["code"], "invalid_json")
        self.assertEqual(responses[2]["error"]["code"], "provider_not_connected")

    def test_oversized_request_does_not_consume_next(self):
        responses = self.run_sidecar(b"x" * (MAX_REQUEST * 3) + b'\n{"version":1,"method":"providers"}\n')
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], "request_too_large")
        self.assertTrue(responses[1]["ok"])

    def test_bad_shape_version_id_and_method(self):
        cases = [([], "invalid_request"), ({"version": 1, "id": []}, "invalid_request"),
                 ({"version": True}, "unsupported_version"), ({"version": 2}, "unsupported_version"),
                 ({"version": 1, "method": "write"}, "unsupported_method"),
                 ({"version": 1, "method": "read"}, "invalid_source")]
        for request, code in cases:
            with self.subTest(request=request):
                self.assertEqual(dispatch(request)["error"]["code"], code)

    def test_read_bytes_through_real_subprocess(self):
        with tempfile.NamedTemporaryFile() as file:
            file.write("# THD Markdown\n\nHello, café.\n".encode())
            file.flush()
            request = {"version": 1, "id": 42, "method": "read", "source": file.name}
            response = self.run_sidecar(json.dumps(request).encode() + b"\n")[0]
            self.assertEqual(response["id"], 42)
            self.assertTrue(response["ok"])
            self.assertIn("café", base64.b64decode(response["result"]["content"]).decode())

    def test_invalid_utf8_and_deep_json_recover(self):
        responses = self.run_sidecar(b'\xff\n' + b'[' * 2000 + b']' * 2000 + b'\n{"version":1,"method":"providers"}\n')
        self.assertEqual(responses[0]["error"]["code"], "invalid_json")
        self.assertIn(responses[1]["error"]["code"], ("invalid_json", "invalid_request"))
        self.assertTrue(responses[2]["ok"])

    def test_transport_rejects_utf16(self):
        response = self.run_sidecar('{"version":1,"method":"providers"}'.encode("utf-16"))[0]
        self.assertEqual(response["error"]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
