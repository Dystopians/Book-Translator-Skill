import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import knowledge_store  # noqa: E402
import context_packet  # noqa: E402


class KnowledgeStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.write("chunk0001.md", "Alice met the Mouse.\n\nShe followed the Mouse home.\n")
        self.write("chunk0002.md", "The bank stood beside the river.\n")

    def tearDown(self):
        self._temporary.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value, encoding="utf-8")
        return path

    def write_json(self, name, value):
        return self.write(name, json.dumps(value, ensure_ascii=False, indent=2))

    def initialize(self):
        return knowledge_store.initialize_database(self.root, profile="unit-test")

    def segment(self, chunk_id="chunk0001", order=1):
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            row = connection.execute(
                "SELECT segment_id, source_text FROM segments "
                "WHERE chunk_id = ? AND segment_order = ?",
                (chunk_id, order),
            ).fetchone()
            return row["segment_id"], row["source_text"]
        finally:
            connection.close()

    def empty_analysis(self):
        return {
            "schema_version": 1,
            "terms": [],
            "facts": [],
            "claims": [],
            "style_observations": [],
            "unresolved": [],
        }

    def evidence(self, chunk_id="chunk0001", order=1, quote=None):
        segment_id, source = self.segment(chunk_id, order)
        return {"segment_id": segment_id, "quote": source if quote is None else quote}


class InitializationTests(KnowledgeStoreTestCase):
    def test_init_is_atomic_and_configures_required_schema(self):
        real_replace = os.replace
        with mock.patch.object(knowledge_store.os, "replace", wraps=real_replace) as replace:
            result = self.initialize()

        database = self.root / knowledge_store.DATABASE_FILENAME
        self.assertTrue(database.is_file())
        self.assertGreaterEqual(replace.call_count, 1)
        self.assertEqual(result["schema_version"], 1)

        connection = knowledge_store.open_database(self.root)
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(set(knowledge_store.TABLES).issubset(tables))
            review_columns = [
                row[1] for row in connection.execute("PRAGMA table_info(reviews)")
            ]
            self.assertEqual(
                review_columns,
                [
                    "id",
                    "chunk_id",
                    "dependency_hash",
                    "severity",
                    "status",
                    "payload_json",
                    "created_at",
                ],
            )
            dependency_columns = [
                row[1]
                for row in connection.execute("PRAGMA table_info(chunk_dependencies)")
            ]
            self.assertEqual(
                dependency_columns,
                [
                    "chunk_id",
                    "dependency_hash",
                    "memory_ids_json",
                    "reviewed_hash",
                    "dirty",
                    "revision_count",
                    "updated_at",
                ],
            )
            context_packet._validate_schema(connection)
        finally:
            connection.close()

    def test_segment_ids_are_hash_generated_and_stable(self):
        self.initialize()
        first = self.segment("chunk0001", 1)[0]
        self.assertRegex(first, r"^seg_[0-9a-f]{64}$")

        (self.root / knowledge_store.DATABASE_FILENAME).unlink()
        self.initialize()
        second = self.segment("chunk0001", 1)[0]
        self.assertEqual(first, second)

    def test_auto_profile_uses_whole_book_evidence_and_falls_back_safely(self):
        self.write(
            "chunk0001.md",
            "Pursuant to Section 4, the plaintiff shall notify the court.\n",
        )
        self.write(
            "chunk0002.md",
            "Whereas the defendant may respond under the statute.\n",
        )
        knowledge_store.initialize_database(self.root, profile="auto")
        self.assertEqual(knowledge_store.get_metadata(self.root, "profile"), "legal")
        detection = knowledge_store.get_metadata(self.root, "profile_detection")
        self.assertEqual(detection["basis"], "whole_book_heuristic")

    def test_failed_first_build_leaves_no_authoritative_database(self):
        glossary = {
            "version": 1,
            "terms": [],
        }
        original = self.write_json("glossary.json", glossary).read_bytes()

        with self.assertRaises(ValueError):
            self.initialize()

        self.assertFalse((self.root / knowledge_store.DATABASE_FILENAME).exists())
        self.assertEqual((self.root / "glossary.json").read_bytes(), original)
        self.assertFalse(list(self.root.glob(".translation_state.init.*")))

    def test_failed_migration_keeps_original_database_byte_for_byte(self):
        self.initialize()
        database = self.root / knowledge_store.DATABASE_FILENAME
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = ?",
                    ("0", "schema_version"),
                )
        finally:
            connection.close()
        before = database.read_bytes()

        with mock.patch.object(
            knowledge_store, "_validate_database", side_effect=ValueError("forced")
        ):
            with self.assertRaisesRegex(ValueError, "forced"):
                knowledge_store.initialize_database(self.root)

        self.assertEqual(database.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".translation_state.migrate.*")))

    def test_legacy_import_is_read_only_and_maps_run_state(self):
        glossary = {
            "version": 2,
            "terms": [
                {
                    "id": "Mouse",
                    "source": "Mouse",
                    "target": "老鼠",
                    "category": "character",
                    "aliases": ["the Mouse"],
                    "confidence": "high",
                    "notes": "",
                }
            ],
        }
        run_state = {
            "version": 1,
            "chunks": {
                "chunk0001": {
                    "source_hash": "a" * 64,
                    "output_hash": "b" * 64,
                    "glossary_version_used": "c" * 64,
                    "entity_hashes_used": {"Mouse": "d" * 64},
                }
            },
        }
        glossary_path = self.write_json("glossary.json", glossary)
        run_state_path = self.write_json("run_state.json", run_state)
        glossary_before = glossary_path.read_bytes()
        run_state_before = run_state_path.read_bytes()

        self.initialize()

        self.assertEqual(glossary_path.read_bytes(), glossary_before)
        self.assertEqual(run_state_path.read_bytes(), run_state_before)
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            term = connection.execute(
                "SELECT surface, target FROM terms WHERE surface = ?", ("Mouse",)
            ).fetchone()
            self.assertEqual(tuple(term), ("Mouse", "老鼠"))
            dependency = connection.execute(
                "SELECT dirty FROM chunk_dependencies WHERE chunk_id = ?",
                ("chunk0001",),
            ).fetchone()
            self.assertEqual(dependency["dirty"], 0)
            translated = connection.execute(
                "SELECT COUNT(*) FROM translations WHERE chunk_id = ? AND dirty = 0",
                ("chunk0001",),
            ).fetchone()[0]
            self.assertEqual(translated, 2)
        finally:
            connection.close()

    def test_changed_legacy_glossary_is_highest_priority_override(self):
        glossary = {
            "version": 2,
            "terms": [
                {
                    "id": "Mouse",
                    "source": "Mouse",
                    "target": "老鼠",
                    "category": "character",
                    "aliases": [],
                    "confidence": "high",
                    "notes": "",
                }
            ],
        }
        path = self.write_json("glossary.json", glossary)
        knowledge_store.initialize_database(self.root, profile="general")
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                connection.execute("UPDATE chunk_dependencies SET dirty = 0")
                connection.execute("UPDATE translations SET dirty = 0")
        finally:
            connection.close()
        glossary["terms"][0]["target"] = "鼠先生"
        path.write_text(json.dumps(glossary, ensure_ascii=False), encoding="utf-8")

        knowledge_store.initialize_database(self.root, profile="general")

        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            row = connection.execute(
                "SELECT target, authority FROM terms WHERE surface = 'Mouse'"
            ).fetchone()
            self.assertEqual(tuple(row), ("鼠先生", "user_decision"))
            dirty = connection.execute(
                "SELECT dirty FROM chunk_dependencies WHERE chunk_id='chunk0001'"
            ).fetchone()[0]
            self.assertEqual(dirty, 1)
        finally:
            connection.close()


class AnalysisValidationTests(KnowledgeStoreTestCase):
    def setUp(self):
        super().setUp()
        self.initialize()

    def test_polysemous_surface_can_have_distinct_senses(self):
        data = self.empty_analysis()
        evidence = self.evidence("chunk0002")
        data["terms"] = [
            {
                "surface": "bank",
                "sense": "financial institution",
                "target": "银行",
                "evidence": evidence,
            },
            {
                "surface": "bank",
                "sense": "river edge",
                "target": "河岸",
                "evidence": evidence,
            },
        ]
        path = self.write_json("analysis_chunk0002.json", data)

        result = knowledge_store.ingest_sidecars(self.root, [path])

        self.assertEqual(result["ingested_chunk_ids"], ["chunk0002"])
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            rows = connection.execute(
                "SELECT sense, target FROM terms WHERE surface = ? ORDER BY sense", ("bank",)
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [("financial institution", "银行"), ("river edge", "河岸")],
            )
            self.assertTrue(
                all(row[0].startswith("term_") for row in connection.execute(
                    "SELECT term_id FROM terms WHERE surface = ?", ("bank",)
                ))
            )
        finally:
            connection.close()
        prepared = knowledge_store.prepare_resolutions(self.root)
        self.assertEqual(
            prepared["candidate_clusters"]["terms"][0]["candidate_type"],
            "term_surface_or_alias",
        )
        self.assertTrue(
            any(
                issue["issue_type"] == "term_sense_ambiguity"
                for issue in prepared["issues"]
            )
        )

    def test_fabricated_evidence_rejects_entire_ingest_transaction(self):
        before = knowledge_store.memory_version(self.root)
        data = self.empty_analysis()
        evidence = self.evidence()
        evidence["quote"] = "Alice definitely met a Dragon."
        data["terms"] = [
            {"surface": "Dragon", "target": "龙", "evidence": evidence}
        ]
        path = self.write_json("analysis_chunk0001.json", data)

        with self.assertRaisesRegex(ValueError, "exact substring"):
            knowledge_store.ingest_sidecars(self.root, [path])

        self.assertEqual(knowledge_store.memory_version(self.root), before)
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_evidence_must_belong_to_filename_chunk(self):
        data = self.empty_analysis()
        data["facts"] = [
            {
                "subject": "Alice",
                "predicate": "met",
                "object": "Mouse",
                "evidence": self.evidence("chunk0002"),
            }
        ]
        path = self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "belongs to chunk0002"):
            knowledge_store.ingest_sidecars(self.root, [path])

    def test_unknown_fields_and_payload_chunk_mismatch_are_rejected(self):
        data = self.empty_analysis()
        data["surprise"] = []
        path = self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "unknown field"):
            knowledge_store.ingest_sidecars(self.root, [path])

        del data["surprise"]
        data["chunk_id"] = "chunk0002"
        self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "does not match filename"):
            knowledge_store.ingest_sidecars(self.root, [path])

    def test_nested_unknown_field_and_quote_limit_are_rejected(self):
        data = self.empty_analysis()
        data["terms"] = [
            {
                "surface": "Mouse",
                "target": "老鼠",
                "evidence": self.evidence(),
                "model_private_note": "do not trust me",
            }
        ]
        path = self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "unknown field"):
            knowledge_store.ingest_sidecars(self.root, [path])

        data["terms"][0].pop("model_private_note")
        data["terms"][0]["evidence"]["quote"] = "x" * 501
        self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "500"):
            knowledge_store.ingest_sidecars(self.root, [path])

    def test_array_limit_duplicate_json_keys_and_file_size_are_rejected(self):
        data = self.empty_analysis()
        data["facts"] = [{}] * 101
        path = self.write_json("analysis_chunk0001.json", data)
        with self.assertRaisesRegex(ValueError, "maximum is 100"):
            knowledge_store.ingest_sidecars(self.root, [path])

        path.write_text(
            '{"schema_version":1,"schema_version":1,"terms":[],"facts":[],"claims":[],"style_observations":[],"unresolved":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            knowledge_store.ingest_sidecars(self.root, [path])

        path.write_bytes(b" " * (knowledge_store.MAX_SIDECAR_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            knowledge_store.ingest_sidecars(self.root, [path])

    def test_symlink_sidecar_is_rejected(self):
        target = self.write_json("real-analysis.json", self.empty_analysis())
        link = self.root / "analysis_chunk0001.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            knowledge_store.ingest_sidecars(self.root, [link])

    def test_ingest_is_idempotent_by_canonical_payload_hash(self):
        data = self.empty_analysis()
        data["style_observations"] = [
            {
                "rule": "Keep dialogue concise.",
                "scope": "dialogue",
                "evidence": self.evidence(),
            }
        ]
        path = self.write_json("analysis_chunk0001.json", data)
        first = knowledge_store.ingest_sidecars(self.root, [path])
        second = knowledge_store.ingest_sidecars(self.root, [path])
        self.assertEqual(second["skipped_chunk_ids"], ["chunk0001"])
        self.assertEqual(first["memory_version"], second["memory_version"])

    def test_translation_meta_v2_records_public_dependency_contract(self):
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                knowledge_store._set_metadata(connection, "target_lang", "zh")
                knowledge_store.refresh_chunk_dependencies(connection)
        finally:
            connection.close()
        dependency_hash = knowledge_store.compute_dependency_bundle(
            self.root, "chunk0001"
        )["dependency_hash"]
        first_segment, _ = self.segment("chunk0001", 1)
        second_segment, _ = self.segment("chunk0001", 2)
        data = {
            "schema_version": 2,
            "memory_dependency_hash": dependency_hash,
            "used_memory_ids": {
                "terms": [],
                "facts": [],
                "claims": [],
                "style_rules": [],
                "resolutions": [],
            },
            "new_terms": [
                {
                    "surface": "Mouse",
                    "sense": "character",
                    "target_proposal": "老鼠",
                    "domain": "fiction",
                    "usage_note": "Treat as a proper name in dialogue.",
                    "forbidden_variants": ["耗子"],
                    "evidence": self.evidence(),
                }
            ],
            "segment_translations": [
                {"segment_id": first_segment, "target_text": "爱丽丝遇见了老鼠。"},
                {"segment_id": second_segment, "target_text": "她跟着老鼠回家。"},
            ],
        }
        self.write(
            "output_chunk0001.md",
            "\n\n".join(
                item["target_text"] for item in data["segment_translations"]
            )
            + "\n",
        )
        path = self.write_json("output_chunk0001.meta.json", data)
        knowledge_store.ingest_sidecars(self.root, [path])
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            term = connection.execute(
                "SELECT domain, usage_note, forbidden_json FROM terms WHERE surface = ?",
                ("Mouse",),
            ).fetchone()
            self.assertEqual(term["domain"], "fiction")
            self.assertEqual(json.loads(term["forbidden_json"]), ["耗子"])
            dependency = connection.execute(
                "SELECT dependency_hash, memory_ids_json FROM chunk_dependencies WHERE chunk_id = ?",
                ("chunk0001",),
            ).fetchone()
            self.assertNotEqual(dependency["dependency_hash"], dependency_hash)
            generated_term_ids = json.loads(dependency["memory_ids_json"])["terms"]
            self.assertEqual(len(generated_term_ids), 1)
            translations = connection.execute(
                "SELECT target_text, context_hash, status FROM translations "
                "WHERE chunk_id = ? ORDER BY segment_id",
                ("chunk0001",),
            ).fetchall()
            self.assertEqual({row["target_text"] for row in translations}, {"爱丽丝遇见了老鼠。", "她跟着老鼠回家。"})
            self.assertTrue(all(row["context_hash"] == dependency_hash for row in translations))
            self.assertTrue(all(row["status"] == "translated" for row in translations))
        finally:
            connection.close()

        # A second pass with no new knowledge records completion against the
        # same dependency hash, making the segment TM exactly reusable.
        current = knowledge_store.compute_dependency_bundle(self.root, "chunk0001")
        data["new_terms"] = []
        data["memory_dependency_hash"] = current["dependency_hash"]
        data["used_memory_ids"] = current["memory_ids"]
        self.write_json("output_chunk0001.meta.json", data)
        knowledge_store.ingest_sidecars(self.root, [path])
        packet = context_packet.build_context_packet(
            self.root, "chunk0001.md", phase="translate"
        )
        exact = packet["translation_memory"]["exact"]
        self.assertEqual(len(exact), 2)
        self.assertTrue(all(item["reusable"] for item in exact))


class ResolutionAndStatusTests(KnowledgeStoreTestCase):
    def setUp(self):
        super().setUp()
        self.initialize()

    def _ingest_term(self, target):
        data = self.empty_analysis()
        data["terms"] = [
            {
                "surface": "Mouse",
                "sense": "character",
                "target": target,
                "category": "character",
                "evidence": self.evidence(),
            }
        ]
        path = self.write_json("analysis_chunk0001.json", data)
        return knowledge_store.ingest_sidecars(self.root, [path])

    def test_conflict_becomes_issue_and_resolution_marks_dependents_dirty(self):
        self._ingest_term("老鼠")
        self._ingest_term("鼠先生")
        prepared = knowledge_store.prepare_resolutions(self.root)
        self.assertEqual(len(prepared["issues"]), 2)
        issue = next(
            item for item in prepared["issues"]
            if item["issue_type"] == "term_target_conflict"
        )
        self.assertIn("issue_id", issue)
        self.assertNotIn("unresolved_id", issue)
        term_id = issue["item_key"]

        # Simulate another translated chunk having consumed this memory object.
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                connection.execute(
                    "UPDATE chunk_dependencies SET memory_ids_json = ?, dirty = 0 "
                    "WHERE chunk_id = ?",
                    (json.dumps({term_id: "old-hash"}), "chunk0002"),
                )
                connection.execute(
                    "UPDATE chunk_dependencies SET dirty = 0 WHERE chunk_id = ?",
                    ("chunk0001",),
                )
                connection.execute("UPDATE translations SET dirty = 0")
        finally:
            connection.close()

        decisions = {
            "schema_version": 1,
            "decisions": [
                {
                    # Compatibility input alias; all output remains issue_id.
                    "unresolved_id": issue["issue_id"],
                    "action": "accept_proposed",
                    "notes": "context supports honorific form",
                }
            ],
        }
        decision_path = self.write_json("decisions.json", decisions)
        applied = knowledge_store.apply_resolutions(self.root, decision_path)

        self.assertEqual(applied["applied_issue_ids"], [issue["issue_id"]])
        self.assertEqual(applied["dirty_chunk_ids"], ["chunk0001", "chunk0002"])
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            target = connection.execute(
                "SELECT target FROM terms WHERE term_id = ?", (term_id,)
            ).fetchone()[0]
            self.assertEqual(target, "鼠先生")
            dirty = connection.execute(
                "SELECT chunk_id, dirty FROM chunk_dependencies ORDER BY chunk_id"
            ).fetchall()
            self.assertEqual([tuple(row) for row in dirty], [("chunk0001", 1), ("chunk0002", 1)])
            translation_dirty = connection.execute(
                "SELECT COUNT(*) FROM translations WHERE dirty = 1"
            ).fetchone()[0]
            self.assertEqual(translation_dirty, 3)
        finally:
            connection.close()

        after = knowledge_store.status(self.root)
        self.assertEqual(after["open_issue_count"], 0)
        self.assertEqual(after["counts"]["decisions"], 1)

    def test_claim_uses_target_gloss_and_dependency_bundle_is_deterministic(self):
        data = self.empty_analysis()
        data["claims"] = [
            {
                "claim": "Mouse is a title, not a species label.",
                "target_gloss": "将 Mouse 视为角色称号",
                "evidence": self.evidence(),
            }
        ]
        self.write_json("analysis_chunk0001.json", data)
        knowledge_store.ingest_sidecars(
            self.root, [self.root / "analysis_chunk0001.json"]
        )

        first = knowledge_store.compute_dependency_bundle(self.root, "chunk0001")
        second = knowledge_store.compute_dependency_bundle(self.root, "chunk0001")
        self.assertEqual(first["dependency_hash"], second["dependency_hash"])
        self.assertEqual(first["claims"][0]["target_gloss"], "将 Mouse 视为角色称号")

    def test_status_and_snapshot_expose_authoritative_issue_id(self):
        data = self.empty_analysis()
        data["unresolved"] = [
            {
                "issue_type": "pronoun_reference",
                "summary": "Who does She refer to?",
                "options": ["Alice", "Mouse"],
                "evidence": self.evidence(order=2, quote="She followed the Mouse home."),
            }
        ]
        path = self.write_json("analysis_chunk0001.json", data)
        knowledge_store.ingest_sidecars(self.root, [path])

        status = knowledge_store.status(self.root)
        snapshot = knowledge_store.snapshot(self.root)
        self.assertEqual(status["open_issue_count"], 1)
        self.assertEqual(status["counts"]["segments"], 3)
        self.assertIn("issue_id", snapshot["unresolved"][0])
        self.assertNotIn("unresolved_id", snapshot["unresolved"][0])


class LongRangeConvergenceTests(KnowledgeStoreTestCase):
    def setUp(self):
        super().setUp()
        for number in (10, 20, 25, 30, 40):
            self.write(
                f"chunk{number:04d}.md",
                f"Neutral bridge chapter {number} discusses sunlight and ordinary weather.\n",
            )
        self.write(
            "chunk0050.md",
            "Much later, Mouse means a ceremonial title rather than a species.\n",
        )
        self.initialize()

    def _analysis(self, chunk_id, **contents):
        data = self.empty_analysis()
        data.update(contents)
        path = self.write_json(f"analysis_{chunk_id}.json", data)
        return knowledge_store.ingest_sidecars(self.root, [path])

    def _mouse_term(self, chunk_id, order=1, *, explicit=False):
        return {
            "surface": "Mouse",
            "sense": "ceremonial title",
            "target": "鼠衔",
            "category": "title",
            "evidence_basis": "explicit_definition" if explicit else "book_usage",
            "evidence": self.evidence(chunk_id, order),
        }

    def test_late_term_evidence_dirties_only_relevant_early_chunks(self):
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                connection.execute("UPDATE chunk_dependencies SET dirty = 0")
                connection.execute("UPDATE translations SET dirty = 0")
        finally:
            connection.close()

        result = self._analysis(
            "chunk0050", terms=[self._mouse_term("chunk0050")]
        )

        self.assertIn("chunk0001", result["dirty_chunk_ids"])
        self.assertIn("chunk0050", result["dirty_chunk_ids"])
        self.assertNotIn("chunk0025", result["dirty_chunk_ids"])
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            rows = connection.execute(
                "SELECT chunk_id, dirty FROM chunk_dependencies ORDER BY chunk_id"
            ).fetchall()
            dirty_by_chunk = {row["chunk_id"]: row["dirty"] for row in rows}
            self.assertEqual(dirty_by_chunk["chunk0001"], 1)
            self.assertEqual(dirty_by_chunk["chunk0025"], 0)
            self.assertEqual(dirty_by_chunk["chunk0050"], 1)
        finally:
            connection.close()

    def test_two_chunks_plus_current_reviews_auto_confirm_then_requeue(self):
        self._analysis(
            "chunk0001", terms=[self._mouse_term("chunk0001")]
        )
        self._analysis(
            "chunk0050", terms=[self._mouse_term("chunk0050")]
        )
        connection = knowledge_store.open_database(self.root)
        try:
            with knowledge_store.write_transaction(connection):
                # Simulate two independent, clean reviews against the exact
                # current dependency versions of both evidence chunks.
                connection.execute(
                    "UPDATE chunk_dependencies SET dirty = 0, reviewed_hash = dependency_hash "
                    "WHERE chunk_id IN ('chunk0001', 'chunk0050')"
                )
                confirmation = knowledge_store.auto_confirm_supported_knowledge(
                    connection
                )
        finally:
            connection.close()

        self.assertEqual(len(confirmation["promoted_ids"]), 1)
        self.assertIn("chunk0001", confirmation["dirty_chunk_ids"])
        self.assertIn("chunk0050", confirmation["dirty_chunk_ids"])
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            term = connection.execute(
                "SELECT status, authority FROM terms WHERE surface = 'Mouse'"
            ).fetchone()
            self.assertEqual(tuple(term), ("active", "corroborated_book_evidence"))
            open_confirmation = connection.execute(
                "SELECT COUNT(*) FROM unresolved WHERE issue_type = 'knowledge_confirmation' "
                "AND status = 'open'"
            ).fetchone()[0]
            self.assertEqual(open_confirmation, 0)
        finally:
            connection.close()

    def test_verifiable_explicit_definition_needs_no_confirmation_queue(self):
        self._analysis(
            "chunk0050", terms=[self._mouse_term("chunk0050", explicit=True)]
        )
        connection = knowledge_store.open_database(self.root, readonly=True)
        try:
            term = connection.execute(
                "SELECT status, authority FROM terms WHERE surface = 'Mouse'"
            ).fetchone()
            self.assertEqual(tuple(term), ("active", "source_definition"))
            count = connection.execute(
                "SELECT COUNT(*) FROM unresolved WHERE issue_type = 'knowledge_confirmation'"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            connection.close()

    def test_distant_claim_paraphrases_enter_restricted_candidate_queue(self):
        common = {
            "holder": "Narrator",
            "polarity": "affirmed",
            "modality": "certain",
            "scope": "book",
            "target_gloss": "Mouse 是称号而非物种",
        }
        self._analysis(
            "chunk0001",
            claims=[
                {
                    **common,
                    "proposition": "Mouse ceremonial title not species",
                    "evidence": self.evidence("chunk0001"),
                }
            ],
        )
        self._analysis(
            "chunk0050",
            claims=[
                {
                    **common,
                    "proposition": "Mouse means ceremonial title rather than species",
                    "evidence": self.evidence("chunk0050"),
                }
            ],
        )
        prepared = knowledge_store.prepare_resolutions(self.root)
        self.assertTrue(prepared["candidate_clusters"]["claims"])
        self.assertTrue(
            any(
                issue["issue_type"] == "claim_semantic_candidate"
                for issue in prepared["issues"]
            )
        )


class ExternalSourceProvenanceTests(KnowledgeStoreTestCase):
    def setUp(self):
        super().setUp()
        self.initialize()

    def _record(self, **overrides):
        data = {
            "url": "https://reference.example/term",
            "allowed_domain": "reference.example",
            "retrieved_at": "2026-07-16T12:00:00Z",
            "content_hash": "a" * 64,
            "conclusion": "The reference supports the selected technical sense.",
            "authorized_by_user": True,
        }
        data.update(overrides)
        path = self.write_json("external-source.json", data)
        return knowledge_store.record_external_source(self.root, path)

    def test_requires_explicit_authorization_and_exact_allowed_domain(self):
        with self.assertRaisesRegex(ValueError, "explicit user authorization"):
            self._record(authorized_by_user=False)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self._record(allowed_domain="example")
        result = self._record()
        self.assertTrue(result["recorded"])
        snapshot = knowledge_store.snapshot(self.root)
        self.assertEqual(len(snapshot["external_sources"]), 1)
        self.assertEqual(
            snapshot["external_sources"][0]["content_hash"], "a" * 64
        )


if __name__ == "__main__":
    unittest.main()
