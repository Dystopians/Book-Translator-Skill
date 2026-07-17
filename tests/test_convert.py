import os
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402


class SafeHtmlzExtractionTests(unittest.TestCase):
    def test_extracts_normal_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "book.htmlz"
            extract_dir = Path(temp_dir) / "extracted"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("index.html", "<html>ok</html>")
                zf.writestr("images/cover.png", b"png")

            html_file, images_dir = convert.extract_htmlz(str(archive), str(extract_dir))

            self.assertEqual(Path(html_file).read_text(encoding="utf-8"), "<html>ok</html>")
            self.assertEqual(Path(images_dir).name, "images")
            self.assertEqual((Path(images_dir) / "cover.png").read_bytes(), b"png")

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "bad.htmlz"
            extract_dir = Path(temp_dir) / "extracted"
            escaped = Path(temp_dir) / "escaped.txt"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escaped.txt", "owned")

            html_file, images_dir = convert.extract_htmlz(str(archive), str(extract_dir))

            self.assertIsNone(html_file)
            self.assertIsNone(images_dir)
            self.assertFalse(escaped.exists())

    def test_rejects_symbolic_link_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "bad.htmlz"
            extract_dir = Path(temp_dir) / "extracted"
            link = zipfile.ZipInfo("index.html")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(link, "outside")

            html_file, images_dir = convert.extract_htmlz(str(archive), str(extract_dir))

            self.assertIsNone(html_file)
            self.assertIsNone(images_dir)

    def test_rejects_archive_over_total_size_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "large.htmlz"
            extract_dir = Path(temp_dir) / "extracted"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("index.html", "12345")

            with mock.patch.object(convert, "MAX_HTMLZ_UNCOMPRESSED_BYTES", 4):
                html_file, images_dir = convert.extract_htmlz(
                    str(archive), str(extract_dir)
                )

            self.assertIsNone(html_file)
            self.assertIsNone(images_dir)


class HtmlzWorkspaceTests(unittest.TestCase):
    def test_main_accepts_utf8_markdown_without_calibre(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "legal.md"
            temp_root = root / "work"
            payload = '# “Charge” — 费用 × 2\n\n[Section 4.3](#section-43)\n'
            source.write_text(payload, encoding="utf-8")

            with mock.patch.object(
                sys,
                "argv",
                [
                    "convert.py",
                    str(source),
                    "--temp-root",
                    str(temp_root),
                    "--chunk-size",
                    "6000",
                ],
            ), mock.patch.object(convert, "find_calibre_convert") as find_calibre:
                convert.main()

            work = temp_root / "legal_temp"
            self.assertEqual((work / "input.md").read_text(encoding="utf-8"), payload)
            self.assertEqual((work / "chunk0001.md").read_text(encoding="utf-8"), payload)
            self.assertIn(
                "conversion_method=direct_markdown",
                (work / "config.txt").read_text(encoding="utf-8"),
            )
            manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in manifest["chunks"]], ["chunk0001"])
            find_calibre.assert_not_called()

    def test_main_does_not_overwrite_source_adjacent_htmlz(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = Path(temp_dir) / "book.epub"
            adjacent_htmlz = Path(temp_dir) / "book.htmlz"
            extracted_html = Path(temp_dir) / "extracted.html"
            work_dir = Path(temp_dir) / "book_temp"
            input_file.write_bytes(b"epub")
            adjacent_htmlz.write_text("keep me", encoding="utf-8")
            extracted_html.write_text("<html></html>", encoding="utf-8")
            work_dir.mkdir()
            captured = {}

            def fake_convert(_input, htmlz_file, _calibre):
                captured["htmlz_file"] = htmlz_file
                return True

            with mock.patch.object(
                sys, "argv", ["convert.py", str(input_file)]
            ), mock.patch.object(
                convert, "find_calibre_convert", return_value="ebook-convert"
            ), mock.patch.object(
                convert,
                "source_fingerprint",
                return_value={"path": str(input_file), "size": 4, "sha256": "0" * 64},
            ), mock.patch.object(
                convert, "check_source_cache", return_value=(None, None)
            ), mock.patch.object(
                convert, "convert_to_htmlz", side_effect=fake_convert
            ), mock.patch.object(
                convert, "extract_htmlz", return_value=(str(extracted_html), None)
            ), mock.patch.object(
                convert, "extract_metadata_from_htmlz", return_value={}
            ), mock.patch.object(
                convert, "setup_temp_directory", return_value=str(work_dir)
            ), mock.patch.object(
                convert, "convert_html_to_markdown", return_value=True
            ), mock.patch.object(
                convert, "_do_split_and_manifest", return_value=1
            ), mock.patch.object(
                convert, "create_config_file"
            ), mock.patch.object(
                convert, "_write_source_fingerprint"
            ):
                convert.main()

            self.assertEqual(adjacent_htmlz.read_text(encoding="utf-8"), "keep me")
            self.assertNotEqual(
                Path(captured["htmlz_file"]).resolve(), adjacent_htmlz.resolve()
            )
            self.assertFalse(Path(captured["htmlz_file"]).exists())

    def test_main_rejects_non_positive_chunk_size_before_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = Path(temp_dir) / "book.epub"
            input_file.write_bytes(b"epub")
            with mock.patch.object(
                sys, "argv", ["convert.py", str(input_file), "--chunk-size", "0"]
            ), mock.patch.object(convert, "find_calibre_convert") as find_calibre:
                with self.assertRaises(SystemExit) as exc:
                    convert.main()

            self.assertNotEqual(exc.exception.code, 0)
            find_calibre.assert_not_called()


class CleanCalibreMarkersTests(unittest.TestCase):
    def test_removes_known_calibre_artifacts(self):
        content = "\n".join(
            [
                "## Heading {#calibre_link-12 .calibre3}",
                "[**Chapter One**]",
                "Paragraph text{.calibre5} (#calibre_link-2)",
                "::: {.calibre1}",
                "42",
                "broken.ct}",
                "Regular paragraph.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertIn("## Heading", cleaned)
        self.assertIn("**Chapter One**", cleaned)
        self.assertIn("Paragraph text", cleaned)
        self.assertIn("Regular paragraph.", cleaned)
        self.assertNotIn(".calibre", cleaned)
        self.assertIn("{#calibre_link-12}", cleaned)
        self.assertNotIn("(#calibre_link-2)", cleaned)
        self.assertNotIn(":::", cleaned)
        # 42 sits between ::: noise and broken.ct} noise, both calibre artifacts.
        # Context-aware cleaner still drops it — but only because of the neighbors.
        self.assertNotIn("\n42\n", f"\n{cleaned}\n")
        self.assertNotIn("broken.ct}", cleaned)

    def test_preserves_real_cross_reference_and_anchor(self):
        content = (
            "## Terms {#calibre_link-12 .calibre3}\n\n"
            "See [Section 4.3](#calibre_link-12).\n\n"
            "Orphaned conversion token (#calibre_link-99).\n"
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertIn("{#calibre_link-12}", cleaned)
        self.assertIn("[Section 4.3](#calibre_link-12)", cleaned)
        self.assertNotIn("(#calibre_link-99)", cleaned)

    def test_pandoc_writer_preserves_smart_punctuation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html = Path(temp_dir) / "input.html"
            output = Path(temp_dir) / "output.md"
            html.write_text("<p>“Charge” — ×</p>", encoding="utf-8")
            fake_pandoc = mock.Mock()

            def fake_convert_file(_source, target_format, *, outputfile, extra_args):
                self.assertEqual(target_format, "markdown-smart")
                self.assertEqual(extra_args, ["--wrap=none"])
                Path(outputfile).write_text("“Charge” — ×", encoding="utf-8")

            fake_pandoc.convert_file.side_effect = fake_convert_file
            with mock.patch.dict(sys.modules, {"pypandoc": fake_pandoc}):
                self.assertTrue(convert.convert_html_to_markdown(str(html), str(output)))

            self.assertEqual(output.read_text(encoding="utf-8"), "“Charge” — ×")

    def test_pandoc_preparation_preserves_calibre_internal_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html = Path(temp_dir) / "input.html"
            output = Path(temp_dir) / "output.md"
            html.write_text(
                '<div id="calibre_link-2"><p><a href="#calibre_link-2">Terms</a></p></div>',
                encoding="utf-8",
            )
            fake_pandoc = mock.Mock()
            prepared_path = None

            def fake_convert_file(source, _target_format, *, outputfile, extra_args):
                nonlocal prepared_path
                prepared_path = Path(source)
                prepared = prepared_path.read_text(encoding="utf-8")
                self.assertIn('<span id="calibre_link-2"></span>', prepared)
                self.assertNotIn('<div id="calibre_link-2">', prepared)
                Path(outputfile).write_text(
                    "[Terms](#calibre_link-2)\n\n[]{#calibre_link-2}\n",
                    encoding="utf-8",
                )

            fake_pandoc.convert_file.side_effect = fake_convert_file
            with mock.patch.dict(sys.modules, {"pypandoc": fake_pandoc}):
                self.assertTrue(convert.convert_html_to_markdown(str(html), str(output)))

            self.assertIn("[]{#calibre_link-2}", output.read_text(encoding="utf-8"))
            self.assertIsNotNone(prepared_path)
            self.assertFalse(prepared_path.exists())

    def test_conversion_fails_closed_if_internal_anchor_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html = Path(temp_dir) / "input.html"
            output = Path(temp_dir) / "output.md"
            html.write_text('<p><a href="#calibre_link-2">Terms</a></p>', encoding="utf-8")
            fake_pandoc = mock.Mock()
            fake_pandoc.convert_file.side_effect = lambda *args, **kwargs: Path(
                kwargs["outputfile"]
            ).write_text("[Terms](#calibre_link-2)\n", encoding="utf-8")

            with mock.patch.dict(sys.modules, {"pypandoc": fake_pandoc}):
                self.assertFalse(convert.convert_html_to_markdown(str(html), str(output)))

    def test_calibre_subprocess_output_uses_utf8_with_replacement(self):
        result = mock.Mock(returncode=1, stderr="diagnostic")
        with mock.patch.object(convert.subprocess, "run", return_value=result) as run:
            self.assertFalse(convert.convert_to_htmlz("book.epub", "book.htmlz", "ebook-convert"))

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_preserves_year_in_paragraph(self):
        content = "\n".join(
            [
                "He was born in",
                "1984",
                "and died later.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertIn("1984", cleaned)
        self.assertIn("He was born in", cleaned)
        self.assertIn("and died later.", cleaned)

    def test_preserves_chapter_number_after_heading(self):
        content = "\n".join(
            [
                "## Chapter",
                "",
                "3",
                "",
                "Introduction text follows.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertIn("\n3\n", f"\n{cleaned}\n")
        self.assertIn("## Chapter", cleaned)
        self.assertIn("Introduction text follows.", cleaned)

    def test_drops_digit_line_inside_calibre_fence(self):
        content = "\n".join(
            [
                "Some real paragraph.",
                "::: {.calibre1}",
                "42",
                ":::",
                "More real paragraph.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertNotIn("42", cleaned)
        self.assertNotIn(":::", cleaned)
        self.assertIn("Some real paragraph.", cleaned)
        self.assertIn("More real paragraph.", cleaned)

    def test_drops_digit_line_adjacent_to_ct_marker(self):
        content = "\n".join(
            [
                "Real paragraph above.",
                "7",
                "broken.ct}",
                "Real paragraph below.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertNotIn("\n7\n", f"\n{cleaned}\n")
        self.assertNotIn("broken.ct}", cleaned)

    def test_drops_sequential_page_numbers(self):
        # Six paragraphs separated by sequential page-number footers — clear
        # monotonic spine should be detected and dropped.
        content = "\n".join(
            [
                "Para one.",
                "",
                "1",
                "",
                "Para two.",
                "",
                "2",
                "",
                "Para three.",
                "",
                "3",
                "",
                "Para four.",
                "",
                "4",
                "",
                "Para five.",
                "",
                "5",
                "",
                "Para six.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        for n in ("1", "2", "3", "4", "5"):
            self.assertNotIn(f"\n{n}\n", f"\n{cleaned}\n")
        for p in ("Para one.", "Para two.", "Para three.", "Para four.", "Para five.", "Para six."):
            self.assertIn(p, cleaned)

    def test_preserves_year_among_page_numbers(self):
        # Same page-number spine plus a year (1984) sitting between two page
        # numbers — LNDS picks 1..5 and skips 1984, which stays as content.
        content = "\n".join(
            [
                "Para one.",
                "",
                "1",
                "",
                "Para two.",
                "",
                "2",
                "",
                "He was born in 1984.",
                "Standalone year:",
                "1984",
                "and continued.",
                "",
                "3",
                "",
                "Para three.",
                "",
                "4",
                "",
                "Para four.",
                "",
                "5",
                "",
                "Para five.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        self.assertIn("\n1984\n", f"\n{cleaned}\n")
        for n in ("1", "2", "3", "4", "5"):
            self.assertNotIn(f"\n{n}\n", f"\n{cleaned}\n")

    def test_few_digits_not_treated_as_page_numbers(self):
        # Only three standalone digits — below the LNDS minimum length, so all
        # are preserved (assuming no calibre-noise neighbors).
        content = "\n".join(
            [
                "Intro paragraph.",
                "",
                "1",
                "",
                "Body paragraph.",
                "",
                "2",
                "",
                "More body.",
                "",
                "3",
                "",
                "Closing paragraph.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        for n in ("1", "2", "3"):
            self.assertIn(f"\n{n}\n", f"\n{cleaned}\n")

    def test_non_monotonic_digits_preserved(self):
        # Five standalone digits but no monotonic spine — LNDS coverage too low
        # to trigger; everything preserved.
        content = "\n".join(
            [
                "Intro.",
                "",
                "1984",
                "",
                "Para A.",
                "",
                "42",
                "",
                "Para B.",
                "",
                "7",
                "",
                "Para C.",
                "",
                "1066",
                "",
                "Para D.",
                "",
                "3",
                "",
                "Closing.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content)

        for n in ("1984", "42", "7", "1066", "3"):
            self.assertIn(f"\n{n}\n", f"\n{cleaned}\n")

    def test_strip_page_numbers_flag_restores_legacy(self):
        content = "\n".join(
            [
                "He was born in",
                "1984",
                "and died later.",
                "",
                "## Chapter",
                "",
                "3",
                "",
                "Introduction text follows.",
            ]
        )

        cleaned = convert.clean_calibre_markers(content, strip_page_numbers=True)

        self.assertNotIn("1984", cleaned)
        self.assertNotIn("\n3\n", f"\n{cleaned}\n")
        self.assertIn("He was born in", cleaned)
        self.assertIn("Introduction text follows.", cleaned)


class TempRootTests(unittest.TestCase):
    def test_build_temp_dir_preserves_cwd_local_default(self):
        self.assertEqual(convert.build_temp_dir("/books/Alice.epub"), "Alice_temp")

    def test_build_temp_dir_uses_explicit_root(self):
        self.assertEqual(
            convert.build_temp_dir("/books/Alice.epub", "/tmp/work"),
            os.path.join("/tmp/work", "Alice_temp"),
        )

    def test_setup_temp_directory_uses_explicit_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "work"
            html_file = Path(temp_dir) / "input.html"
            images_dir = Path(temp_dir) / "images"
            image_file = images_dir / "cover.jpg"
            html_file.write_text("<html></html>", encoding="utf-8")
            images_dir.mkdir()
            image_file.write_text("image", encoding="utf-8")

            created = convert.setup_temp_directory(
                "/books/Alice.epub",
                str(html_file),
                str(images_dir),
                temp_root=str(root),
            )

            self.assertEqual(created, str(root / "Alice_temp"))
            self.assertTrue((root / "Alice_temp" / "input.html").exists())
            self.assertTrue((root / "Alice_temp" / "images" / "cover.jpg").exists())


class StripPageNumbersCacheConflictTests(unittest.TestCase):
    def test_no_blockers_when_flag_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_md = os.path.join(tmp, "input.md")
            with open(input_md, "w", encoding="utf-8") as f:
                f.write("placeholder")
            with open(os.path.join(tmp, "chunk0001.md"), "w", encoding="utf-8") as f:
                f.write("placeholder")

            blockers = convert._check_strip_page_numbers_cache_conflict(
                strip_flag=False, temp_dir=tmp, input_md=input_md
            )

            self.assertEqual(blockers, [])

    def test_no_blockers_when_temp_dir_missing(self):
        missing_dir = os.path.join(tempfile.gettempdir(), "definitely-not-here-xyz-123")
        # Make extra sure it really doesn't exist.
        self.assertFalse(os.path.isdir(missing_dir))
        input_md = os.path.join(missing_dir, "input.md")

        blockers = convert._check_strip_page_numbers_cache_conflict(
            strip_flag=True, temp_dir=missing_dir, input_md=input_md
        )

        self.assertEqual(blockers, [])

    def test_aborts_when_input_md_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_md = os.path.join(tmp, "input.md")
            with open(input_md, "w", encoding="utf-8") as f:
                f.write("cached markdown")

            blockers = convert._check_strip_page_numbers_cache_conflict(
                strip_flag=True, temp_dir=tmp, input_md=input_md
            )

            self.assertEqual(len(blockers), 1)
            self.assertIn("input.md", blockers[0])

    def test_aborts_when_chunks_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_md = os.path.join(tmp, "input.md")  # absent
            for i in range(1, 4):
                with open(os.path.join(tmp, f"chunk{i:04d}.md"), "w", encoding="utf-8") as f:
                    f.write("chunk")
            # output_chunk*.md files must not be counted as source chunks.
            with open(os.path.join(tmp, "output_chunk0001.md"), "w", encoding="utf-8") as f:
                f.write("translated")

            blockers = convert._check_strip_page_numbers_cache_conflict(
                strip_flag=True, temp_dir=tmp, input_md=input_md
            )

            self.assertEqual(len(blockers), 1)
            self.assertIn("3 chunk file(s)", blockers[0])


class SourceFingerprintCacheTests(unittest.TestCase):
    def _write(self, path, content):
        Path(path).write_text(content, encoding="utf-8")

    def test_no_conflict_for_fresh_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            self._write(source, "source bytes")

            status, message = convert.check_source_cache(
                str(Path(tmp) / "book_temp"),
                convert.source_fingerprint(str(source)),
            )

        self.assertIsNone(status)
        self.assertIsNone(message)

    def test_adopts_cache_that_predates_fingerprinting(self):
        # Temp dirs created before this feature have no fingerprint file.
        # They must stay resumable (trust-on-first-use), with a warning.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            temp_dir = Path(tmp) / "book_temp"
            temp_dir.mkdir()
            self._write(source, "source bytes")
            self._write(temp_dir / "input.html", "<html></html>")

            status, message = convert.check_source_cache(
                str(temp_dir), convert.source_fingerprint(str(source))
            )

        self.assertEqual(status, "adopt")
        self.assertIn(convert.SOURCE_FINGERPRINT_FILE, message)

    def test_mismatch_when_source_bytes_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            temp_dir = Path(tmp) / "book_temp"
            temp_dir.mkdir()
            self._write(source, "old source bytes")
            convert._write_source_fingerprint(
                str(temp_dir), convert.source_fingerprint(str(source))
            )
            self._write(temp_dir / "input.html", "<html></html>")

            self._write(source, "new source bytes!!")
            status, message = convert.check_source_cache(
                str(temp_dir), convert.source_fingerprint(str(source))
            )

        self.assertEqual(status, "mismatch")
        self.assertIn("different source bytes", message)

    def test_no_conflict_when_source_bytes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            temp_dir = Path(tmp) / "book_temp"
            temp_dir.mkdir()
            self._write(source, "source bytes")
            convert._write_source_fingerprint(
                str(temp_dir), convert.source_fingerprint(str(source))
            )
            self._write(temp_dir / "input.html", "<html></html>")

            status, message = convert.check_source_cache(
                str(temp_dir), convert.source_fingerprint(str(source))
            )

        self.assertIsNone(status)
        self.assertIsNone(message)

    def test_moving_source_file_does_not_invalidate_cache(self):
        # Only content identity matters — renaming/moving the book is fine.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            temp_dir = Path(tmp) / "book_temp"
            temp_dir.mkdir()
            self._write(source, "source bytes")
            convert._write_source_fingerprint(
                str(temp_dir), convert.source_fingerprint(str(source))
            )
            self._write(temp_dir / "input.html", "<html></html>")

            moved = Path(tmp) / "renamed-book.epub"
            source.rename(moved)
            status, _ = convert.check_source_cache(
                str(temp_dir), convert.source_fingerprint(str(moved))
            )

        self.assertIsNone(status)

    def test_corrupt_fingerprint_file_is_adopted_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            temp_dir = Path(tmp) / "book_temp"
            temp_dir.mkdir()
            self._write(source, "source bytes")
            self._write(temp_dir / "input.html", "<html></html>")
            self._write(temp_dir / convert.SOURCE_FINGERPRINT_FILE, "{not json")

            status, _ = convert.check_source_cache(
                str(temp_dir), convert.source_fingerprint(str(source))
            )

        self.assertEqual(status, "adopt")


if __name__ == "__main__":
    unittest.main()
