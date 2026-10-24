import base64
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from thd_sources import read,resolve,SourceError
from thd_sources.remote import read_gcs,read_dropbox,_google_get

class RemoteTests(unittest.TestCase):

    def assert_code(self, code, function, *args):
        with self.assertRaises(SourceError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_gcs_preserves_literal_key_and_pins_generation(self):
        with patch('thd_sources.remote.gcloud_path', return_value='gcloud'), patch('thd_sources.remote._run', return_value='private-token\n'), patch('thd_sources.remote._google_get', side_effect=[b'{"size":"3","generation":"42"}', b'abc']) as get:
            result = read_gcs(resolve('gs://test-bucket/a/../%23%3F.md'), 3)
        self.assertEqual(result, b'abc')
        self.assertIn('a%2F..%2F%23%3F.md', get.call_args_list[0].args[0])
        self.assertIn('generation=42', get.call_args_list[1].args[0])

    def test_gcs_rejects_size_before_download(self):
        with patch('thd_sources.remote.gcloud_path', return_value='gcloud'), patch('thd_sources.remote._run', return_value='token'), patch('thd_sources.remote._google_get', return_value=b'{"size":"4","generation":"42"}') as get:
            self.assert_code('too_large', read_gcs, resolve('gs://test-bucket/file'), 3)
        self.assertEqual(get.call_count, 1)

    def test_gcs_http_errors_do_not_leak_tokens_or_response(self):
        for status, code in ((401, 'auth_required'), (403, 'access_denied'), (404, 'not_found'), (412, 'source_changed'), (500, 'read_failed')):
            error = HTTPError('https://storage.googleapis.com/', status, 'secret response', {}, io.BytesIO(b'secret'))
            with patch('thd_sources.remote.urlopen', side_effect=error):
                self.assert_code(code, _google_get, 'https://storage.googleapis.com/', 'private-token', 5)

    def test_gcs_detects_short_body(self):
        with patch('thd_sources.remote.gcloud_path', return_value='gcloud'), patch('thd_sources.remote._run', return_value='token'), patch('thd_sources.remote._google_get', side_effect=[b'{"size":"3","generation":"42"}', b'ab']):
            self.assert_code('source_changed', read_gcs, resolve('gs://test-bucket/file'), 3)

    def test_remote_read_never_falls_back_to_local(self):
        with patch('thd_sources.remote.gcloud_path', return_value='gcloud'), patch('thd_sources.remote.read_gcs', side_effect=SourceError('access_denied', 'Denied')), patch('thd_sources.core._read_local', side_effect=AssertionError('local fallback')):
            self.assert_code('access_denied', read, 'gs://test-bucket/a')

    def test_s3_remains_unconditionally_disabled(self):
        self.assert_code('provider_not_connected', read, 's3://test-bucket/a')

class PortableDropboxTests(unittest.TestCase):
    def response(self, data, size=None):
        class Response(io.BytesIO):
            pass
        result=Response(data)
        result.headers={"Dropbox-API-Result":json.dumps({"size":len(data) if size is None else size})}
        return result

    def test_download_is_a_single_authenticated_response_with_bound_metadata(self):
        with patch.dict(os.environ,{"DROPBOX_ACCESS_TOKEN":"synthetic-test-token"}), patch("thd_sources.remote.urlopen",return_value=self.response(b"abc")) as open_request:
            self.assertEqual(read_dropbox(resolve("dropbox:///notes/doc.md"),3),b"abc")
        request=open_request.call_args.args[0]
        self.assertEqual(request.get_method(),"POST")
        self.assertIsNone(request.data)
        self.assertEqual(json.loads(request.get_header("Dropbox-api-arg")),{"path":"/notes/doc.md"})

    def test_oversize_file_is_rejected_before_read(self):
        response=self.response(b"abc",4)
        with patch.dict(os.environ,{"DROPBOX_ACCESS_TOKEN":"synthetic-test-token"}), patch("thd_sources.remote.urlopen",return_value=response):
            with self.assertRaises(SourceError) as caught:read_dropbox(resolve("dropbox:///doc.md"),3)
        self.assertEqual(caught.exception.code,"too_large")

    def test_metadata_size_mismatch_is_rejected(self):
        with patch.dict(os.environ,{"DROPBOX_ACCESS_TOKEN":"synthetic-test-token"}), patch("thd_sources.remote.urlopen",return_value=self.response(b"ab",3)):
            with self.assertRaises(SourceError) as caught:read_dropbox(resolve("dropbox:///doc.md"),3)
        self.assertEqual(caught.exception.code,"source_changed")

    def test_missing_token_never_attempts_network(self):
        with patch.dict(os.environ,{"DROPBOX_ACCESS_TOKEN":""}), patch("thd_sources.remote.urlopen",side_effect=AssertionError("network")):
            with self.assertRaises(SourceError) as caught:read_dropbox(resolve("dropbox:///doc.md"),3)
        self.assertEqual(caught.exception.code,"auth_required")
