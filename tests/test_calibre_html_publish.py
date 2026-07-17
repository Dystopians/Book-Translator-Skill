import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import calibre_html_publish  # noqa: E402


class ConversionWorkspaceTests(unittest.TestCase):
    def test_main_preserves_preexisting_legacy_temp_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_html = Path(temp_dir) / "book.html"
            output_file = Path(temp_dir) / "book.epub"
            legacy_dir = Path(temp_dir) / "book_conversion_temp"
            sentinel = legacy_dir / "keep.txt"
            input_html.write_text("<html><body>book</body></html>", encoding="utf-8")
            output_file.write_text("epub", encoding="utf-8")
            legacy_dir.mkdir()
            sentinel.write_text("keep", encoding="utf-8")
            captured = {}

            def fake_prepare(_input, working_dir, _lang):
                captured["working_dir"] = working_dir
                return str(input_html)

            with mock.patch.object(
                sys,
                "argv",
                [
                    "calibre_html_publish.py",
                    str(input_html),
                    "-o",
                    str(output_file),
                ],
            ), mock.patch.object(
                calibre_html_publish, "copy_images_if_needed", return_value=0
            ), mock.patch.object(
                calibre_html_publish,
                "prepare_html_for_conversion",
                side_effect=fake_prepare,
            ), mock.patch.object(
                calibre_html_publish,
                "convert_html_with_calibre",
                return_value=True,
            ):
                calibre_html_publish.main()

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertNotEqual(
                os.path.normcase(captured["working_dir"]),
                os.path.normcase(str(legacy_dir)),
            )
            self.assertFalse(Path(captured["working_dir"]).exists())


class ExtractHtmlMetadataTests(unittest.TestCase):
    def test_decodes_escaped_title_and_author(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html_file = Path(temp_dir) / "book.html"
            html_file.write_text(
                '<html><head><title>A &lt; B &amp; C</title>'
                '<meta name="author" content="A &quot;Writer&quot;">'
                '</head></html>',
                encoding="utf-8",
            )

            title, author = calibre_html_publish.extract_html_metadata(str(html_file))

            self.assertEqual(title, "A < B & C")
            self.assertEqual(author, 'A "Writer"')


class ConvertHtmlWithCalibreTests(unittest.TestCase):
    def test_builds_expected_epub_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_html = Path(temp_dir) / "input.html"
            output_file = Path(temp_dir) / "output.epub"
            input_html.write_text("<html><head><title>Book</title></head></html>", encoding="utf-8")

            def fake_run(cmd, capture_output, text, encoding, errors, timeout):
                self.assertEqual((encoding, errors), ("utf-8", "replace"))
                output_file.write_text("epub", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.object(
                calibre_html_publish, "find_calibre_convert", return_value="/usr/bin/ebook-convert"
            ), mock.patch.object(
                calibre_html_publish, "extract_html_metadata", return_value=("Book", "Author")
            ), mock.patch.object(
                calibre_html_publish.subprocess, "run", side_effect=fake_run
            ) as run_mock:
                ok = calibre_html_publish.convert_html_with_calibre(
                    str(input_html), str(output_file), "epub", timeout=12, lang="ja"
                )

            self.assertTrue(ok)
            cmd = run_mock.call_args.args[0]
            self.assertEqual(cmd[0], "/usr/bin/ebook-convert")
            self.assertEqual(cmd[1], str(input_html))
            self.assertEqual(cmd[2], str(output_file))
            self.assertIn("--title", cmd)
            self.assertIn("--authors", cmd)
            self.assertIn("--language", cmd)
            self.assertIn("ja", cmd)
            self.assertIn("--epub-version", cmd)
            self.assertIn("3", cmd)
            self.assertNotIn("--disable-font-rescaling", cmd)

    @unittest.skipUnless(
        "cover" in inspect.signature(calibre_html_publish.convert_html_with_calibre).parameters,
        "cover parameter unavailable",
    )
    def test_includes_cover_argument_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_html = Path(temp_dir) / "input.html"
            output_file = Path(temp_dir) / "output.epub"
            cover_file = Path(temp_dir) / "cover.jpg"
            input_html.write_text("<html><head><title>Book</title></head></html>", encoding="utf-8")
            cover_file.write_text("img", encoding="utf-8")

            def fake_run(cmd, capture_output, text, encoding, errors, timeout):
                self.assertEqual((encoding, errors), ("utf-8", "replace"))
                output_file.write_text("epub", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.object(
                calibre_html_publish, "find_calibre_convert", return_value="/usr/bin/ebook-convert"
            ), mock.patch.object(
                calibre_html_publish, "extract_html_metadata", return_value=("Book", "Author")
            ), mock.patch.object(
                calibre_html_publish.subprocess, "run", side_effect=fake_run
            ) as run_mock:
                ok = calibre_html_publish.convert_html_with_calibre(
                    str(input_html),
                    str(output_file),
                    "epub",
                    timeout=12,
                    lang="ja",
                    cover=str(cover_file),
                )

            self.assertTrue(ok)
            cmd = run_mock.call_args.args[0]
            self.assertIn("--cover", cmd)
            self.assertIn(str(cover_file), cmd)


if __name__ == "__main__":
    unittest.main()
