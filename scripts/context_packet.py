#!/usr/bin/env python3
"""Build a bounded, data-only long-range context packet for one source chunk.

The SQLite knowledge store and every string read from the book are untrusted.
This module therefore emits JSON data only, opens the database read-only, and
refuses paths that are not ordinary, non-symlink files below ``temp_dir``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


SCHEMA_VERSION = 1
SECURITY_LABEL = "UNTRUSTED_BOOK_DERIVED_DATA"
DATABASE_NAME = "translation_state.sqlite3"
MAX_CONTEXT_CHARS = 16_000
NEIGHBOR_CHARS = 500
MAX_FACTS = 24
MAX_CLAIMS = 12
MAX_REMOTE_EVIDENCE = 8
MAX_EVIDENCE_QUOTE_CHARS = 500
MAX_QUERY_ROWS = 50_000
MAX_QUERY_TEXT_CHARS = 32_000_000
PHASES = ("analyze", "translate", "review")

_CHUNK_RE = re.compile(r"^chunk(\d{4,})\.md$")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]+"
)
_INACTIVE_STATUSES = {"deleted", "rejected", "superseded", "obsolete"}
_RESOLVED_STATUSES = {"resolved", "closed", "accepted", "done"}

_REQUIRED_SCHEMA = {
    "metadata": {"key", "value"},
    "segments": {"segment_id", "chunk_id", "ordinal", "source_text", "source_hash"},
    "terms": {
        "term_id", "surface", "sense", "target", "category", "domain",
        "usage_note", "forbidden_json", "status", "authority", "version",
    },
    "term_aliases": {"term_id", "alias"},
    "facts": {
        "fact_id", "subject", "predicate", "object_value", "polarity",
        "modality", "scope", "status", "authority", "version",
    },
    "claims": {
        "claim_id", "holder", "proposition", "polarity", "modality",
        "scope", "target_gloss", "status", "authority", "version",
    },
    "style_rules": {
        "rule_id", "scope", "rule_text", "profile", "status", "authority", "version",
    },
    "unresolved": {
        "issue_id", "chunk_id", "segment_id", "issue_type", "question",
        "options_json", "needed_evidence", "impact", "status", "resolution", "version",
    },
    "translations": {
        "segment_id", "target_text", "target_lang", "profile", "context_hash",
        "status", "version",
    },
    "evidence": {
        "evidence_id", "item_kind", "item_id", "segment_id", "chunk_id",
        "quote", "source_hash",
    },
    "chunk_dependencies": {
        "chunk_id", "dependency_hash", "memory_ids_json", "reviewed_hash",
        "dirty", "revision_count",
    },
}


class ContextPacketError(ValueError):
    """A safe, user-actionable context-packet failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _require_directory(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContextPacketError("unsafe_path", f"{label} must not be a symlink or junction: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContextPacketError("missing_path", f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise ContextPacketError("unsafe_path", f"{label} must be a directory: {path}")
    return path.resolve(strict=True)


def _require_regular_file(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContextPacketError("unsafe_path", f"{label} must not be a symlink or junction: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContextPacketError("missing_path", f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ContextPacketError("unsafe_path", f"{label} must be an ordinary file: {path}")
    return path.resolve(strict=True)


def _safe_direct_child(root: Path, name: str, label: str, *, required: bool = True) -> Path | None:
    candidate = root / name
    if not candidate.exists() and not candidate.is_symlink():
        if required:
            raise ContextPacketError("missing_path", f"{label} does not exist: {candidate}")
        return None
    resolved = _require_regular_file(candidate, label)
    if resolved.parent != root:
        raise ContextPacketError("unsafe_path", f"{label} escaped the temp directory: {candidate}")
    return resolved


def _validate_paths(temp_dir: str | os.PathLike[str], chunk_filename: str):
    match = _CHUNK_RE.fullmatch(chunk_filename) if isinstance(chunk_filename, str) else None
    if match is None:
        raise ContextPacketError(
            "invalid_chunk_name",
            "chunk filename must be canonical 'chunkNNNN.md' with at least four digits and no path",
        )
    number = int(match.group(1))
    if number < 1 or f"{number:04d}" != match.group(1):
        raise ContextPacketError(
            "invalid_chunk_name",
            "chunk filename must use canonical zero padding (for example chunk0001.md)",
        )
    if Path(chunk_filename).name != chunk_filename:
        raise ContextPacketError("invalid_chunk_name", "chunk filename must not contain a path")

    root = _require_directory(Path(temp_dir), "temp_dir")
    chunk_path = _safe_direct_child(root, chunk_filename, "source chunk")
    database_path = _safe_direct_child(root, DATABASE_NAME, "knowledge database")

    # SQLite may consult sidecars. Refuse link tricks there as well.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = root / f"{DATABASE_NAME}{suffix}"
        if sidecar.exists() or sidecar.is_symlink():
            _safe_direct_child(root, sidecar.name, f"SQLite sidecar {sidecar.name}")
    return root, chunk_path, database_path


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri_path = quote(database_path.as_posix(), safe="/:")
    try:
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        return conn
    except sqlite3.Error as exc:
        raise ContextPacketError(
            "database_open_failed", f"cannot open {DATABASE_NAME} read-only: {exc}"
        ) from exc


def _validate_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    present = {row[0] for row in rows}
    problems = []
    for table, required_columns in _REQUIRED_SCHEMA.items():
        if table not in present:
            problems.append(f"missing table {table!r}")
            continue
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        missing = sorted(required_columns - columns)
        if missing:
            problems.append(f"table {table!r} missing columns: {', '.join(missing)}")
    if problems:
        raise ContextPacketError(
            "database_schema_mismatch",
            "; ".join(problems)
            + ". Run the knowledge-store schema migration/reinitialization before building context.",
        )


def _fetch_all(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    try:
        cursor = conn.execute(sql, params)
        rows = []
        text_chars = 0
        while True:
            batch = cursor.fetchmany(512)
            if not batch:
                break
            rows.extend(batch)
            text_chars += sum(
                len(value)
                for row in batch
                for value in row
                if isinstance(value, (str, bytes))
            )
            if len(rows) > MAX_QUERY_ROWS:
                raise ContextPacketError(
                    "database_row_limit",
                    f"a knowledge-store query exceeded {MAX_QUERY_ROWS} rows; compact or shard the store",
                )
            if text_chars > MAX_QUERY_TEXT_CHARS:
                raise ContextPacketError(
                    "database_text_limit",
                    f"a knowledge-store query exceeded {MAX_QUERY_TEXT_CHARS} text characters; compact or shard the store",
                )
    except sqlite3.Error as exc:
        raise ContextPacketError("database_query_failed", f"knowledge-store query failed: {exc}") from exc
    return [dict(row) for row in rows]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContextPacketError("invalid_utf8", f"{label} must be valid UTF-8: {path.name}") from exc
    except OSError as exc:
        raise ContextPacketError("read_failed", f"cannot read {label} {path.name}: {exc}") from exc


def _neighbor(root: Path, number: int, *, tail: bool):
    if number < 1:
        return None
    name = f"chunk{number:04d}.md"
    path = _safe_direct_child(root, name, f"neighbor chunk {name}", required=False)
    if path is None:
        return None
    text = _read_utf8(path, "neighbor chunk")
    excerpt = text[-NEIGHBOR_CHARS:] if tail else text[:NEIGHBOR_CHARS]
    return {"chunk_id": name[:-3], "filename": name, "excerpt": excerpt}


def tokenize(text: str) -> list[str]:
    """Return locale-neutral ASCII tokens plus overlapping CJK bigrams."""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    tokens = [f"a:{token}" for token in _ASCII_TOKEN_RE.findall(normalized)]
    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == 1:
            tokens.append(f"c:{run}")
        else:
            tokens.extend(f"c:{run[index:index + 2]}" for index in range(len(run) - 1))
    return tokens


def bm25_rank(query: str, records: list[dict], text_key) -> list[tuple[float, dict]]:
    """Rank records with an offline BM25 implementation."""
    query_terms = Counter(tokenize(query))
    if not query_terms or not records:
        return []
    documents = [tokenize(text_key(record)) for record in records]
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(set(document))
    average_length = sum(len(document) for document in documents) / max(len(documents), 1)
    average_length = max(average_length, 1.0)
    count = len(documents)
    ranked = []
    for record, document in zip(records, documents):
        frequencies = Counter(document)
        length = len(document)
        score = 0.0
        for term, query_frequency in query_terms.items():
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_document_frequency = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * length / average_length)
            score += inverse_document_frequency * (frequency * 2.5 / denominator) * query_frequency
        if score > 0:
            ranked.append((score, record))
    ranked.sort(key=lambda item: (-item[0], str(next(iter(item[1].values()), ""))))
    return ranked


def _active(row: dict) -> bool:
    return str(row.get("status") or "").casefold() not in _INACTIVE_STATUSES


def _parse_json_value(value, *, field: str, empty_default):
    if value is None or value == "":
        return empty_default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContextPacketError(
            "invalid_database_json", f"knowledge-store field {field} contains invalid JSON"
        ) from exc


def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in _fetch_all(conn, "SELECT key, value FROM metadata")
    }


def _profile(metadata: dict[str, str]) -> str:
    for key in ("profile", "translation_profile", "active_profile", "target_profile"):
        value = metadata.get(key)
        if value:
            return value
    return "general"


def _load_segments(conn: sqlite3.Connection, chunk_id: str) -> list[dict]:
    rows = _fetch_all(
        conn,
        "SELECT segment_id, chunk_id, ordinal, source_text, source_hash "
        "FROM segments WHERE chunk_id = ? ORDER BY ordinal, segment_id",
        (chunk_id,),
    )
    for row in rows:
        row["computed_source_hash"] = _sha256_text(row.get("source_text") or "")
        row["source_hash_valid"] = bool(row.get("source_hash")) and (
            row["source_hash"] == row["computed_source_hash"]
        )
    return rows


def _surface_occurs(surface: str, source: str) -> bool:
    if not surface:
        return False
    normalized_surface = unicodedata.normalize("NFKC", surface).casefold()
    normalized_source = unicodedata.normalize("NFKC", source).casefold()
    if _CJK_RUN_RE.search(normalized_surface):
        return normalized_surface in normalized_source
    if re.fullmatch(r"[A-Za-z0-9_]+", normalized_surface):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(normalized_surface)}(?![A-Za-z0-9_])", normalized_source) is not None
    return normalized_surface in normalized_source


def _terms(conn: sqlite3.Connection, local_text: str):
    terms = [row for row in _fetch_all(conn, "SELECT * FROM terms") if _active(row)]
    aliases = defaultdict(list)
    for row in _fetch_all(conn, "SELECT term_id, alias FROM term_aliases"):
        aliases[str(row["term_id"])].append(row.get("alias") or "")

    matches_by_form = defaultdict(set)
    local_terms = []
    semantic_candidates = []
    for row in terms:
        term_id = str(row["term_id"])
        forms = [row.get("surface") or ""] + aliases.get(term_id, [])
        matched = [form for form in forms if _surface_occurs(form, local_text)]
        item = {
            "term_id": row["term_id"],
            "surface": row.get("surface"),
            "aliases": aliases.get(term_id, []),
            "sense": row.get("sense"),
            "target": row.get("target"),
            "category": row.get("category"),
            "domain": row.get("domain"),
            "usage_note": row.get("usage_note"),
            "forbidden": _parse_json_value(
                row.get("forbidden_json"), field=f"terms[{term_id}].forbidden_json", empty_default=[]
            ),
            "authority": row.get("authority"),
            "version": row.get("version"),
            "matched_forms": matched,
            "ambiguous": False,
        }
        if matched:
            local_terms.append(item)
            for form in matched:
                matches_by_form[unicodedata.normalize("NFKC", form).casefold()].add(term_id)
        else:
            semantic_candidates.append(item)

    ambiguities = []
    ambiguous_ids = set()
    by_id = {str(item["term_id"]): item for item in local_terms}
    for normalized_form, term_ids in sorted(matches_by_form.items()):
        if len(term_ids) <= 1:
            continue
        ordered_ids = sorted(term_ids)
        ambiguous_ids.update(ordered_ids)
        ambiguities.append({
            "surface": normalized_form,
            "term_ids": ordered_ids,
            "senses": [by_id[term_id].get("sense") for term_id in ordered_ids],
            "requires_disambiguation": True,
        })
    for item in local_terms:
        item["ambiguous"] = str(item["term_id"]) in ambiguous_ids

    semantic_ranked = bm25_rank(
        local_text,
        semantic_candidates,
        lambda item: " ".join(str(item.get(key) or "") for key in (
            "surface", "sense", "target", "domain", "usage_note"
        )),
    )
    return local_terms, [item for _, item in semantic_ranked[:32]], ambiguities


def _ranked_facts(conn: sqlite3.Connection, query: str) -> list[dict]:
    rows = [row for row in _fetch_all(conn, "SELECT * FROM facts") if _active(row)]
    ranked = bm25_rank(
        query, rows,
        lambda row: " ".join(str(row.get(key) or "") for key in (
            "subject", "predicate", "object_value", "scope"
        )),
    )
    return [dict(row, relevance=round(score, 6)) for score, row in ranked[:MAX_FACTS]]


def _ranked_claims(conn: sqlite3.Connection, query: str) -> list[dict]:
    rows = [row for row in _fetch_all(conn, "SELECT * FROM claims") if _active(row)]
    ranked = bm25_rank(
        query, rows,
        lambda row: " ".join(str(row.get(key) or "") for key in (
            "holder", "proposition", "scope", "target_gloss"
        )),
    )
    return [dict(row, relevance=round(score, 6)) for score, row in ranked[:MAX_CLAIMS]]


def _style_rules(conn: sqlite3.Connection, profile: str, chunk_id: str) -> list[dict]:
    rows = [row for row in _fetch_all(conn, "SELECT * FROM style_rules") if _active(row)]
    result = []
    for row in rows:
        row_profile = str(row.get("profile") or "").casefold()
        scope = str(row.get("scope") or "").casefold()
        if row_profile not in {"", "*", "all", "global", profile.casefold()}:
            continue
        if scope.startswith("chunk") and chunk_id.casefold() not in scope:
            continue
        result.append(row)
    result.sort(key=lambda row: (str(row.get("scope") or ""), str(row.get("rule_id") or "")))
    return result


def _resolved_issues(conn: sqlite3.Connection, query: str, chunk_id: str):
    rows = _fetch_all(conn, "SELECT * FROM unresolved")
    resolved = []
    for row in rows:
        status = str(row.get("status") or "").casefold()
        if status not in _RESOLVED_STATUSES and not str(row.get("resolution") or "").strip():
            continue
        item = dict(row)
        item["options"] = _parse_json_value(
            item.pop("options_json", None),
            field=f"unresolved[{row.get('issue_id')}].options_json",
            empty_default=[],
        )
        resolved.append(item)
    local = [row for row in resolved if row.get("chunk_id") in (None, "", chunk_id)]
    remote = [row for row in resolved if row not in local]
    ranked = bm25_rank(
        query, remote,
        lambda row: " ".join(str(row.get(key) or "") for key in (
            "question", "needed_evidence", "resolution", "issue_type"
        )),
    )
    return local, [row for _, row in ranked[:24]]


def _open_issues(conn: sqlite3.Connection, query: str, chunk_id: str):
    rows = _fetch_all(conn, "SELECT * FROM unresolved")
    open_items = []
    for row in rows:
        status = str(row.get("status") or "").casefold()
        if status in _RESOLVED_STATUSES or str(row.get("resolution") or "").strip():
            continue
        item = dict(row)
        item["options"] = _parse_json_value(
            item.pop("options_json", None),
            field=f"unresolved[{row.get('issue_id')}].options_json",
            empty_default=[],
        )
        open_items.append(item)
    local = [row for row in open_items if row.get("chunk_id") in (None, "", chunk_id)]
    remote = [row for row in open_items if row not in local]
    ranked = bm25_rank(
        query,
        remote,
        lambda row: " ".join(
            str(row.get(key) or "")
            for key in ("question", "needed_evidence", "issue_type")
        ),
    )
    return local, [row for _, row in ranked[:24]]


def _dependency(conn: sqlite3.Connection, chunk_id: str) -> dict | None:
    rows = _fetch_all(conn, "SELECT * FROM chunk_dependencies WHERE chunk_id = ?", (chunk_id,))
    if not rows:
        return None
    if len(rows) > 1:
        raise ContextPacketError(
            "database_integrity_error", f"multiple chunk_dependencies rows exist for {chunk_id}"
        )
    row = rows[0]
    row["memory_ids"] = _parse_json_value(
        row.pop("memory_ids_json", None),
        field=f"chunk_dependencies[{chunk_id}].memory_ids_json",
        empty_default=[],
    )
    return row


def _translation_memory(
    conn: sqlite3.Connection,
    current_segments: list[dict],
    profile: str,
    target_lang: str,
    dependency_hash: str | None,
    dependency_clean: bool,
):
    candidates = _fetch_all(
        conn,
        "SELECT t.segment_id, t.target_text, t.target_lang, t.profile, t.context_hash, "
        "t.status, t.version, s.chunk_id, s.ordinal, s.source_text, s.source_hash "
        "FROM translations AS t JOIN segments AS s ON s.segment_id = t.segment_id "
        "WHERE COALESCE(t.target_text, '') <> ''",
    )
    candidates = [candidate for candidate in candidates if _active(candidate)]
    current_by_hash = defaultdict(list)
    current_segment_ids = {
        str(segment.get("segment_id")) for segment in current_segments
    }
    for segment in current_segments:
        current_by_hash[segment["computed_source_hash"]].append(segment)

    exact = []
    fuzzy_pool = []
    for candidate in candidates:
        source_text = candidate.get("source_text") or ""
        computed_hash = _sha256_text(source_text)
        stored_hash = candidate.get("source_hash") or ""
        if computed_hash in current_by_hash:
            current_hash_ok = any(
                segment.get("source_hash_valid") for segment in current_by_hash[computed_hash]
            )
            source_hash_ok = bool(stored_hash) and stored_hash == computed_hash and current_hash_ok
            profile_ok = bool(profile) and candidate.get("profile") == profile
            target_lang_ok = bool(target_lang) and candidate.get("target_lang") == target_lang
            context_ok = bool(dependency_hash) and candidate.get("context_hash") == dependency_hash
            # Cross-segment reuse needs a positively identified speaker.  The
            # current schema cannot prove that for an arbitrary duplicate, so
            # only the same canonical segment is automatically reusable;
            # other exact duplicates remain visible as non-reusable evidence.
            speaker_context_ok = str(candidate.get("segment_id")) in current_segment_ids
            reusable = (
                source_hash_ok
                and profile_ok
                and target_lang_ok
                and context_ok
                and dependency_clean
                and speaker_context_ok
            )
            exact.append({
                "segment_id": candidate.get("segment_id"),
                "matches_current_segment_ids": [
                    segment["segment_id"] for segment in current_by_hash[computed_hash]
                ],
                "source_hash": stored_hash,
                "target_text": candidate.get("target_text"),
                "target_lang": candidate.get("target_lang"),
                "profile": candidate.get("profile"),
                "context_hash": candidate.get("context_hash"),
                "status": candidate.get("status"),
                "version": candidate.get("version"),
                "checks": {
                    "source_hash_matches": source_hash_ok,
                    "current_source_hash_valid": current_hash_ok,
                    "profile_matches": profile_ok,
                    "target_lang_matches": target_lang_ok,
                    "context_matches_dependency_hash": context_ok,
                    "dependency_clean": dependency_clean,
                    "speaker_context_known": speaker_context_ok,
                },
                "reusable": reusable,
            })
        else:
            candidate["computed_source_hash"] = computed_hash
            fuzzy_pool.append(candidate)
    exact.sort(key=lambda item: (not item["reusable"], str(item["segment_id"])))

    fuzzy_query = "\n".join(segment.get("source_text") or "" for segment in current_segments)
    fuzzy_ranked = bm25_rank(fuzzy_query, fuzzy_pool, lambda row: row.get("source_text") or "")
    fuzzy = []
    for score, candidate in fuzzy_ranked[:12]:
        fuzzy.append({
            "segment_id": candidate.get("segment_id"),
            "source_text": candidate.get("source_text"),
            "source_hash": candidate.get("source_hash"),
            "target_text": candidate.get("target_text"),
            "target_lang": candidate.get("target_lang"),
            "profile": candidate.get("profile"),
            "context_hash": candidate.get("context_hash"),
            "similarity_score": round(score, 6),
            "kind": "suggestion",
            "reusable": False,
        })
    return exact, fuzzy


def _remote_evidence(
    conn: sqlite3.Connection,
    query: str,
    excluded_chunks: set[str],
    selected_item_ids: set[tuple[str, str]],
) -> list[dict]:
    rows = _fetch_all(conn, "SELECT * FROM evidence")
    remote = []
    for row in rows:
        if str(row.get("chunk_id") or "") in excluded_chunks:
            continue
        item = dict(row)
        item["quote"] = str(item.get("quote") or "")[:MAX_EVIDENCE_QUOTE_CHARS]
        remote.append(item)
    ranked = bm25_rank(query, remote, lambda row: row.get("quote") or "")
    scores = {str(row.get("evidence_id")): score for score, row in ranked}
    for row in remote:
        key = (str(row.get("item_kind") or "").casefold(), str(row.get("item_id") or ""))
        if key in selected_item_ids:
            scores[str(row.get("evidence_id"))] = scores.get(str(row.get("evidence_id")), 0.0) + 2.0
    remote.sort(key=lambda row: (-scores.get(str(row.get("evidence_id")), 0.0), str(row.get("evidence_id"))))
    return [
        dict(row, relevance=round(scores.get(str(row.get("evidence_id")), 0.0), 6))
        for row in remote
        if scores.get(str(row.get("evidence_id")), 0.0) > 0
    ][:MAX_REMOTE_EVIDENCE]


def safe_json_dumps(value, *, sort_keys=True) -> str:
    """Serialize data so Markdown fences/tables cannot escape the data layer."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=sort_keys,
        allow_nan=False,
    )
    return (
        encoded.replace("`", "\\u0060")
        .replace("|", "\\u007c")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _json_length(value) -> int:
    return len(safe_json_dumps(value))


def _required_budget_check(packet: dict, memory_limit: int) -> None:
    used = _json_length(packet)
    if used > memory_limit:
        raise ContextPacketError(
            "required_context_overflow",
            f"required local/blocking context needs {used} characters, above the {memory_limit}-character limit; "
            "reduce the chunk size or resolve/shorten blocking memory explicitly",
        )


def _fit_list(packet: dict, key: str, candidates: list[dict], memory_limit: int) -> int:
    added = 0
    destination = packet[key]
    for candidate in candidates:
        destination.append(candidate)
        if _json_length(packet) > memory_limit:
            destination.pop()
            continue
        added += 1
    return len(candidates) - added


def _fit_nested_list(packet: dict, parent: str, key: str, candidates: list[dict], memory_limit: int) -> int:
    added = 0
    destination = packet[parent][key]
    for candidate in candidates:
        destination.append(candidate)
        if _json_length(packet) > memory_limit:
            destination.pop()
            continue
        added += 1
    return len(candidates) - added


def _finalize_budget(packet: dict, memory_limit: int, dropped: dict[str, int]) -> None:
    packet["budget"]["dropped_optional_items"] = {
        key: value for key, value in dropped.items() if value
    }
    # Stabilize the self-referential serialized length field.
    for _ in range(5):
        packet["budget"]["used_chars"] = _json_length(packet)
    if _json_length(packet) > memory_limit:
        # Budget metadata is required; optional data was already fitted with it
        # present, so this indicates a programming or unusually-long-key error.
        raise ContextPacketError(
            "context_budget_overflow",
            f"context packet exceeds the {memory_limit}-character limit after budgeting",
        )


def build_context_packet(
    temp_dir: str | os.PathLike[str],
    chunk_filename: str,
    phase: str = "translate",
    *,
    memory_limit: int = MAX_CONTEXT_CHARS,
) -> dict:
    """Return one strict, bounded context-packet object."""
    if phase not in PHASES:
        raise ContextPacketError("invalid_phase", f"phase must be one of: {', '.join(PHASES)}")
    if not isinstance(memory_limit, int) or memory_limit < 1:
        raise ContextPacketError("invalid_budget", "memory_limit must be a positive integer")

    root, chunk_path, database_path = _validate_paths(temp_dir, chunk_filename)
    match = _CHUNK_RE.fullmatch(chunk_filename)
    number = int(match.group(1))
    chunk_id = chunk_filename[:-3]
    source_text = _read_utf8(chunk_path, "source chunk")
    review_target = None
    if phase == "review":
        output_name = f"output_{chunk_filename}"
        output_path = _safe_direct_child(root, output_name, "review target")
        # Validate text encoding even though the translation itself is supplied
        # to the isolated reviewer separately.  The packet binds that exact
        # ordinary file by digest without duplicating it into long-term memory.
        _read_utf8(output_path, "review target")
        review_target = {
            "filename": output_name,
            "output_hash": _sha256_file(output_path),
        }
    previous = _neighbor(root, number - 1, tail=True)
    following = _neighbor(root, number + 1, tail=False)
    local_query = "\n".join(
        part for part in (
            previous["excerpt"] if previous else "",
            source_text,
            following["excerpt"] if following else "",
        ) if part
    )

    with closing(_connect_read_only(database_path)) as conn:
        _validate_schema(conn)
        metadata = _metadata(conn)
        profile = _profile(metadata)
        segments = _load_segments(conn, chunk_id)
        dependency = _dependency(conn, chunk_id)
        if metadata.get("database_filename") == DATABASE_NAME:
            # The authoritative store can derive the current dependency from
            # content.  Do not trust a stale worker-supplied hash merely
            # because it was persisted in chunk_dependencies.
            import knowledge_store

            computed = knowledge_store.compute_dependency_bundle(conn, chunk_id)
            if dependency is None:
                dependency = {
                    "chunk_id": chunk_id,
                    "reviewed_hash": "",
                    "dirty": 1,
                    "revision_count": 0,
                }
            stored_hash = dependency.get("dependency_hash")
            dependency["stored_dependency_hash"] = stored_hash
            dependency["dependency_hash"] = computed["dependency_hash"]
            dependency["memory_ids"] = computed["memory_ids"]
            if stored_hash != computed["dependency_hash"]:
                dependency["dirty"] = 1
        dependency_hash = dependency.get("dependency_hash") if dependency else None
        dependency_clean = bool(dependency) and dependency.get("dirty") in (0, "0", False)
        local_terms, semantic_terms, ambiguities = _terms(conn, local_query)
        facts = _ranked_facts(conn, local_query)
        claims = _ranked_claims(conn, local_query)
        style_rules = _style_rules(conn, profile, chunk_id)
        local_resolved, remote_resolved = _resolved_issues(conn, local_query, chunk_id)
        local_open, remote_open = _open_issues(conn, local_query, chunk_id)
        target_lang = metadata.get("target_lang") or metadata.get("output_lang") or ""
        exact_tm, fuzzy_tm = _translation_memory(
            conn, segments, profile, target_lang, dependency_hash, dependency_clean
        )
        selected_ids = {
            *(('fact', str(item['fact_id'])) for item in facts),
            *(('claim', str(item['claim_id'])) for item in claims),
        }
        excluded_chunks = {chunk_id}
        if previous:
            excluded_chunks.add(previous["chunk_id"])
        if following:
            excluded_chunks.add(following["chunk_id"])
        evidence = _remote_evidence(conn, local_query, excluded_chunks, selected_ids)

    required_exact_tm = [item for item in exact_tm if item["reusable"]]
    optional_exact_tm = [item for item in exact_tm if not item["reusable"]]
    required_resolved = [
        item for item in local_resolved
        if str(item.get("impact") or "").casefold() in {"critical", "high", "blocking"}
    ]
    optional_local_resolved = [item for item in local_resolved if item not in required_resolved]
    required_open = [
        item for item in local_open
        if str(item.get("impact") or "").casefold() in {"critical", "high", "blocking"}
    ]
    optional_local_open = [item for item in local_open if item not in required_open]

    public_segments = []
    offset = 0
    for segment in segments:
        if not segment.get("source_hash_valid"):
            raise ContextPacketError(
                "source_hash_mismatch",
                f"canonical segment hash is stale for {segment.get('segment_id')}; "
                "reinitialize the knowledge store from the validated source chunks",
            )
        segment_text = str(segment.get("source_text") or "")
        start = source_text.find(segment_text, offset)
        if start < 0:
            raise ContextPacketError(
                "segment_alignment_error",
                f"canonical segment {segment.get('segment_id')} no longer aligns with {chunk_id}.md",
            )
        end = start + len(segment_text)
        public_segments.append(
            {
                "segment_id": segment.get("segment_id"),
                "ordinal": segment.get("ordinal"),
                "start": start,
                "end": end,
            }
        )
        offset = end
    budget_sections = (
        "facts", "claims", "remote_evidence", "semantic_terms",
        "resolved_issues", "unresolved_issues", "exact_nonreusable", "fuzzy_suggestions",
    )
    packet = {
        "schema_version": SCHEMA_VERSION,
        "security_label": SECURITY_LABEL,
        "phase": phase,
        "profile": profile,
        "target_lang": target_lang,
        "source": {
            "chunk_id": chunk_id,
            "filename": chunk_filename,
            "source_hash": _sha256_file(chunk_path),
            "text": source_text,
            "segments": public_segments,
        },
        **({"review_target": review_target} if review_target else {}),
        "neighbors": {"previous": previous, "next": following},
        "dependency": dependency,
        "dependency_hash": dependency_hash,
        # Exact local surface/alias matches are required and never silently cut.
        "terms": local_terms,
        "ambiguities": ambiguities,
        # Applicable style rules and blocking local resolutions are constraints.
        "style_rules": style_rules,
        "facts": [],
        "claims": [],
        "remote_evidence": [],
        "resolved_issues": required_resolved,
        # Blocking local ambiguity is mandatory context.  It remains data,
        # never an instruction, and tells the worker to retain a readable
        # provisional translation rather than silently choosing an answer.
        "unresolved_issues": required_open,
        "translation_memory": {
            "exact": required_exact_tm,
            "fuzzy_suggestions": [],
        },
        "budget": {
            "max_chars": memory_limit,
            # Reserve final bookkeeping before fitting optional data.
            "used_chars": memory_limit,
            "measurement": "serialized_json_characters",
            "dropped_optional_items": {key: MAX_QUERY_ROWS for key in budget_sections},
        },
    }
    _required_budget_check(packet, memory_limit)

    dropped = {}
    # Phase affects priority, never the safety/reusability rules.
    if phase == "analyze":
        sections = (("facts", facts), ("claims", claims), ("remote_evidence", evidence))
    else:
        sections = (("claims", claims), ("facts", facts), ("remote_evidence", evidence))
    for key, candidates in sections:
        dropped[key] = _fit_list(packet, key, candidates, memory_limit)
    dropped["semantic_terms"] = _fit_list(packet, "terms", semantic_terms, memory_limit)
    dropped["resolved_issues"] = _fit_list(
        packet, "resolved_issues", optional_local_resolved + remote_resolved, memory_limit
    )
    dropped["unresolved_issues"] = _fit_list(
        packet, "unresolved_issues", optional_local_open + remote_open, memory_limit
    )
    dropped["exact_nonreusable"] = _fit_nested_list(
        packet, "translation_memory", "exact", optional_exact_tm, memory_limit
    )
    dropped["fuzzy_suggestions"] = _fit_nested_list(
        packet, "translation_memory", "fuzzy_suggestions", fuzzy_tm, memory_limit
    )
    _finalize_budget(packet, memory_limit, dropped)
    return packet


def _error_document(error: ContextPacketError) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "security_label": SECURITY_LABEL,
        "error": {"code": error.code, "message": error.message},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a bounded long-range translation context packet")
    parser.add_argument("temp_dir", help="Path to the book temp directory")
    parser.add_argument("chunk_file", help="Canonical filename such as chunk0001.md")
    parser.add_argument("--phase", choices=PHASES, required=True)
    args = parser.parse_args(argv)
    try:
        document = build_context_packet(args.temp_dir, args.chunk_file, args.phase)
    except ContextPacketError as error:
        print(safe_json_dumps(_error_document(error)))
        return 1
    except Exception:
        error = ContextPacketError(
            "internal_error",
            "context packet generation failed unexpectedly; validate the knowledge store and retry",
        )
        print(safe_json_dumps(_error_document(error)))
        return 1
    print(safe_json_dumps(document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
