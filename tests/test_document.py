import tempfile
import os
from pathlib import Path
import unittest

from thd_markdown.document import MAX_DOCUMENT, decode_document, digest, save_document
from thd_markdown.preview import render


class DocumentTests(unittest.TestCase):
    def test_new_file_roundtrip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "café.md"
            sha = save_document(path, "# Café\n", create_only=True)
            self.assertEqual(decode_document(path.read_bytes()), "# Café\n")
            self.assertEqual(sha, digest(path.read_bytes()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".thd-markdown-*")), [])

    def test_external_edit_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            sha = save_document(path, "first", create_only=True)
            path.write_text("someone else's edit")
            with self.assertRaisesRegex(ValueError, "changed outside"):
                save_document(path, "my edit", sha)
            self.assertEqual(path.read_text(), "someone else's edit")

    def test_removed_original_cannot_be_recreated_by_save(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "removed"):
                save_document(Path(directory) / "missing.md", "edit", "old-hash")

    def test_external_replacement_with_fifo_never_blocks_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replaced.md"
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "regular file"):
                save_document(path, "edit", "old-hash")

    def test_save_as_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text("keep me")
            with self.assertRaises(FileExistsError):
                save_document(path, "new", create_only=True)
            self.assertEqual(path.read_text(), "keep me")
            self.assertEqual(list(Path(directory).glob(".thd-markdown-*")), [])

    def test_symlink_cannot_redirect_save(self):
        with tempfile.TemporaryDirectory() as directory:
            target, link = Path(directory) / "original", Path(directory) / "link"
            target.write_text("keep")
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic"):
                save_document(link, "edit")
            self.assertEqual(target.read_text(), "keep")

    def test_valid_save_updates_hash_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            before = save_document(path, "first", create_only=True)
            path.chmod(0o640)
            after = save_document(path, "second", before)
            self.assertEqual(path.read_text(), "second")
            self.assertNotEqual(before, after)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_strict_text_and_size_boundaries(self):
        self.assertEqual(decode_document(b"\xef\xbb\xbf# UTF-8"), "# UTF-8")
        for payload in (b"bad\x00data", b"\xff", b"x" * (MAX_DOCUMENT + 1)):
            with self.assertRaises((ValueError, UnicodeError)):
                decode_document(payload)

    def test_preview_escapes_html_and_never_embeds_images(self):
        result = render('<script>alert(1)</script>\n\n![alt](https://example.com/tracker)\n\n[x](javascript:alert(1))')
        self.assertNotIn("<script>", result)
        self.assertNotIn("<img", result)
        self.assertNotIn('href="javascript:', result)
        self.assertIn("script-src 'none'", result)
        self.assertIn("[Image: alt]", result)

    def test_reader_supports_tables_code_and_strikethrough(self):
        result = render("# Title\n\n| A | B |\n|---|---|\n| X | Y |\n\n```python\nprint('hi')\n```\n\n~~gone~~")
        for tag in ("<h1>", "<table>", '<code class="language-python">', "<s>gone</s>"):
            self.assertIn(tag, result)


if __name__ == "__main__":
    unittest.main()
