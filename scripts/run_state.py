#!/usr/bin/env python3
"""
run_state.py - Selective re-translation state for translate-book.

The state file records what glossary and source/output hashes were used for
each translated chunk. Future runs can then decide which chunks need actual
re-translation after glossary or source changes, and which existing outputs
only need their state recorded.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import glossary as glossary_mod
import meta as meta_mod
from manifest import file_hash, load_manifest, read_output_text


RUN_STATE_VERSION = 1
RUN_STATE_FILE = "run_state.json"


def _run_state_path(temp_dir):
    return os.path.join(temp_dir, RUN_STATE_FILE)


def _empty_state():
    return {"version": RUN_STATE_VERSION, "chunks": {}}


def load_run_state(temp_dir):
    path = _run_state_path(temp_dir)
    if not os.path.exists(path):
        return _empty_state()
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if data.get("version") != RUN_STATE_VERSION:
        raise ValueError(
            f"run_state.json version mismatch: expected {RUN_STATE_VERSION}, "
            f"got {data.get('version')!r}"
        )
    chunks = data.get("chunks")
    if not isinstance(chunks, dict):
        raise ValueError("run_state.json field 'chunks' must be an object")
    return data


def save_run_state(temp_dir, state):
    path = _run_state_path(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.run_state.', suffix='.json', dir=temp_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _chunk_entries_from_manifest(temp_dir):
    manifest = load_manifest(temp_dir)
    if manifest:
        entries = []
        for chunk in sorted(manifest.get("chunks", []), key=lambda c: c.get("order", 0)):
            entries.append({
                "id": chunk["id"],
                "source_file": chunk["source_file"],
                "output_file": chunk["output_file"],
                "manifest_source_hash": chunk.get("source_hash", ""),
                "order": chunk.get("order", 0),
            })
        return entries

    temp_path = Path(temp_dir)
    source_files = sorted(
        p for p in temp_path.glob('chunk*.md')
        if not p.name.startswith('output_')
    )
    return [
        {
            "id": p.stem,
            "source_file": p.name,
            "output_file": f"output_{p.name}",
            "manifest_source_hash": "",
            "order": i,
        }
        for i, p in enumerate(source_files, 1)
    ]


def _load_glossary(temp_dir):
    path = os.path.join(temp_dir, 'glossary.json')
    if not os.path.exists(path):
        return None, "", [], {}
    glossary = glossary_mod.load_glossary(path)
    glossary_hash = glossary_mod.glossary_hash(glossary)
    terms = glossary.get('terms', [])
    term_by_id = {t.get('id', t.get('source')): t for t in terms}
    return glossary, glossary_hash, terms, term_by_id


def _knowledge_database_exists(temp_dir):
    path = os.path.join(temp_dir, 'translation_state.sqlite3')
    return os.path.isfile(path) and not os.path.islink(path)


def _knowledge_dependency_bundle(temp_dir, chunk_id):
    """Return the evidence-store dependency packet when enhanced mode is active."""
    if not _knowledge_database_exists(temp_dir):
        return None
    import knowledge_store

    return knowledge_store.compute_dependency_bundle(temp_dir, chunk_id)


def _knowledge_chunk_state(temp_dir, chunk_id):
    if not _knowledge_database_exists(temp_dir):
        return None
    import knowledge_store

    connection = knowledge_store.open_database(temp_dir, readonly=True)
    try:
        row = connection.execute(
            "SELECT dependency_hash, memory_ids_json, reviewed_hash, dirty, "
            "revision_count FROM chunk_dependencies WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        connection.close()


def _knowledge_blocking_unresolved(temp_dir):
    if not _knowledge_database_exists(temp_dir):
        return 0
    import knowledge_store

    connection = knowledge_store.open_database(temp_dir, readonly=True)
    try:
        columns = {
            row['name'] for row in connection.execute("PRAGMA table_info(unresolved)")
        }
        if 'impact' in columns:
            row = connection.execute(
                "SELECT COUNT(*) FROM unresolved WHERE lower(status) NOT IN "
                "('resolved', 'closed', 'dismissed') AND lower(impact) IN ('critical', 'high')"
            ).fetchone()
        else:
            # Older enhanced stores did not classify impact. Treat every open
            # issue as blocking rather than publishing through uncertainty.
            row = connection.execute(
                "SELECT COUNT(*) FROM unresolved WHERE lower(status) NOT IN "
                "('resolved', 'closed', 'dismissed')"
            ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def _record_knowledge_translation(
    temp_dir,
    chunk_id,
    output_hash,
    *,
    dependency_hash_used=None,
    memory_ids_used=None,
    legacy_adoption=False,
):
    """Atomically mark one output as translated against current knowledge."""
    if not _knowledge_database_exists(temp_dir):
        return None
    import knowledge_store

    connection = knowledge_store.open_database(temp_dir, readonly=False)
    try:
        bundle = knowledge_store.compute_dependency_bundle(connection, chunk_id)
        dependency_hash = bundle['dependency_hash']
        memory_ids = bundle.get('memory_ids', {})
        used_hash = dependency_hash_used or dependency_hash
        used_ids = memory_ids_used if memory_ids_used is not None else memory_ids
        stale_translation = used_hash != dependency_hash
        previous = connection.execute(
            "SELECT output_hash FROM translations WHERE chunk_id = ? ORDER BY segment_id LIMIT 1",
            (chunk_id,),
        ).fetchone()
        semantic_revision = int(
            previous is None or str(previous['output_hash'] or '') != output_hash
        )
        history = []
        for row in connection.execute(
            "SELECT payload_json FROM audit_log WHERE action = 'record_translation' "
            "AND object_type = 'chunk' AND object_id = ? ORDER BY id DESC LIMIT 20",
            (chunk_id,),
        ):
            try:
                historical_hash = json.loads(row['payload_json']).get('dependency_hash')
            except (TypeError, json.JSONDecodeError):
                continue
            if historical_hash and (not history or history[-1] != historical_hash):
                history.append(historical_hash)
        oscillating = bool(
            history
            and dependency_hash != history[0]
            and dependency_hash in history[1:]
        )
        with knowledge_store.write_transaction(connection):
            if oscillating:
                segment = connection.execute(
                    "SELECT segment_id, source_text FROM segments WHERE chunk_id = ? "
                    "ORDER BY ordinal LIMIT 1",
                    (chunk_id,),
                ).fetchone()
                if segment is None:
                    raise ValueError(f"Missing canonical segment for {chunk_id!r}")
                quote = str(segment['source_text'])[:500]
                knowledge_store._insert_unresolved(
                    connection,
                    issue_type="dependency_oscillation",
                    chunk_id=chunk_id,
                    item_type="explicit",
                    item_key=chunk_id,
                    summary="Knowledge dependency returned to an earlier state",
                    existing={"most_recent_dependency_hash": history[0]},
                    proposed={"repeated_dependency_hash": dependency_hash},
                    evidence={"segment_id": segment['segment_id'], "quote": quote},
                    question=(
                        "Which knowledge decision should remain authoritative before this "
                        "chunk is translated again?"
                    ),
                    options=["keep latest decision", "restore earlier decision", "set manually"],
                    needed_evidence="An explicit user decision that breaks the A-B-A cycle",
                    impact="critical",
                )
                connection.execute(
                    "UPDATE chunk_dependencies SET reviewed_hash = '', dirty = 1 "
                    "WHERE chunk_id = ?",
                    (chunk_id,),
                )
                connection.execute(
                    "UPDATE translations SET dirty = 1 WHERE chunk_id = ?",
                    (chunk_id,),
                )
                knowledge_store._audit(
                    connection,
                    "dependency_oscillation",
                    "chunk",
                    chunk_id,
                    {"current": dependency_hash, "history": history},
                )
            else:
                cursor = connection.execute(
                    "UPDATE chunk_dependencies SET dependency_hash = ?, memory_ids_json = ?, "
                    "reviewed_hash = '', dirty = ?, revision_count = revision_count + ? "
                    "WHERE chunk_id = ?",
                    (
                        dependency_hash,
                        json.dumps(memory_ids, ensure_ascii=False, sort_keys=True),
                        int(stale_translation),
                        semantic_revision,
                        chunk_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Missing knowledge dependency row for {chunk_id!r}")

                columns = {
                    row['name']
                    for row in connection.execute("PRAGMA table_info(translations)")
                }
                assignments = [
                    "dependency_hash = ?", "output_hash = ?", "status = 'translated'", "dirty = ?"
                ]
                params = [dependency_hash, output_hash, int(stale_translation)]
                if 'context_hash' in columns:
                    assignments.append("context_hash = ?")
                    params.append(used_hash)
                params.append(chunk_id)
                connection.execute(
                    "UPDATE translations SET " + ", ".join(assignments) + " WHERE chunk_id = ?",
                    tuple(params),
                )
                knowledge_store._audit(
                    connection,
                    "record_translation",
                    "chunk",
                    chunk_id,
                    {
                        "dependency_hash": dependency_hash,
                        "dependency_hash_used": used_hash,
                        "output_hash": output_hash,
                        "semantic_revision": bool(semantic_revision),
                        "stale_translation": stale_translation,
                        "legacy_adoption": bool(legacy_adoption),
                    },
                )
        if oscillating:
            raise ValueError(
                f"Knowledge dependency oscillation detected for {chunk_id}; "
                "manual resolution is required"
            )
        return bundle
    finally:
        connection.close()


def _selected_terms_for_chunk(glossary, source_path):
    if glossary is None or not os.path.exists(source_path):
        return []
    text = Path(source_path).read_text(encoding='utf-8')
    return glossary_mod.select_terms_for_chunk(glossary, text)


def _term_ids_and_hashes(terms):
    ids = []
    hashes = {}
    for term in terms:
        term_id = term.get('id', term.get('source'))
        ids.append(term_id)
        hashes[term_id] = glossary_mod.term_hash(term)
    return ids, hashes


def _now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_chunk_record(
    temp_dir,
    chunk_id,
    *,
    dependency_hash_used=None,
    memory_ids_used=None,
    legacy_adopted=False,
):
    entries = {entry["id"]: entry for entry in _chunk_entries_from_manifest(temp_dir)}
    if chunk_id not in entries:
        raise ValueError(f"Unknown chunk id {chunk_id!r}")

    entry = entries[chunk_id]
    source_path = os.path.join(temp_dir, entry["source_file"])
    output_path = os.path.join(temp_dir, entry["output_file"])
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source chunk not found: {source_path}")
    if not os.path.exists(output_path):
        raise FileNotFoundError(f"Output chunk not found: {output_path}")
    if os.path.getsize(output_path) == 0:
        raise ValueError(f"Output chunk is empty: {output_path}")
    output_text = read_output_text(output_path)
    if output_text is None:
        raise ValueError(f"Output chunk is not readable UTF-8 text: {output_path}")
    if not output_text.strip():
        raise ValueError(f"Output chunk is blank (whitespace-only): {output_path}")

    glossary, glossary_hash, _, _ = _load_glossary(temp_dir)
    selected_terms = _selected_terms_for_chunk(glossary, source_path)
    entity_ids, entity_hashes = _term_ids_and_hashes(selected_terms)

    knowledge_bundle = _knowledge_dependency_bundle(temp_dir, chunk_id)

    record = {
        "source_file": entry["source_file"],
        "output_file": entry["output_file"],
        "source_hash": file_hash(source_path),
        "output_hash": file_hash(output_path),
        "glossary_version_used": glossary_hash,
        "entity_ids_used": entity_ids,
        "entity_hashes_used": entity_hashes,
        "updated_at": _now_utc(),
    }
    if knowledge_bundle is not None:
        record["memory_dependency_hash"] = (
            dependency_hash_used or knowledge_bundle["dependency_hash"]
        )
        record["memory_ids_used"] = (
            memory_ids_used
            if memory_ids_used is not None
            else knowledge_bundle.get("memory_ids", {})
        )
        record["memory_version_used"] = knowledge_bundle.get("memory_version", 0)
        record["legacy_adopted"] = bool(legacy_adopted)
    return record


def _applied_translation_meta(temp_dir, chunk_id):
    path = os.path.join(temp_dir, f"output_{chunk_id}.meta.json")
    data = meta_mod.load_meta(path)
    if data.get("schema_version") != 2:
        raise ValueError(f"{path}: enhanced translation requires meta schema v2")
    import knowledge_store

    connection = knowledge_store.open_database(temp_dir, readonly=True)
    try:
        applied_hash = knowledge_store.get_metadata(
            connection, f"translation_meta_hash:{chunk_id}"
        )
        applied_output_hash = knowledge_store.get_metadata(
            connection, f"translation_meta_output_hash:{chunk_id}"
        )
    finally:
        connection.close()
    actual_hash = meta_mod.meta_content_hash(data)
    if applied_hash != actual_hash:
        raise ValueError(
            f"{path}: v2 meta has not been successfully ingested into the knowledge store"
        )
    output_path = os.path.join(temp_dir, f"output_{chunk_id}.md")
    if not applied_output_hash or applied_output_hash != file_hash(output_path):
        raise ValueError(
            f"{path}: applied v2 meta is not bound to the current output_{chunk_id}.md"
        )
    return data


def record_chunks(temp_dir, chunk_ids, *, legacy_adoption=False):
    state = load_run_state(temp_dir)
    recorded = []
    records = {}
    knowledge_inputs = {}
    for chunk_id in chunk_ids:
        dependency_hash_used = None
        memory_ids_used = None
        adopted = False
        if _knowledge_database_exists(temp_dir):
            try:
                meta_data = _applied_translation_meta(temp_dir, chunk_id)
            except FileNotFoundError:
                if not legacy_adoption:
                    raise ValueError(
                        f"Missing applied output_{chunk_id}.meta.json v2; use explicit "
                        "legacy adoption only for a pre-existing translation"
                    )
                adopted = True
            else:
                dependency_hash_used = meta_data["memory_dependency_hash"]
                memory_ids_used = meta_data["used_memory_ids"]
        record = build_chunk_record(
            temp_dir,
            chunk_id,
            dependency_hash_used=dependency_hash_used,
            memory_ids_used=memory_ids_used,
            legacy_adopted=adopted,
        )
        state["chunks"][chunk_id] = record
        records[chunk_id] = record
        knowledge_inputs[chunk_id] = {
            "dependency_hash_used": dependency_hash_used,
            "memory_ids_used": memory_ids_used,
            "legacy_adoption": adopted,
        }
        recorded.append(chunk_id)
    # Persist the JSON record first. If the later database update fails, the
    # knowledge dirty bit remains conservative and the chunk will be re-planned.
    save_run_state(temp_dir, state)
    for chunk_id, record in records.items():
        _record_knowledge_translation(
            temp_dir,
            chunk_id,
            record["output_hash"],
            **knowledge_inputs[chunk_id],
        )
    return recorded


def _reason(item, code, detail=None):
    if detail is None:
        item["reasons"].append(code)
    else:
        item["reasons"].append({"code": code, "detail": detail})


def plan(temp_dir, retranslate_untracked=False):
    state = load_run_state(temp_dir)
    glossary, glossary_hash, _, _ = _load_glossary(temp_dir)

    result = {
        "temp_dir": temp_dir,
        "glossary_hash": glossary_hash,
        "translation_chunk_ids": [],
        "record_only_chunk_ids": [],
        "unchanged_chunk_ids": [],
        "blocked_chunk_ids": [],
        "review_chunk_ids": [],
        "converged": False,
        "max_semantic_revision_rounds": 3,
        "chunks": [],
        "decision_rules": [
            "missing_output_or_empty_output",
            "blank_or_unreadable_output",
            "manifest_source_hash_changed",
            "untracked_existing_output",
            "source_hash_changed_since_record",
            "glossary_term_selection_or_term_hash_changed",
            "knowledge_dependency_or_dirty_state_changed",
        ],
        "record_update_rules": [
            "output_hash_changed_since_record",
        ],
    }

    entries = _chunk_entries_from_manifest(temp_dir)
    records = state.get("chunks", {})

    for entry in entries:
        chunk_id = entry["id"]
        source_path = os.path.join(temp_dir, entry["source_file"])
        output_path = os.path.join(temp_dir, entry["output_file"])
        item = {
            "chunk_id": chunk_id,
            "source_file": entry["source_file"],
            "output_file": entry["output_file"],
            "action": "unchanged",
            "reasons": [],
        }

        if not os.path.exists(output_path):
            item["action"] = "translate"
            _reason(item, "missing_output")
        elif os.path.getsize(output_path) == 0:
            item["action"] = "translate"
            _reason(item, "empty_output")
        else:
            output_text = read_output_text(output_path)
            if output_text is None:
                item["action"] = "translate"
                _reason(item, "unreadable_output")
            elif not output_text.strip():
                item["action"] = "translate"
                _reason(item, "blank_output")

        current_source_hash = file_hash(source_path) if os.path.exists(source_path) else ""
        manifest_source_hash = entry.get("manifest_source_hash", "")
        if item["action"] == "unchanged" and manifest_source_hash:
            if current_source_hash != manifest_source_hash:
                item["action"] = "translate"
                _reason(item, "manifest_source_hash_changed")

        record = records.get(chunk_id)
        if item["action"] == "unchanged" and record is None:
            if retranslate_untracked:
                item["action"] = "translate"
                _reason(item, "untracked_existing_output")
            else:
                item["action"] = "record"
                _reason(item, "untracked_existing_output")

        if item["action"] == "unchanged" and record is not None:
            if record.get("source_hash") != current_source_hash:
                item["action"] = "translate"
                _reason(item, "source_hash_changed_since_record")

        knowledge_bundle = _knowledge_dependency_bundle(temp_dir, chunk_id)
        knowledge_state = _knowledge_chunk_state(temp_dir, chunk_id)
        if item["action"] == "unchanged" and record is not None and knowledge_bundle is not None:
            recorded_dependency = record.get("memory_dependency_hash")
            current_dependency = knowledge_bundle.get("dependency_hash")
            if not recorded_dependency:
                # Adopt legacy output without a mass retranslation. It still
                # needs an independent review before final publication.
                item["action"] = "record"
                _reason(item, "knowledge_state_untracked")
            elif recorded_dependency != current_dependency:
                item["action"] = "translate"
                _reason(item, "knowledge_dependency_hash_changed")
            elif knowledge_state is None:
                item["action"] = "translate"
                _reason(item, "knowledge_dependency_state_missing")
            elif bool(knowledge_state.get("dirty")):
                item["action"] = "translate"
                _reason(item, "knowledge_chunk_dirty")

        if item["action"] == "unchanged" and record is not None:
            selected_terms = _selected_terms_for_chunk(glossary, source_path)
            current_entity_ids, current_entity_hashes = _term_ids_and_hashes(selected_terms)
            recorded_entity_ids = record.get("entity_ids_used", [])
            recorded_entity_hashes = record.get("entity_hashes_used", {})

            if current_entity_ids != recorded_entity_ids:
                item["action"] = "translate"
                _reason(item, "glossary_term_selection_changed")
            else:
                changed_ids = [
                    term_id for term_id in current_entity_ids
                    if current_entity_hashes.get(term_id) != recorded_entity_hashes.get(term_id)
                ]
                if changed_ids:
                    item["action"] = "translate"
                    _reason(item, "glossary_term_hash_changed", changed_ids)

        if item["action"] == "unchanged" and record is not None:
            current_output_hash = file_hash(output_path) if os.path.exists(output_path) else ""
            if record.get("output_hash") != current_output_hash:
                item["action"] = "record"
                _reason(item, "output_hash_changed_since_record")

        if (
            item["action"] == "translate"
            and knowledge_state is not None
            and int(knowledge_state.get("revision_count") or 0) >= 3
        ):
            item["action"] = "blocked"
            _reason(item, "semantic_revision_limit_reached", 3)

        if item["action"] == "translate":
            result["translation_chunk_ids"].append(chunk_id)
        elif item["action"] == "record":
            result["record_only_chunk_ids"].append(chunk_id)
        elif item["action"] == "blocked":
            result["blocked_chunk_ids"].append(chunk_id)
        else:
            result["unchanged_chunk_ids"].append(chunk_id)
        if knowledge_state is not None:
            if (
                item["action"] != "unchanged"
                or knowledge_state.get("reviewed_hash") != knowledge_state.get("dependency_hash")
                or bool(knowledge_state.get("dirty"))
            ):
                result["review_chunk_ids"].append(chunk_id)
        result["chunks"].append(item)

    result["blocking_unresolved"] = _knowledge_blocking_unresolved(temp_dir)
    result["converged"] = (
        not result["translation_chunk_ids"]
        and not result["record_only_chunk_ids"]
        and not result["blocked_chunk_ids"]
        and not result["review_chunk_ids"]
        and result["blocking_unresolved"] == 0
    )
    return result


def status(temp_dir):
    state = load_run_state(temp_dir)
    entries = _chunk_entries_from_manifest(temp_dir)
    planned = plan(temp_dir)
    return {
        "temp_dir": temp_dir,
        "tracked_chunks": len(state.get("chunks", {})),
        "source_chunks": len(entries),
        "translation_needed": len(planned["translation_chunk_ids"]),
        "record_only_needed": len(planned["record_only_chunk_ids"]),
        "unchanged": len(planned["unchanged_chunk_ids"]),
        "review_needed": len(planned.get("review_chunk_ids", [])),
        "blocked": len(planned.get("blocked_chunk_ids", [])),
        "converged": planned.get("converged", False),
    }


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Track selective re-translation state")
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_plan = sub.add_parser('plan', help="Decide which chunks need translation or state recording")
    p_plan.add_argument('temp_dir')
    p_plan.add_argument(
        '--retranslate-untracked',
        action='store_true',
        help="Treat existing outputs without run_state records as needing translation",
    )

    p_record = sub.add_parser('record', help="Record one or more completed output chunks")
    p_record.add_argument('temp_dir')
    p_record.add_argument('chunk_ids', nargs='+')
    p_record.add_argument(
        '--legacy-adoption',
        action='store_true',
        help="Explicitly adopt pre-enhancement outputs that have no v2 meta sidecar",
    )

    p_record_all = sub.add_parser('record-all', help="Record every complete output chunk")
    p_record_all.add_argument('temp_dir')

    p_status = sub.add_parser('status', help="Show run_state progress summary")
    p_status.add_argument('temp_dir')

    args = parser.parse_args()

    try:
        if args.cmd == 'plan':
            _print_json(plan(args.temp_dir, retranslate_untracked=args.retranslate_untracked))
        elif args.cmd == 'record':
            _print_json({
                "recorded_chunk_ids": record_chunks(
                    args.temp_dir,
                    args.chunk_ids,
                    legacy_adoption=args.legacy_adoption,
                )
            })
        elif args.cmd == 'record-all':
            plan_data = plan(args.temp_dir)
            eligible = [
                item["chunk_id"] for item in plan_data["chunks"]
                if item["action"] in ("record", "unchanged")
            ]
            _print_json({
                "recorded_chunk_ids": record_chunks(
                    args.temp_dir, eligible, legacy_adoption=True
                )
            })
        elif args.cmd == 'status':
            _print_json(status(args.temp_dir))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
