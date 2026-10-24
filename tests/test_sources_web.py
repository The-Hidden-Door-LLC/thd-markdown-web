import gzip
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thd_sources import SourceError
from thd_sources.sitemap import https_url, parse_map, discover, site_url
from thd_sources.web import convert_html, get_page, get_site
from thd_sources.directory import export
from thd_sources.store import cache_path, read_json, write_json


SITE = "https://example.test"
MAP = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/article?a=1&amp;b=2</loc><lastmod>2026-10-23</lastmod></url></urlset>'


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CACHE_HOME":self.temporary.name+"/cache", "XDG_DATA_HOME":self.temporary.name+"/data"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_code(self, code, call, *args):
        with self.assertRaises(SourceError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)

    def test_address_unicode_and_scheme_validation(self):
        self.assertEqual(https_url("example.test/a b"), "https://example.test/a%20b")
        for bad in (None, "", "http://example.test", "file:///etc/passwd", "https://u:p@example.test", "https://example.test:bad", "https://example.test/a\nb"):
            self.assert_code("invalid_url", https_url, bad)

    def test_cross_origin_sitemap_links_never_followed(self):
        self.assertIsNone(site_url("https://other.test/private", SITE, SITE+"/"))
        self.assertEqual(site_url("http://example.test/a", SITE, SITE+"/"), SITE+"/a")

    def test_xml_entities_and_extensions_do_not_replace_page_locations(self):
        kind, pages = parse_map(MAP)
        self.assertEqual(kind, "urlset")
        self.assertEqual(pages[0]["url"], SITE+"/article?a=1&b=2")
        extended = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://example.test/image"><url><loc>https://example.test/page</loc><image:loc>https://example.test/img</image:loc></url></urlset>'
        self.assertEqual(parse_map(extended)[1][0]["url"], SITE+"/page")

    def test_dtd_rejected_in_utf8_and_utf16(self):
        xml = '<!DOCTYPE urlset [<!ENTITY x "expand">]><urlset><url><loc>&x;</loc></url></urlset>'
        for payload in (xml.encode(), xml.encode("utf-16")):
            self.assert_code("invalid_sitemap", parse_map, payload)

    def test_robots_nested_index_deduplicates_and_ignores_external_pages(self):
        def fetch(url, *args):
            if url.endswith("/robots.txt"):
                return b"User-agent: *\nSitemap: https://example.test/maps/index.xml\n"
            if url.endswith("/maps/index.xml"):
                return b'<sitemapindex><sitemap><loc>https://example.test/maps/pages.xml.gz</loc></sitemap></sitemapindex>'
            if url.endswith("/maps/pages.xml.gz"):
                return MAP
            raise SourceError("not_found", "missing")
        with patch("thd_sources.sitemap.fetch", side_effect=fetch):
            result = discover(SITE)
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(len(result["sitemaps"]), 2)
        self.assertFalse(result["missing_sitemap"])

    def test_missing_sitemap_requires_conclusive_absence(self):
        with patch("thd_sources.sitemap.fetch", side_effect=SourceError("not_found", "missing")):
            result = get_site(SITE)
        self.assertTrue(result["missing_sitemap"])
        index = read_json(Path(result["directory"]["json"]))
        self.assertEqual(index["entries"][0]["status"], "missing_sitemap")
        self.assertEqual(len(index["entries"][0]["checks"]), 3)
        self.assertFalse(result["directory"]["published"])

    def test_network_auth_parser_and_limits_never_enter_directory(self):
        for code in ("connection_failed", "timeout", "access_denied", "invalid_sitemap", "too_large"):
            with patch("thd_sources.sitemap.fetch", side_effect=SourceError(code, "failed")):
                result = get_site(SITE, refresh=True)
            self.assertFalse(result["missing_sitemap"])
            self.assertEqual(read_json(Path(result["directory"]["json"]))["entries"], [])

    def test_external_published_sitemap_is_not_misreported_missing(self):
        def fetch(url, *args):
            if url.endswith("robots.txt"):
                return b"Sitemap: https://cdn.example.test/sitemap.xml\n"
            raise SourceError("not_found", "missing")
        with patch("thd_sources.sitemap.fetch", side_effect=fetch):
            self.assertFalse(discover(SITE)["missing_sitemap"])

    def test_explicit_sitemap_address_is_used(self):
        def fetch(url, *args):
            if url.endswith("custom.xml"):
                return MAP
            raise SourceError("not_found", "missing")
        with patch("thd_sources.sitemap.fetch", side_effect=fetch):
            result = get_site(SITE+"/custom.xml")
        self.assertEqual(result["sitemaps"], [SITE+"/custom.xml"])

    def test_site_map_cache_is_available_without_network(self):
        with patch("thd_sources.sitemap.fetch", return_value=MAP):
            get_site(SITE)
        with patch("thd_sources.sitemap.fetch", side_effect=AssertionError("network access")):
            self.assertTrue(get_site(SITE)["cached"])

    def test_directory_recheck_marks_resolved_preserving_record(self):
        with patch("thd_sources.sitemap.fetch", side_effect=SourceError("not_found", "missing")):
            get_site(SITE)
        with patch("thd_sources.sitemap.fetch", return_value=MAP):
            result = get_site(SITE, refresh=True)
        self.assertEqual(read_json(Path(result["directory"]["json"]))["entries"][0]["status"], "resolved")

    def test_html_to_markdown_has_headings_links_tables_and_no_images_or_scripts(self):
        title, markdown = convert_html('<html><head><title>Clear writing</title></head><body><nav>noise</nav><main><h1>Hello</h1><p>Read <a href="/next">next</a>.</p><img src="/tracker.png"><script>secret()</script><table><tr><th>A</th></tr><tr><td>B</td></tr></table></main></body></html>', SITE+"/")
        self.assertEqual(title, "Clear writing")
        self.assertIn("# Hello", markdown)
        self.assertIn("[next](https://example.test/next)", markdown)
        for absent in ("noise", "tracker", "secret", "<script", "<img"):
            self.assertNotIn(absent, markdown)
        self.assertIn("| A |", markdown)

    def test_unsafe_links_are_text_only(self):
        _, markdown = convert_html('<main><a href="file:///etc/passwd">file</a><a href="javascript:alert(1)">js</a></main>', SITE)
        self.assertNotIn("file:", markdown)
        self.assertNotIn("javascript:", markdown)

    def test_page_cache_fidelity_offline_and_explicit_refresh(self):
        with patch("thd_sources.web.fetch_page", return_value=(SITE+"/", "<main><h1>Hello</h1><p>World</p></main>", "text/html")) as fetch:
            first = get_page(SITE)
            second = get_page(SITE)
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(first["markdown"], second["markdown"])
            self.assertEqual(second["sha256"], hashlib.sha256(second["markdown"].encode()).hexdigest())
            get_page(SITE, refresh=True)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(cache_path("pages", SITE+"/").stat().st_mode & 0o777, 0o600)

    def test_website_markdown_is_not_reinterpreted_as_html(self):
        with patch("thd_sources.web.fetch_page", return_value=(SITE+"/", "# Already Markdown\n", "text/markdown")):
            self.assertEqual(get_page(SITE)["markdown"], "# Already Markdown\n")

    def test_bad_refresh_preserves_old_cache(self):
        with patch("thd_sources.web.fetch_page", return_value=(SITE+"/", "# Keep me", "text/markdown")):
            before = get_page(SITE)
        with patch("thd_sources.web.fetch_page", side_effect=SourceError("connection_failed", "offline")):
            self.assert_code("connection_failed", get_page, SITE, True)
            self.assertEqual(get_page(SITE)["markdown"], before["markdown"])

    def test_malformed_cache_metadata_is_not_accepted(self):
        write_json(cache_path("sites", SITE), {"pages":[]})
        with patch("thd_sources.sitemap.fetch", return_value=MAP):
            self.assertFalse(get_site(SITE)["cached"])
        with patch("thd_sources.web.fetch_page", return_value=(SITE+"/", "# Correct", "text/markdown")):
            first = get_page(SITE)
            first["requested_url"] = "https://other.test/"
            write_json(cache_path("pages", SITE+"/"), first)
            self.assertFalse(get_page(SITE)["cached"])

    def test_known_custom_sitemap_is_not_forgotten_on_domain_recheck(self):
        def fetch(url, *args):
            if url.endswith("/custom.xml"):
                return MAP
            raise SourceError("not_found", "missing")
        with patch("thd_sources.sitemap.fetch", side_effect=fetch):
            get_site(SITE+"/custom.xml")
            result = get_site(SITE, refresh=True)
        self.assertFalse(result["missing_sitemap"])
        self.assertIn(SITE+"/custom.xml", result["sitemaps"])
        self.assertEqual(read_json(Path(result["directory"]["json"]))["entries"], [])


if __name__ == "__main__":
    unittest.main()
