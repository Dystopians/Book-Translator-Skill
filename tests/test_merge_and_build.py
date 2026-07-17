import contextlib
import inspect
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import merge_and_build  # noqa: E402


class GenerateFormatTests(unittest.TestCase):
    def _write_file(self, path, content="data"):
        Path(path).write_text(content, encoding="utf-8")

    def _set_mtime(self, path, timestamp):
        os.utime(path, (timestamp, timestamp))

    def test_skips_when_output_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html_file = os.path.join(temp_dir, "book_doc.html")
            output_file = os.path.join(temp_dir, "book.epub")
            self._write_file(html_file, "<html></html>")
            self._write_file(output_file, "epub")
            self._set_mtime(html_file, 100)
            self._set_mtime(output_file, 200)

            with mock.patch.object(merge_and_build.subprocess, "run") as run_mock:
                result = merge_and_build.generate_format(
                    html_file, temp_dir, ".epub", "zh-CN"
                )

            self.assertEqual(result, output_file)
            run_mock.assert_not_called()

    def test_rebuilds_when_image_assets_are_newer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html_file = os.path.join(temp_dir, "book_doc.html")
            output_file = os.path.join(temp_dir, "book.epub")
            images_dir = os.path.join(temp_dir, "images")
            image_file = os.path.join(images_dir, "cover.jpg")

            os.makedirs(images_dir, exist_ok=True)
            self._write_file(html_file, "<html></html>")
            self._write_file(output_file, "epub")
            self._write_file(image_file, "image")

            self._set_mtime(html_file, 100)
            self._set_mtime(output_file, 200)
            self._set_mtime(image_file, 300)

            with mock.patch.object(
                merge_and_build.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="", stderr=""),
            ) as run_mock:
                result = merge_and_build.generate_format(
                    html_file, temp_dir, ".epub", "zh-CN"
                )

            self.assertEqual(result, output_file)
            run_mock.assert_called_once()
            cmd = run_mock.call_args.args[0]
            self.assertEqual(cmd[0], sys.executable)
            self.assertEqual(cmd[2], html_file)
            self.assertEqual(cmd[4], output_file)

    @unittest.skipUnless(
        "cover" in inspect.signature(merge_and_build.generate_format).parameters,
        "cover parameter unavailable",
    )
    def test_rebuilds_epub_when_cover_is_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html_file = os.path.join(temp_dir, "book_doc.html")
            output_file = os.path.join(temp_dir, "book.epub")
            cover_file = os.path.join(temp_dir, "cover.jpg")

            self._write_file(html_file, "<html></html>")
            self._write_file(output_file, "epub")
            self._write_file(cover_file, "image")

            self._set_mtime(html_file, 100)
            self._set_mtime(output_file, 200)
            self._set_mtime(cover_file, 300)

            with mock.patch.object(
                merge_and_build.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="", stderr=""),
            ) as run_mock:
                result = merge_and_build.generate_format(
                    html_file, temp_dir, ".epub", "zh-CN", cover=cover_file
                )

            self.assertEqual(result, output_file)
            run_mock.assert_called_once()
            cmd = run_mock.call_args.args[0]
            self.assertIn("--cover", cmd)
            self.assertIn(cover_file, cmd)


class MissingCoverPathTests(unittest.TestCase):
    @unittest.skipUnless(
        "cover" in inspect.signature(merge_and_build.generate_format).parameters,
        "cover parameter unavailable",
    )
    def test_main_rejects_missing_cover_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_cover = os.path.join(temp_dir, "missing-cover.jpg")

            with mock.patch.object(
                merge_and_build, "load_config", return_value={}
            ), mock.patch.object(
                merge_and_build, "get_lang_config", return_value={"lang_attr": "zh-CN"}
            ), mock.patch.object(
                merge_and_build, "merge_markdown_files", return_value=True
            ), mock.patch.object(
                merge_and_build, "convert_md_to_html", return_value=True
            ), mock.patch.object(
                merge_and_build, "add_toc", return_value=True
            ), mock.patch.object(
                merge_and_build, "generate_formats"
            ) as generate_formats_mock, mock.patch.object(
                sys, "argv", ["merge_and_build.py", "--temp-dir", temp_dir, "--cover", missing_cover]
            ):
                with self.assertRaises(SystemExit) as exc:
                    merge_and_build.main()

            self.assertNotEqual(exc.exception.code, 0)
            generate_formats_mock.assert_not_called()


class ExportAliasTests(unittest.TestCase):
    def _write(self, path, content="x"):
        Path(path).write_text(content, encoding="utf-8")

    def test_export_named_aliases_copies_canonical_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for name in ["book.html", "book_doc.html", "book.docx", "book.epub", "book.pdf"]:
                self._write(Path(temp_dir) / name, name)

            copied = merge_and_build.export_named_aliases(temp_dir, "Translated Book")

            self.assertEqual(
                set(copied),
                {
                    "Translated Book.html",
                    "Translated Book_doc.html",
                    "Translated Book.docx",
                    "Translated Book.epub",
                    "Translated Book.pdf",
                },
            )
            self.assertEqual(
                (Path(temp_dir) / "Translated Book.epub").read_text(encoding="utf-8"),
                "book.epub",
            )
            self.assertTrue((Path(temp_dir) / "book.epub").exists())

    def test_export_name_rejects_paths(self):
        with self.assertRaises(ValueError):
            merge_and_build.export_named_aliases("/tmp", "../bad")

    def test_export_name_rejects_nonportable_stems(self):
        for stem in ["CON", "bad:name", "..", "trailing.", "BOOK"]:
            with self.subTest(stem=stem):
                with self.assertRaises(ValueError):
                    merge_and_build._validate_export_name(stem)

    def test_export_name_cannot_overwrite_book_doc_html(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            web = Path(temp_dir) / "book.html"
            ebook = Path(temp_dir) / "book_doc.html"
            self._write(web, "web")
            self._write(ebook, "ebook")

            with self.assertRaises(ValueError):
                merge_and_build.export_named_aliases(temp_dir, "book_doc")

            self.assertEqual(web.read_text(encoding="utf-8"), "web")
            self.assertEqual(ebook.read_text(encoding="utf-8"), "ebook")


class SafeCleanupTests(unittest.TestCase):
    def test_transient_cleanup_preserves_resumable_translation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            for name in (
                "chunk0001.md", "output_chunk0001.md",
                "translation_state.sqlite3", "run_state.json", "glossary.json",
                "input.md", "input.html", "output.html",
            ):
                (temp_dir / name).write_text("data", encoding="utf-8")

            merge_and_build.cleanup_intermediate_files(str(temp_dir))

            self.assertTrue((temp_dir / "chunk0001.md").exists())
            self.assertTrue((temp_dir / "output_chunk0001.md").exists())
            self.assertTrue((temp_dir / "translation_state.sqlite3").exists())
            self.assertTrue((temp_dir / "run_state.json").exists())
            self.assertFalse((temp_dir / "input.md").exists())
            self.assertFalse((temp_dir / "input.html").exists())
            self.assertFalse((temp_dir / "output.html").exists())

    def test_aggressive_cleanup_removes_resumable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            for name in (
                "chunk0001.md", "output_chunk0001.md",
                "analysis_chunk0001.json", "review_chunk0001.json",
                "output_chunk0001.meta.json", "translation_state.sqlite3",
                "run_state.json", "glossary.json", "decisions-round1.json",
                "external-source-reference.json",
            ):
                (temp_dir / name).write_text("data", encoding="utf-8")

            merge_and_build.cleanup_intermediate_files(str(temp_dir), aggressive=True)
            self.assertEqual(list(temp_dir.iterdir()), [])


class DraftArtifactTests(unittest.TestCase):
    def test_draft_publish_uses_non_final_names(self):
        with tempfile.TemporaryDirectory() as build_tmp, tempfile.TemporaryDirectory() as out_tmp:
            build_dir = Path(build_tmp)
            out_dir = Path(out_tmp)
            (build_dir / "book.html").write_text("draft", encoding="utf-8")
            (build_dir / "book.epub").write_bytes(b"draft epub")
            (out_dir / "book.html").write_text("existing final", encoding="utf-8")

            copied = merge_and_build._publish_draft_artifacts(str(build_dir), str(out_dir))

            self.assertIn("book.draft.html", copied)
            self.assertIn("book.draft.epub", copied)
            self.assertEqual(
                (out_dir / "book.html").read_text(encoding="utf-8"),
                "existing final",
            )
            self.assertEqual(
                (out_dir / "book.draft.html").read_text(encoding="utf-8"),
                "draft",
            )

    def test_enhanced_state_cannot_use_legacy_gate_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "translation_state.sqlite3").write_bytes(b"state")
            with self.assertRaisesRegex(ValueError, "draft or final"):
                merge_and_build._enforce_quality_mode_state(str(root), "legacy")
            merge_and_build._enforce_quality_mode_state(str(root), "draft")
            merge_and_build._enforce_quality_mode_state(str(root), "final")


class HtmlSanitizationTests(unittest.TestCase):
    def test_removes_active_content_and_keeps_book_markup(self):
        fragment = (
            '<h1 onclick="steal()">Title</h1>'
            '<script>alert(1)</script>'
            '<a href="java&#x0A;script:alert(2)">bad</a>'
            '<img src="images/cover.png" onerror="steal()" alt="Cover">'
            '<p class="lead">Safe &amp; sound</p>'
        )

        safe, removed = merge_and_build.sanitize_html_fragment(fragment)

        self.assertGreaterEqual(removed, 4)
        self.assertNotIn("<script", safe)
        self.assertNotIn("alert(1)", safe)
        self.assertNotIn("onclick", safe)
        self.assertNotIn("onerror", safe)
        self.assertNotIn("javascript:", safe.lower())
        self.assertIn("<h1>Title</h1>", safe)
        self.assertIn('<img src="images/cover.png" alt="Cover">', safe)
        self.assertIn('<p class="lead">Safe &amp; sound</p>', safe)

    def test_blocked_script_cannot_be_escaped_by_void_end_tag(self):
        fragment = (
            '<script><img src="x"></img><p>hidden</p></script>'
            '<p>visible</p>'
        )

        safe, _ = merge_and_build.sanitize_html_fragment(fragment)

        self.assertNotIn("hidden", safe)
        self.assertNotIn("script", safe)
        self.assertEqual(safe, "<p>visible</p>")

    def test_template_escapes_title_author_and_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template = Path(temp_dir) / "template.html"
            output = Path(temp_dir) / "output.html"
            template.write_text(
                '<html lang="$lang$"><head><title>$title$</title></head>'
                '<body>$body$</body></html>',
                encoding="utf-8",
            )
            lang_cfg = {
                "lang_attr": 'en" onload="bad',
                "font_family": "serif",
                "toc_label": "Contents",
            }

            ok = merge_and_build.apply_template_to_html(
                "<p>Body: $title$ / $lang$</p>",
                str(template),
                str(output),
                '</title><script>alert(1)</script>',
                lang_cfg,
                'A "quoted" author',
            )

            self.assertTrue(ok)
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("</title><script>", rendered)
            self.assertIn("&lt;/title&gt;&lt;script&gt;", rendered)
            self.assertIn('lang="en&quot; onload=&quot;bad"', rendered)
            self.assertIn(
                'content="A &quot;quoted&quot; author"',
                rendered,
            )
            self.assertIn("<p>Body: $title$ / $lang$</p>", rendered)

    def test_toc_text_is_html_escaped(self):
        toc = merge_and_build.generate_simple_toc_html(
            [{"level": 1, "text": '<img src=x onerror="bad">', "id": 'a"b'}]
        )

        self.assertIn("&lt;img", toc)
        self.assertIn('href="#a&quot;b"', toc)
        self.assertNotIn("<img", toc)

    def test_convert_pipeline_sanitizes_book_supplied_active_html(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_md = Path(temp_dir) / "output.md"
            output_md.write_text(
                '# Safe heading\n\n'
                '<script>alert("book supplied")</script>\n\n'
                '</body><p>content after injected body close</p>\n\n'
                '<img src="images/cover.png" onerror="steal()">\n',
                encoding="utf-8",
            )

            with mock.patch.object(
                merge_and_build, "check_pandoc_available", return_value=False
            ), mock.patch.object(
                merge_and_build,
                "convert_with_python_markdown",
                return_value=False,
            ):
                ok = merge_and_build.convert_md_to_html(
                    temp_dir,
                    "Safe title",
                    merge_and_build.get_lang_config("en"),
                    "Safe author",
                )

            self.assertTrue(ok)
            for filename in ("book.html", "book_doc.html"):
                rendered = (Path(temp_dir) / filename).read_text(encoding="utf-8")
                self.assertNotIn('alert("book supplied")', rendered)
                self.assertNotIn("onerror", rendered)
                self.assertIn("images/cover.png", rendered)
                self.assertIn("content after injected body close", rendered)


class MergeBlankOutputTests(unittest.TestCase):
    """A whitespace-only output chunk must abort the merge instead of being
    silently dropped from the final book."""

    def _write(self, path, content):
        Path(path).write_text(content, encoding="utf-8")

    def _workspace(self, tmp):
        from manifest import create_manifest

        temp_dir = Path(tmp)
        self._write(temp_dir / "input.md", "One.\n\nTwo.\n")
        self._write(temp_dir / "chunk0001.md", "One.\n")
        self._write(temp_dir / "chunk0002.md", "Two.\n")
        self._write(temp_dir / "output_chunk0001.md", "一。\n")
        self._write(temp_dir / "output_chunk0002.md", "二。\n")
        create_manifest(
            str(temp_dir),
            ["chunk0001.md", "chunk0002.md"],
            str(temp_dir / "input.md"),
        )
        return temp_dir

    def _merge(self, temp_dir):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = merge_and_build.merge_markdown_files(str(temp_dir))
        return ok, buf.getvalue()

    def test_manifest_merge_fails_on_blank_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = self._workspace(tmp)
            self._write(temp_dir / "output_chunk0002.md", "\n   \n")
            ok, out = self._merge(temp_dir)

            self.assertFalse(ok)
            self.assertFalse((temp_dir / "output.md").exists())
            self.assertIn("Blank output", out)

    def test_legacy_merge_fails_on_blank_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = self._workspace(tmp)
            (temp_dir / "manifest.json").unlink()
            self._write(temp_dir / "output_chunk0002.md", "\n   \n")
            ok, out = self._merge(temp_dir)

            self.assertFalse(ok)
            self.assertFalse((temp_dir / "output.md").exists())
            self.assertIn("Blank output", out)

    def test_merge_succeeds_with_substantive_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = self._workspace(tmp)
            ok, _ = self._merge(temp_dir)

            self.assertTrue(ok)
            merged = (temp_dir / "output.md").read_text(encoding="utf-8")
            self.assertIn("一。", merged)
            self.assertIn("二。", merged)


class ImageValidationTests(unittest.TestCase):
    """Validates _validate_chunk_images, _check_generated_html_sanity, and the
    basic-regex alt-escape fix. Together these guard against subagent-produced
    malformed <img> tags surviving into the final HTML."""

    def _write(self, path, content):
        Path(path).write_text(content, encoding="utf-8")

    def _run_validator(self, temp_dir):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = merge_and_build._validate_chunk_images(temp_dir)
        return ok, buf.getvalue()

    def _run_html_sanity(self, html_path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = merge_and_build._check_generated_html_sanity(html_path)
        return ok, buf.getvalue()

    def test_passes_for_clean_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                'Hello <img src="images/a.png" alt="A"> and ![fig](images/b.png).',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                'Translated <img src="images/a.png" alt="译"> and ![图](images/b.png).',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_fails_on_unescaped_double_quote_in_alt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                'Bottle <img src="images/a.png" alt="Drink Me bottle">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '瓶子 <img src="images/a.png" alt="标着"喝我"的瓶子">',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("output_chunk0001.md", out)
            self.assertIn("malformed <img>", out)

    def test_passes_for_curly_quote_alt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                '<img src="images/a.png" alt="Drink Me bottle">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '<img src="images/a.png" alt="标着“喝我”的瓶子">',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_passes_for_html_entity_alt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                '<img src="images/a.png" alt="Drink Me">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '<img src="images/a.png" alt="标着&quot;喝我&quot;的瓶子">',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_fails_on_missing_image_src(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                '<img src="images/a.png" alt="A"> some text <img src="images/b.png" alt="B">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '<img src="images/a.png" alt="译"> 译文',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("images/b.png", out)
            self.assertIn("missing <img src>", out)

    def test_fails_on_changed_src_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                '<img src="images/000034.png" alt="orig">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '<img src="images/000035.png" alt="译">',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("images/000034.png", out)
            self.assertIn("images/000035.png", out)

    def test_fails_on_repeated_image_dropped_to_one(self):
        # Counter-based regression: source has same src twice, translated has it once.
        # A set-based comparison would miss this.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                '<img src="images/a.png" alt="first"> middle <img src="images/a.png" alt="second">',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '<img src="images/a.png" alt="译一"> 中间译文',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("images/a.png", out)

    def test_fails_when_output_md_uptodate_but_chunks_bad(self):
        # Regression test for cache-bypass: even if output.md exists and is "up to date",
        # bad chunks must still cause merge_markdown_files() to fail, and the stale
        # output.md must be removed.
        with tempfile.TemporaryDirectory() as temp_dir:
            chunk = Path(temp_dir) / "chunk0001.md"
            out_chunk = Path(temp_dir) / "output_chunk0001.md"
            output_md = Path(temp_dir) / "output.md"

            self._write(chunk, '<img src="images/a.png" alt="A">')
            self._write(out_chunk, '<img src="images/a.png" alt="标"喝我"">')
            self._write(output_md, "stale merged content")

            os.utime(chunk, (100, 100))
            os.utime(out_chunk, (150, 150))
            os.utime(output_md, (200, 200))  # newer than chunks

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = merge_and_build.merge_markdown_files(temp_dir)
            out = buf.getvalue()

            self.assertFalse(result)
            self.assertFalse(output_md.exists(), msg=f"stale output.md should be deleted; stdout=\n{out}")
            self.assertIn("output_chunk0001.md", out)

    def test_passes_when_code_block_preserves_broken_img_example(self):
        # Regression: a tech book may legitimately ship a fenced code block that
        # demonstrates a deliberately-broken <img> tag. Both source and output
        # carry the same example, so the per-chunk delta is empty — must pass.
        broken_example = (
            "Here is a buggy tag to demonstrate the parser:\n\n"
            "```html\n"
            '<img src="x.png" alt="he said "hi" loudly">\n'
            "```\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(Path(temp_dir) / "chunk0001.md", broken_example)
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                broken_example.replace("Here is a buggy tag to demonstrate the parser:",
                                       "下面这个有 bug 的标签是用来演示解析器的："),
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_fails_when_output_introduces_new_broken_img(self):
        # If source had one broken example and output adds a *second* broken tag
        # (not present in source), the new bad attr shows up in the delta and
        # we must flag it — even when there's a baseline of broken attrs.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                "Demo block:\n\n"
                "```html\n"
                '<img src="x.png" alt="he said "hi" loudly">\n'
                "```\n\n"
                'And a real image: <img src="images/a.png" alt="A">\n',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                "演示代码块：\n\n"
                "```html\n"
                '<img src="x.png" alt="he said "hi" loudly">\n'
                "```\n\n"
                # New corruption: subagent broke the real image alt with unescaped quote
                '真实图片：<img src="images/a.png" alt="标着"喝我"">\n',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("introduced malformed <img>", out)
            self.assertIn("output_chunk0001.md", out)

    def test_fails_when_real_image_replaced_with_escaped_markdown(self):
        # Regression: a regex that didn't honor `\!` would count `\![](path)` as
        # an image, masking real loss when the subagent escaped it accidentally.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                "See ![Fig 1](images/a.png) for details.",
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                "见 \\![图 1](images/a.png) 了解详情。",
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("missing ![](path)", out)
            self.assertIn("images/a.png", out)

    def test_passes_when_both_chunks_have_escaped_markdown_image(self):
        # Symmetric case: both chunks intentionally use `\![...]` as literal text.
        # Neither counts as a real image; counts match.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                "Use \\![alt](path) syntax to write the example as text.",
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                "用 \\![alt](path) 语法把示例写成文本。",
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_fails_when_markdown_image_missing_closing_paren(self):
        # Regression: the regex must require the closing `)` — a fragment like
        # `![图](images/a.png` does NOT render as an image, so counting it as a
        # preserved reference would mask real image loss.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                "See ![Fig 1](images/a.png) for details.",
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                "见 ![图 1](images/a.png 了解详情。",  # missing closing )
            )
            ok, out = self._run_validator(temp_dir)
            self.assertFalse(ok)
            self.assertIn("missing ![](path)", out)
            self.assertIn("images/a.png", out)

    def test_passes_for_markdown_image_with_title(self):
        # Forward-compatibility: standard `![alt](url "title")` syntax must keep
        # parsing as a single image when both chunks preserve the title.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                'Look at ![Fig 1](images/a.png "Diagram of the system") below.',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '看下方 ![图 1](images/a.png "系统示意图")。',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_passes_with_gt_in_quoted_alt(self):
        # Regression: a regex like <img\b[^>]*> would truncate at the first `>`
        # inside a quoted attribute value, producing a false-positive malformed
        # report for legitimate math/comparison content. HTMLParser handles this.
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                Path(temp_dir) / "chunk0001.md",
                'Compare <img src="images/a.png" alt="x > y and a < b"> here.',
            )
            self._write(
                Path(temp_dir) / "output_chunk0001.md",
                '比较 <img src="images/a.png" alt="x > y 且 a < b"> 这里。',
            )
            ok, out = self._run_validator(temp_dir)
            self.assertTrue(ok, msg=out)

    def test_html_canary_passes_when_prose_mentions_img_tag(self):
        # Regression: a book that legitimately discusses HTML will render `<img>`
        # mentions as `&lt;img&gt;` in prose or inside <pre><code>. That is not
        # corruption and must not block the build.
        with tempfile.TemporaryDirectory() as temp_dir:
            html_file = Path(temp_dir) / "book.html"
            self._write(
                html_file,
                "<html><body>"
                "<p>The &lt;img&gt; tag is used for inline images.</p>"
                "<pre><code>&lt;img src=&quot;example.png&quot;&gt;</code></pre>"
                "<p>Here is a real image: <img src=\"images/real.png\" alt=\"real\"></p>"
                "</body></html>",
            )
            ok, out = self._run_html_sanity(str(html_file))
            self.assertTrue(ok, msg=out)

    def test_basic_regex_escapes_alt_with_quote(self):
        # The basic-regex fallback used to inline alt verbatim, so a literal " in
        # markdown alt text would produce malformed raw <img>. Now it html.escapes.
        with tempfile.TemporaryDirectory() as temp_dir:
            md_file = Path(temp_dir) / "in.md"
            html_file = Path(temp_dir) / "out.html"
            self._write(md_file, '![Title with "quote" inside](images/x.png)\n')

            ok = merge_and_build.convert_with_basic_regex(str(md_file), str(html_file), "t")
            self.assertTrue(ok)

            html_text = html_file.read_text(encoding="utf-8")
            self.assertIn("&quot;", html_text)
            self.assertNotIn('alt="Title with "quote"', html_text)

            sanity_ok, sanity_out = self._run_html_sanity(str(html_file))
            self.assertTrue(sanity_ok, msg=sanity_out)


if __name__ == "__main__":
    unittest.main()
