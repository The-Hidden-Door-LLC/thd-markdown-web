import importlib.util
from pathlib import Path
import socket
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from thd_sources import SourceError

script=Path(__file__).resolve().parents[1]/'scripts/publish-directory.py'
spec=importlib.util.spec_from_file_location('publisher',script)
publisher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicDirectoryTests(unittest.TestCase):
    def test_only_public_origin_names_are_accepted(self):
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
        with patch.object(publisher.socket,'getaddrinfo',return_value=answer):
            self.assertEqual(publisher.public_domain('https://example.org/'),'example.org')
            for address in ('http://example.org','https://name@example.org','https://example.org/path','https://example.org/?value=example','https://example.org:444'):
                with self.assertRaises(ValueError):publisher.public_domain(address)

    def test_private_network_answers_cannot_be_public_checks(self):
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(publisher.socket,'getaddrinfo',return_value=answer):
            with self.assertRaises(ValueError):publisher.public_domain('example.org')

    def test_mixed_private_and_public_answers_are_rejected(self):
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(publisher.socket,'getaddrinfo',return_value=answer):
            with self.assertRaises(ValueError):publisher.public_domain('example.org')

    def test_custom_sitemaps_survive_isolated_public_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'directory').mkdir()
            (root/'directory/targets.json').write_text('[]')
            (root/'directory/sitemaps.json').write_text('{}')
            first=[True]
            def fetch(url,*args):
                if first[0] and url.endswith('/robots.txt'):return b'Sitemap: https://example.test/custom.xml\n'
                if url.endswith('/custom.xml'):return b'<urlset><url><loc>https://example.test/page</loc></url></urlset>'
                raise SourceError('not_found','missing')
            with patch.object(publisher,'ROOT',root), patch.object(publisher,'public_domain',return_value='example.test'), patch('thd_sources.sitemap.fetch',side_effect=fetch), patch.dict(os.environ,{'CHECK_DOMAIN':'example.test','PAGES_DOMAIN':''}):
                publisher.main()
                first[0]=False
                publisher.main()
            self.assertEqual(json.loads((root/'site/index.json').read_text())['entries'],[])
            self.assertIn('https://example.test/custom.xml',json.loads((root/'directory/sitemaps.json').read_text())['example.test']['urls'])
