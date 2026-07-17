import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import knowledge_store  # noqa: E402
import manifest  # noqa: E402
import quality_gate  # noqa: E402
import run_state  # noqa: E402


class EvidencePipelineIntegrationTests(unittest.TestCase):
    SOURCE = "A stable claim appears here.\n\nThe glossary remains empty.\n"
    TARGET = "这里出现了一个稳定的主张。\n\n术语表仍为空。\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "input.md").write_text(self.SOURCE, encoding="utf-8")
        (self.root / "chunk0001.md").write_text(self.SOURCE, encoding="utf-8")
        (self.root / "output_chunk0001.md").write_text(
            self.TARGET, encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            manifest.create_manifest(
                str(self.root), ["chunk0001.md"], str(self.root / "input.md")
            )
        knowledge_store.initialize_database(self.root, profile="general")
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                knowledge_store._set_metadata(connection, "target_lang", "zh")
                knowledge_store.refresh_chunk_dependencies(connection)
        finally:
            connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def _segments(self):
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT segment_id, ordinal FROM segments "
                    "WHERE chunk_id='chunk0001' ORDER BY ordinal"
                )
            ]
        finally:
            connection.close()

    def _write_meta(self, *, new_terms=None):
        dependency = knowledge_store.compute_dependency_bundle(
            self.root, "chunk0001"
        )
        target_paragraphs = knowledge_store._paragraphs(self.TARGET)
        translations = [
            {
                "segment_id": segment["segment_id"],
                "target_text": target_paragraphs[index - 1],
            }
            for index, segment in enumerate(self._segments(), 1)
        ]
        data = {
            "schema_version": 2,
            "memory_dependency_hash": dependency["dependency_hash"],
            "used_memory_ids": dependency["memory_ids"],
            "new_terms": new_terms or [],
            "segment_translations": translations,
        }
        path = self.root / "output_chunk0001.meta.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path, data

    def _write_review(self, dependency_hash=None):
        dependency_hash = dependency_hash or knowledge_store.compute_dependency_bundle(
            self.root, "chunk0001"
        )["dependency_hash"]
        path = self.root / "review_chunk0001.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "dependency_hash": dependency_hash,
                    "output_hash": hashlib.sha256(
                        (self.root / "output_chunk0001.md").read_bytes()
                    ).hexdigest(),
                    "findings": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_ingest_record_review_then_final_gate(self):
        meta_path, _ = self._write_meta()
        knowledge_store.ingest_sidecars(self.root, [meta_path])
        run_state.record_chunks(str(self.root), ["chunk0001"])
        self._write_review()

        report = quality_gate.evaluate_gate(self.root, "final")

        self.assertTrue(report["ready_for_final"])
        self.assertEqual(report["translation_meta"]["applied"], 1)
        self.assertEqual(report["reviews"]["ingested_files"], 1)
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            clean_audit = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE severity='none' AND status='resolved'"
            ).fetchone()[0]
            self.assertEqual(clean_audit, 1)
        finally:
            connection.close()

    def test_review_is_invalidated_if_output_changes_after_review(self):
        meta_path, _ = self._write_meta()
        knowledge_store.ingest_sidecars(self.root, [meta_path])
        run_state.record_chunks(str(self.root), ["chunk0001"])
        self._write_review()
        self.assertTrue(quality_gate.evaluate_gate(self.root, "final")["ready_for_final"])

        (self.root / "output_chunk0001.md").write_text(
            self.TARGET + "\npost-review edit\n", encoding="utf-8"
        )
        report = quality_gate.evaluate_gate(self.root, "final")

        self.assertFalse(report["ready_for_final"])
        invalid = next(
            item for item in report["blockers"] if item["code"] == "review_invalid"
        )
        self.assertIn("output_hash does not match", invalid["message"])

    def test_translation_meta_cannot_seed_memory_with_text_absent_from_output(self):
        meta_path, data = self._write_meta()
        data["segment_translations"][0]["target_text"] = "invented hidden memory"
        meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "do not match the actual"):
            knowledge_store.ingest_sidecars(self.root, [meta_path])

    def test_output_edit_after_meta_ingest_must_be_rebound_before_record(self):
        meta_path, _ = self._write_meta()
        knowledge_store.ingest_sidecars(self.root, [meta_path])
        (self.root / "output_chunk0001.md").write_text(
            self.TARGET + "\nchanged before record\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "not bound to the current output"):
            run_state.record_chunks(str(self.root), ["chunk0001"])

    def test_new_knowledge_after_translation_preserves_dirty_backwrite(self):
        segment = self._segments()[0]["segment_id"]
        meta_path, used = self._write_meta(
            new_terms=[
                {
                    "surface": "stable claim",
                    "sense": "argumentative proposition",
                    "target_proposal": "稳定主张",
                    "evidence": {
                        "segment_id": segment,
                        "quote": "stable claim",
                    },
                }
            ]
        )
        used_hash = used["memory_dependency_hash"]
        knowledge_store.ingest_sidecars(self.root, [meta_path])
        run_state.record_chunks(str(self.root), ["chunk0001"])
        self._write_review(used_hash)

        report = quality_gate.evaluate_gate(self.root, "final")

        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("chunk_dirty", codes)
        self.assertIn("review_missing_or_stale", codes)
        self.assertIn("blocking_unresolved", codes)
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            row = connection.execute(
                "SELECT dirty, dependency_hash, reviewed_hash FROM chunk_dependencies "
                "WHERE chunk_id='chunk0001'"
            ).fetchone()
            self.assertEqual(row["dirty"], 1)
            self.assertNotEqual(row["dependency_hash"], used_hash)
        finally:
            connection.close()

    def test_uningested_meta_cannot_be_recorded_or_published(self):
        self._write_meta()
        with self.assertRaisesRegex(ValueError, "not been successfully ingested"):
            run_state.record_chunks(str(self.root), ["chunk0001"])
        self._write_review()
        report = quality_gate.evaluate_gate(self.root, "final")
        self.assertIn(
            "translation_meta_missing",
            {item["code"] for item in report["blockers"]},
        )


if __name__ == "__main__":
    unittest.main()
