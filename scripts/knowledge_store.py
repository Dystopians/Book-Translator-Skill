#!/usr/bin/env python3
"""Durable, evidence-backed translation memory for translate-book.

The store is deliberately stdlib-only.  It treats source ``chunk*.md`` files
as canonical, derives every authoritative object identifier with SHA-256, and
never edits the legacy ``glossary.json`` or ``run_state.json`` inputs.

Analysis sidecars use this strict v1 envelope (chunk identity comes from the
filename, never from model-controlled payload data)::

    {
      "schema_version": 1,
      "terms": [],
      "facts": [],
      "claims": [],
      "style_observations": [],
      "unresolved": []
    }

Every observation contains ``evidence`` with exactly ``segment_id`` and
``quote``.  The quote must be an exact substring of that canonical segment.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import meta as meta_mod


DATABASE_FILENAME = "translation_state.sqlite3"
SCHEMA_VERSION = 1
ANALYSIS_SCHEMA_VERSION = 1
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_OUTPUT_CHUNK_BYTES = 16 * 1024 * 1024
MAX_DECISIONS_BYTES = 1024 * 1024
MAX_ARRAY_ITEMS = 100
MAX_QUOTE_CHARS = 500
MAX_DEPENDENCY_TERMS = 80
MAX_DEPENDENCY_FACTS = 24
MAX_DEPENDENCY_CLAIMS = 12
MAX_DEPENDENCY_EVIDENCE = 8
MAX_DEPENDENCY_RESOLUTIONS = 24
VALID_POLARITIES = frozenset({"affirmed", "negated", "mixed", "unknown"})
VALID_MODALITIES = frozenset(
    {"certain", "necessary", "probable", "possible", "conditional", "reported", "unknown"}
)
VALID_IMPACTS = frozenset({"critical", "high", "medium", "low"})
VALID_EVIDENCE_BASES = frozenset(
    {"explicit_definition", "book_usage", "trusted_user_source", "model_inference"}
)
MEMORY_KINDS = ("terms", "facts", "claims", "style_rules", "resolutions")
BUILTIN_PROFILES = frozenset({"general", "academic-technical", "legal", "literary"})

CHUNK_RE = re.compile(r"^chunk\d+$")
CHUNK_FILE_RE = re.compile(r"^(chunk\d+)\.md$")
ANALYSIS_FILE_RE = re.compile(r"^analysis_(chunk\d+)\.json$")
TRANSLATION_META_FILE_RE = re.compile(r"^output_(chunk\d+)\.meta\.json$")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]+"
)

ANALYSIS_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "chunk_id",  # optional compatibility field; filename remains authoritative
        "terms",
        "facts",
        "claims",
        "style_observations",
        "unresolved",
    }
)

TABLES = (
    "metadata",
    "segments",
    "terms",
    "aliases",
    "term_aliases",
    "entities",
    "facts",
    "claims",
    "style_rules",
    "unresolved",
    "translations",
    "chunk_dependencies",
    "reviews",
    "evidence",
    "decisions",
    "external_sources",
    "audit_log",
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL CHECK (chunk_order >= 1),
    segment_order INTEGER NOT NULL CHECK (segment_order >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    source_text TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    UNIQUE (chunk_id, segment_order),
    UNIQUE (chunk_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_segments_chunk ON segments(chunk_id, segment_order);

CREATE TABLE IF NOT EXISTS terms (
    term_id TEXT PRIMARY KEY,
    surface TEXT NOT NULL,
    sense TEXT NOT NULL DEFAULT 'default',
    target TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    usage_note TEXT NOT NULL DEFAULT '',
    forbidden_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'medium',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    authority TEXT NOT NULL DEFAULT 'model_observation',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (surface, sense)
);
CREATE INDEX IF NOT EXISTS idx_terms_surface ON terms(surface);

CREATE TABLE IF NOT EXISTS aliases (
    alias_id TEXT PRIMARY KEY,
    term_id TEXT NOT NULL REFERENCES terms(term_id) ON DELETE CASCADE,
    surface TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (term_id, surface)
);
CREATE INDEX IF NOT EXISTS idx_aliases_surface ON aliases(surface);

CREATE TABLE IF NOT EXISTS term_aliases (
    term_id TEXT NOT NULL REFERENCES terms(term_id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    PRIMARY KEY (term_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_term_aliases_alias ON term_aliases(alias);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    canonical_term_id TEXT REFERENCES terms(term_id) ON DELETE SET NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    object_value TEXT NOT NULL,
    polarity TEXT NOT NULL DEFAULT 'affirmed',
    modality TEXT NOT NULL DEFAULT 'certain',
    scope TEXT NOT NULL DEFAULT 'book',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'active',
    authority TEXT NOT NULL DEFAULT 'model_observation',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_chunk_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    claim TEXT NOT NULL,
    holder TEXT NOT NULL DEFAULT '',
    proposition TEXT NOT NULL,
    polarity TEXT NOT NULL DEFAULT 'affirmed',
    modality TEXT NOT NULL DEFAULT 'certain',
    scope TEXT NOT NULL DEFAULT 'book',
    target_gloss TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'active',
    authority TEXT NOT NULL DEFAULT 'model_observation',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_chunk_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS style_rules (
    style_rule_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL UNIQUE,
    rule TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'book',
    profile TEXT NOT NULL DEFAULT 'general',
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    authority TEXT NOT NULL DEFAULT 'model_observation',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_chunk_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unresolved (
    issue_id TEXT PRIMARY KEY,
    issue_type TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    segment_id TEXT NOT NULL DEFAULT '',
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    needed_evidence TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT 'medium',
    existing_json TEXT NOT NULL DEFAULT '{}',
    proposed_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT NOT NULL DEFAULT '',
    resolution_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unresolved_status ON unresolved(status, chunk_id);

CREATE TABLE IF NOT EXISTS translations (
    segment_id TEXT PRIMARY KEY REFERENCES segments(segment_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    target_text TEXT NOT NULL DEFAULT '',
    target_lang TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT 'general',
    context_hash TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL DEFAULT '',
    dependency_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translations_chunk ON translations(chunk_id, dirty);

CREATE TABLE IF NOT EXISTS chunk_dependencies (
    chunk_id TEXT PRIMARY KEY,
    dependency_hash TEXT NOT NULL,
    memory_ids_json TEXT NOT NULL DEFAULT '{}',
    reviewed_hash TEXT NOT NULL DEFAULT '',
    dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
    revision_count INTEGER NOT NULL DEFAULT 0 CHECK (revision_count >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    dependency_hash TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_chunk ON reviews(chunk_id, status);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    segment_id TEXT NOT NULL REFERENCES segments(segment_id) ON DELETE RESTRICT,
    chunk_id TEXT NOT NULL,
    quote TEXT NOT NULL CHECK (length(quote) <= 500),
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (owner_type, owner_id, segment_id, quote)
);
CREATE INDEX IF NOT EXISTS idx_evidence_owner ON evidence(owner_type, owner_id);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES unresolved(issue_id) ON DELETE RESTRICT,
    resolution TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    authorized_by_user INTEGER NOT NULL CHECK (authorized_by_user = 1),
    created_at TEXT NOT NULL,
    UNIQUE (url, content_hash)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DuplicateJSONKey(ValueError):
    """Raised when strict JSON contains the same object key twice."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_id(kind: str, *parts: Any) -> str:
    payload = _canonical_json([kind, *parts]).encode("utf-8")
    return f"{kind}_" + hashlib.sha256(payload).hexdigest()


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _loads_strict_json(raw: bytes, path: Path) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: file is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, DuplicateJSONKey, ValueError) as exc:
        raise ValueError(f"{path}: invalid strict JSON: {exc}") from exc


def _root_path(temp_dir: os.PathLike[str] | str) -> Path:
    supplied = Path(temp_dir)
    if supplied.is_symlink():
        raise ValueError(f"temp directory may not be a symbolic link: {supplied}")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"temp directory does not exist: {supplied}") from exc
    if not root.is_dir():
        raise ValueError(f"temp directory is not a directory: {root}")
    return root


def _is_confined(root: Path, path: Path) -> bool:
    try:
        root_norm = os.path.normcase(str(root.resolve(strict=True)))
        path_norm = os.path.normcase(str(path.resolve(strict=True)))
        return os.path.commonpath([root_norm, path_norm]) == root_norm
    except (OSError, ValueError):
        return False


def _resolve_input_file(
    root: Path,
    supplied: os.PathLike[str] | str,
    *,
    max_bytes: int,
    label: str,
) -> Path:
    path = Path(supplied)
    if not path.is_absolute() and path.parent == Path("."):
        path = root / path
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symbolic link: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not _is_confined(root, resolved):
        raise ValueError(f"{label} escapes temp directory: {path}")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a regular file: {resolved}")
    if info.st_size > max_bytes:
        raise ValueError(
            f"{label} exceeds {max_bytes} bytes: {resolved} ({info.st_size} bytes)"
        )
    return resolved


def _read_limited_file(path: Path, max_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open {label} {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} is not a regular file: {path}")
        if info.st_size > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
        return raw
    finally:
        os.close(fd)


def _read_json_file(path: Path, max_bytes: int, label: str) -> Any:
    return _loads_strict_json(_read_limited_file(path, max_bytes, label), path)


def _database_path(root: Path) -> Path:
    return root / DATABASE_FILENAME


def _configure_connection(connection: sqlite3.Connection, *, readonly: bool) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    else:
        # DELETE mode keeps the authoritative database a single replaceable file.
        connection.execute("PRAGMA journal_mode = DELETE")


def open_database(
    temp_dir: os.PathLike[str] | str,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Open an initialized store with safety PRAGMAs enabled.

    Writable callers must use :func:`write_transaction`; it obtains SQLite's
    single-writer reservation with ``BEGIN IMMEDIATE``.
    """

    root = _root_path(temp_dir)
    path = _database_path(root)
    if path.is_symlink():
        raise ValueError(f"database may not be a symbolic link: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"knowledge database not initialized: {path}")
    if readonly:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    else:
        connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    _configure_connection(connection, readonly=readonly)
    return connection


@contextlib.contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run one atomic, single-writer transaction."""

    if connection.in_transaction:
        raise RuntimeError("nested knowledge-store write transactions are not allowed")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


def _encode_meta(value: Any) -> str:
    # Context-packet consumers intentionally read metadata as plain strings.
    # Numeric/structured values remain canonical JSON and decode cleanly here.
    return value if isinstance(value, str) else _canonical_json(value)


def _decode_meta(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _set_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _encode_meta(value)),
    )


@contextlib.contextmanager
def _borrow_connection(
    source: sqlite3.Connection | os.PathLike[str] | str,
) -> Iterator[sqlite3.Connection]:
    if isinstance(source, sqlite3.Connection):
        yield source
        return
    connection = open_database(source, readonly=True)
    try:
        yield connection
    finally:
        connection.close()


def get_metadata(
    source: sqlite3.Connection | os.PathLike[str] | str,
    key: str | None = None,
) -> Any:
    """Return decoded metadata, or a single decoded value when ``key`` is set."""

    with _borrow_connection(source) as connection:
        if key is not None:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else _decode_meta(row["value"])
        rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
        return {row["key"]: _decode_meta(row["value"]) for row in rows}


def memory_version(source: sqlite3.Connection | os.PathLike[str] | str) -> int:
    value = get_metadata(source, "memory_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid memory_version metadata: {value!r}")
    return value


def _bump_memory_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", ("memory_version",)
    ).fetchone()
    current = 0 if row is None else _decode_meta(row["value"])
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise ValueError(f"invalid memory_version metadata: {current!r}")
    new_value = current + 1
    _set_metadata(connection, "memory_version", new_value)
    return new_value


def _audit(
    connection: sqlite3.Connection,
    action: str,
    object_type: str,
    object_id: str,
    payload: Any,
) -> None:
    connection.execute(
        "INSERT INTO audit_log(action, object_type, object_id, payload_json, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (action, object_type, object_id, _canonical_json(payload), _utc_now()),
    )


def _paragraphs(text: str) -> list[str]:
    # Normalize platform newlines once; evidence thereafter compares byte-for-byte
    # in Unicode space against the stored canonical paragraph.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip(" \t\r\n") == "":
            if current:
                paragraph = "".join(current).rstrip("\n")
                if paragraph:
                    result.append(paragraph)
                current = []
        else:
            current.append(line)
    if current:
        paragraph = "".join(current).rstrip("\n")
        if paragraph:
            result.append(paragraph)
    return result


def _segment_id(chunk_id: str, source_text: str, occurrence: int) -> str:
    # Occurrence is among identical paragraphs, so inserting/reordering unrelated
    # paragraphs does not churn their IDs.
    return _hash_id("seg", chunk_id, source_text, occurrence)


def _discover_segments(root: Path) -> list[dict[str, Any]]:
    chunk_paths: list[tuple[int, str, Path]] = []
    for candidate in root.iterdir():
        match = CHUNK_FILE_RE.fullmatch(candidate.name)
        if not match:
            continue
        if candidate.is_symlink():
            raise ValueError(f"canonical chunk may not be a symbolic link: {candidate}")
        if not candidate.is_file() or not _is_confined(root, candidate):
            raise ValueError(f"canonical chunk is not a confined regular file: {candidate}")
        chunk_id = match.group(1)
        numeric = int(chunk_id[len("chunk") :])
        chunk_paths.append((numeric, chunk_id, candidate))
    chunk_paths.sort(key=lambda item: (item[0], item[1]))
    if not chunk_paths:
        raise ValueError(f"no canonical chunk*.md files found in {root}")

    records: list[dict[str, Any]] = []
    for chunk_order, (_, chunk_id, path) in enumerate(chunk_paths, 1):
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read canonical UTF-8 chunk {path}: {exc}") from exc
        seen_text: dict[str, int] = {}
        for segment_order, paragraph in enumerate(_paragraphs(raw_text), 1):
            occurrence = seen_text.get(paragraph, 0) + 1
            seen_text[paragraph] = occurrence
            records.append(
                {
                    "segment_id": _segment_id(chunk_id, paragraph, occurrence),
                    "chunk_id": chunk_id,
                    "chunk_order": chunk_order,
                    "segment_order": segment_order,
                    "source_text": paragraph,
                    "source_hash": _text_hash(paragraph),
                }
            )
    if not records:
        raise ValueError(f"canonical chunks in {root} contain no non-blank paragraphs")
    return records


def _load_optional_legacy_json(root: Path, filename: str, max_bytes: int) -> Any | None:
    path = root / filename
    if not path.exists():
        return None
    safe = _resolve_input_file(root, path, max_bytes=max_bytes, label=filename)
    return _read_json_file(safe, max_bytes, filename)


def _load_conversion_metadata(root: Path) -> dict[str, str]:
    """Read bounded scalar settings from convert.py's legacy config.txt."""

    path = root / "config.txt"
    if not path.exists():
        return {}
    safe = _resolve_input_file(root, path, max_bytes=1024 * 1024, label="config.txt")
    raw = _read_limited_file(safe, 1024 * 1024, "config.txt")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"config.txt is not valid UTF-8: {exc}") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"input_lang", "output_lang"}:
            value = value.strip()
            if len(value) > 128 or "\x00" in value:
                raise ValueError(f"config.txt field {key!r} is invalid")
            result[key] = value
    return result


def _insert_segments(connection: sqlite3.Connection, segments: Sequence[Mapping[str, Any]]) -> None:
    for segment in segments:
        connection.execute(
            "INSERT INTO segments(segment_id, chunk_id, chunk_order, segment_order, ordinal, "
            "source_text, source_hash) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                segment["segment_id"],
                segment["chunk_id"],
                segment["chunk_order"],
                segment["segment_order"],
                segment["segment_order"],
                segment["source_text"],
                segment["source_hash"],
            ),
        )


def _legacy_glossary_terms(data: Any, path: str) -> list[dict[str, Any]]:
    if data is None:
        return []
    if not isinstance(data, dict) or data.get("version") != 2:
        raise ValueError(f"{path}: only glossary schema v2 can be imported")
    terms = data.get("terms")
    if not isinstance(terms, list):
        raise ValueError(f"{path}: 'terms' must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(terms):
        where = f"{path}: terms[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be an object")
        surface = item.get("source")
        target = item.get("target")
        if not isinstance(surface, str) or not surface:
            raise ValueError(f"{where}.source must be a non-empty string")
        if not isinstance(target, str):
            raise ValueError(f"{where}.target must be a string")
        category = item.get("category", "")
        if not isinstance(category, str):
            raise ValueError(f"{where}.category must be a string")
        legacy_id = item.get("id", surface)
        if not isinstance(legacy_id, str) or not legacy_id:
            raise ValueError(f"{where}.id must be a non-empty string")
        # Prefer an explicit future-compatible sense; category separates the
        # common polysemy case, and a non-surface legacy id is the final hint.
        sense = item.get("sense")
        if sense is None:
            sense = category or (legacy_id if legacy_id != surface else "default")
        if not isinstance(sense, str) or not sense:
            raise ValueError(f"{where}.sense must be a non-empty string")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias for alias in aliases
        ):
            raise ValueError(f"{where}.aliases must contain non-empty strings")
        result.append(
            {
                "legacy_id": legacy_id,
                "term_id": _hash_id("term", surface, sense),
                "surface": surface,
                "sense": sense,
                "target": target,
                "category": category,
                "confidence": item.get("confidence", "medium"),
                "notes": item.get("notes", ""),
                "aliases": aliases,
            }
        )
    return result


def _insert_legacy_terms(
    connection: sqlite3.Connection, terms: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    now = _utc_now()
    legacy_map: dict[str, str] = {}
    for term in terms:
        existing = connection.execute(
            "SELECT * FROM terms WHERE surface = ? AND sense = ?",
            (term["surface"], term["sense"]),
        ).fetchone()
        if existing is not None and existing["target"] != term["target"]:
            _insert_unresolved(
                connection,
                issue_type="legacy_term_conflict",
                chunk_id="chunk0",
                item_type="term",
                item_key=existing["term_id"],
                summary=f"Conflicting targets for {term['surface']!r}/{term['sense']!r}",
                existing=dict(existing),
                proposed=dict(term),
                evidence={"segment_id": "", "quote": ""},
                attach_evidence=False,
            )
            continue
        connection.execute(
            "INSERT OR IGNORE INTO terms(term_id, surface, sense, target, category, domain, "
            "usage_note, forbidden_json, confidence, notes, status, authority, version, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 'active', "
            "'legacy_glossary', 1, ?, ?)",
            (
                term["term_id"],
                term["surface"],
                term["sense"],
                term["target"],
                term["category"],
                term["category"],
                term["notes"] if isinstance(term["notes"], str) else "",
                term["confidence"] if isinstance(term["confidence"], str) else "medium",
                term["notes"] if isinstance(term["notes"], str) else "",
                now,
                now,
            ),
        )
        legacy_map[str(term["legacy_id"])] = str(term["term_id"])
        for alias in term["aliases"]:
            connection.execute(
                "INSERT OR IGNORE INTO aliases(alias_id, term_id, surface, created_at) "
                "VALUES(?, ?, ?, ?)",
                (_hash_id("alias", term["term_id"], alias), term["term_id"], alias, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO term_aliases(term_id, alias) VALUES(?, ?)",
                (term["term_id"], alias),
            )
        if term["category"]:
            entity_id = _hash_id("entity", term["term_id"])
            connection.execute(
                "INSERT OR IGNORE INTO entities(entity_id, name, entity_type, "
                "canonical_term_id, attributes_json, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, '{}', 'active', ?, ?)",
                (entity_id, term["surface"], term["category"], term["term_id"], now, now),
            )
    return legacy_map


def _sync_legacy_glossary_overrides(root: Path, connection: sqlite3.Connection) -> list[str]:
    """Treat a changed legacy glossary as an explicit, highest-priority edit."""

    data = _load_optional_legacy_json(root, "glossary.json", MAX_SIDECAR_BYTES)
    current_hash = "" if data is None else _content_hash(data)
    previous_hash = str(get_metadata(connection, "legacy_glossary_hash") or "")
    if current_hash == previous_hash:
        return []
    terms = _legacy_glossary_terms(data, "glossary.json")
    changed_ids: list[str] = []
    now = _utc_now()
    with write_transaction(connection):
        for term in terms:
            existing = connection.execute(
                "SELECT * FROM terms WHERE surface = ? AND sense = ?",
                (term["surface"], term["sense"]),
            ).fetchone()
            if existing is None:
                _insert_legacy_terms(connection, [term])
                connection.execute(
                    "UPDATE terms SET authority = 'user_decision', status = 'active', "
                    "updated_at = ? WHERE term_id = ?",
                    (now, term["term_id"]),
                )
                changed_ids.append(term["term_id"])
            else:
                aliases = {
                    row["alias"]
                    for row in connection.execute(
                        "SELECT alias FROM term_aliases WHERE term_id = ?",
                        (existing["term_id"],),
                    )
                }
                changed = any(
                    (
                        existing["target"] != term["target"],
                        existing["category"] != term["category"],
                        not set(term["aliases"]).issubset(aliases),
                    )
                )
                if changed:
                    connection.execute(
                        "UPDATE terms SET target = ?, category = ?, domain = ?, "
                        "status = 'active', authority = 'user_decision', "
                        "version = version + 1, updated_at = ? WHERE term_id = ?",
                        (
                            term["target"],
                            term["category"],
                            term["category"],
                            now,
                            existing["term_id"],
                        ),
                    )
                    for alias in term["aliases"]:
                        connection.execute(
                            "INSERT OR IGNORE INTO aliases(alias_id, term_id, surface, created_at) "
                            "VALUES(?, ?, ?, ?)",
                            (
                                _hash_id("alias", existing["term_id"], alias),
                                existing["term_id"],
                                alias,
                                now,
                            ),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO term_aliases(term_id, alias) VALUES(?, ?)",
                            (existing["term_id"], alias),
                        )
                    _confirm_by_user_decision(
                        connection, "term", existing["term_id"], now
                    )
                    changed_ids.append(existing["term_id"])
        _set_metadata(connection, "legacy_glossary_hash", current_hash)
        if changed_ids:
            _bump_memory_version(connection)
            refresh_chunk_dependencies(connection)
            _audit(
                connection,
                "legacy_glossary_override",
                "glossary",
                "glossary.json",
                {"changed_term_ids": sorted(set(changed_ids))},
            )
    return sorted(set(changed_ids))


def _validate_run_state(data: Any, path: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{path}: only run_state schema v1 can be imported")
    chunks = data.get("chunks")
    if not isinstance(chunks, dict):
        raise ValueError(f"{path}: 'chunks' must be an object")
    result: dict[str, Any] = {}
    for chunk_id, record in chunks.items():
        if not isinstance(chunk_id, str) or not CHUNK_RE.fullmatch(chunk_id):
            raise ValueError(f"{path}: invalid chunk id {chunk_id!r}")
        if not isinstance(record, dict):
            raise ValueError(f"{path}: record for {chunk_id} must be an object")
        result[chunk_id] = record
    return result


def _initial_memory_ids_for_chunk(
    connection: sqlite3.Connection, chunk_id: str
) -> dict[str, list[str]]:
    text = "\n".join(
        row["source_text"]
        for row in connection.execute(
            "SELECT source_text FROM segments WHERE chunk_id = ? ORDER BY segment_order",
            (chunk_id,),
        )
    )
    memory_ids: dict[str, list[str]] = {kind: [] for kind in MEMORY_KINDS}
    for row in connection.execute(
        "SELECT term_id, surface, sense, target, category FROM terms "
        "WHERE status = 'active' ORDER BY term_id"
    ):
        aliases = [
            alias["surface"]
            for alias in connection.execute(
                "SELECT surface FROM aliases WHERE term_id = ? ORDER BY surface",
                (row["term_id"],),
            )
        ]
        if row["surface"] in text or any(alias in text for alias in aliases):
            memory_ids["terms"].append(row["term_id"])
    memory_ids["terms"].sort()
    return memory_ids


def _insert_initial_translation_state(
    connection: sqlite3.Connection,
    run_state: Mapping[str, Any],
    legacy_term_map: Mapping[str, str],
) -> None:
    now = _utc_now()
    chunks = [
        row["chunk_id"]
        for row in connection.execute(
            "SELECT DISTINCT chunk_id, chunk_order FROM segments ORDER BY chunk_order"
        )
    ]
    for chunk_id in chunks:
        record = run_state.get(chunk_id)
        tracked = isinstance(record, dict)
        memory_ids = _initial_memory_ids_for_chunk(connection, chunk_id)
        if tracked:
            hashes = record.get("entity_hashes_used", {})
            if isinstance(hashes, dict):
                for legacy_id, value in hashes.items():
                    mapped = legacy_term_map.get(str(legacy_id))
                    if mapped and isinstance(value, str):
                        if mapped not in memory_ids["terms"]:
                            memory_ids["terms"].append(mapped)
                memory_ids["terms"].sort()
        dependency_hash = _content_hash(memory_ids)
        connection.execute(
            "INSERT INTO chunk_dependencies(chunk_id, dependency_hash, memory_ids_json, "
            "reviewed_hash, dirty, revision_count, updated_at) VALUES(?, ?, ?, '', ?, 0, ?)",
            (chunk_id, dependency_hash, _canonical_json(memory_ids), 0 if tracked else 1, now),
        )
        for segment in connection.execute(
            "SELECT segment_id, source_hash FROM segments WHERE chunk_id = ? ORDER BY segment_order",
            (chunk_id,),
        ):
            connection.execute(
                "INSERT INTO translations(segment_id, chunk_id, target_text, target_lang, profile, "
                "context_hash, source_hash, output_hash, dependency_hash, status, dirty, version, "
                "updated_at) VALUES(?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    segment["segment_id"],
                    chunk_id,
                    str(get_metadata(connection, "target_lang") or ""),
                    str(get_metadata(connection, "profile") or "general"),
                    record.get("glossary_version_used", dependency_hash)
                    if tracked
                    else dependency_hash,
                    segment["source_hash"],
                    record.get("output_hash", "") if tracked else "",
                    record.get("glossary_version_used", dependency_hash)
                    if tracked
                    else dependency_hash,
                    "legacy" if tracked else "pending",
                    0 if tracked else 1,
                    now,
                ),
            )


def _required_columns() -> dict[str, set[str]]:
    return {
        "segments": {"segment_id", "chunk_id", "ordinal", "source_text", "source_hash"},
        "reviews": {
            "id",
            "chunk_id",
            "dependency_hash",
            "severity",
            "status",
            "payload_json",
            "created_at",
        },
        "chunk_dependencies": {
            "chunk_id",
            "dependency_hash",
            "memory_ids_json",
            "reviewed_hash",
            "dirty",
            "revision_count",
        },
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
            "claim_id", "holder", "proposition", "polarity", "modality", "scope",
            "target_gloss", "status", "authority", "version",
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
    }


def _validate_database(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ValueError(f"SQLite integrity check failed: {integrity!r}")
    foreign_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_errors:
        raise ValueError(f"SQLite foreign-key check failed: {foreign_errors!r}")
    existing = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = set(TABLES) - existing
    if missing:
        raise ValueError(f"knowledge database missing table(s): {sorted(missing)!r}")
    for table, required in _required_columns().items():
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(" + table + ")")
        }
        absent = required - columns
        if absent:
            raise ValueError(f"table {table} missing column(s): {sorted(absent)!r}")
    version = get_metadata(connection, "schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"knowledge schema version mismatch: expected {SCHEMA_VERSION}, got {version!r}"
        )
    for row in connection.execute(
        "SELECT segment_id, source_text, source_hash FROM segments"
    ):
        if row["source_hash"] != _text_hash(row["source_text"]):
            raise ValueError(f"segment source hash mismatch: {row['segment_id']}")


def _cleanup_sqlite_family(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        try:
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for fsync on regular files.
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _select_automatic_profile(connection: sqlite3.Connection) -> str:
    """Choose a conservative whole-book profile without executing book data."""

    requested = str(get_metadata(connection, "requested_profile") or "auto")
    if requested != "auto":
        _set_metadata(connection, "profile", requested)
        return requested

    observed = Counter(
        str(row["profile"])
        for row in connection.execute(
            "SELECT profile FROM style_rules WHERE status = 'active'"
        )
        if str(row["profile"]) in BUILTIN_PROFILES
        and str(row["profile"]) != "general"
    )
    if observed:
        ordered = observed.most_common()
        if len(ordered) == 1 or ordered[0][1] > ordered[1][1]:
            selected = ordered[0][0]
            _set_metadata(
                connection,
                "profile_detection",
                {"basis": "analysis_style_majority", "counts": dict(sorted(observed.items()))},
            )
            _set_metadata(connection, "profile", selected)
            return selected

    text = "\n".join(
        str(row["source_text"])
        for row in connection.execute(
            "SELECT source_text FROM segments ORDER BY chunk_order, ordinal"
        )
    ).casefold()
    scores = {
        "academic-technical": sum(
            text.count(marker)
            for marker in (
                " et al.", "doi:", "abstract", "methodology", "theorem", "equation",
                "figure ", "table ", "references", "bibliography",
            )
        ),
        "legal": sum(
            text.count(marker)
            for marker in (
                "hereinafter", "pursuant to", "shall ", "statute", "plaintiff",
                "defendant", "article ", "section ", "court ", "whereas",
            )
        ),
        "literary": sum(
            text.count(marker)
            for marker in (
                '"', "“", "”", " chapter ", "said ", "whispered ", "replied ",
                "i thought", "she thought", "he thought",
            )
        ),
    }
    ordered_scores = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if ordered_scores[0][1] >= 2 and (
        len(ordered_scores) == 1 or ordered_scores[0][1] > ordered_scores[1][1]
    ):
        selected = ordered_scores[0][0]
        basis = "whole_book_heuristic"
    else:
        selected = "general"
        basis = "ambiguous_fallback"
    _set_metadata(
        connection,
        "profile_detection",
        {"basis": basis, "scores": dict(sorted(scores.items()))},
    )
    _set_metadata(connection, "profile", selected)
    return selected


def _create_database_file(
    path: Path,
    segments: Sequence[Mapping[str, Any]],
    glossary_data: Any,
    run_state_data: Any,
    profile: str | None,
    conversion_metadata: Mapping[str, str],
) -> None:
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    _configure_connection(connection, readonly=False)
    try:
        with write_transaction(connection):
            _create_schema(connection)
            _set_metadata(connection, "schema_version", SCHEMA_VERSION)
            _set_metadata(connection, "memory_version", 1)
            _set_metadata(connection, "created_at", _utc_now())
            requested_profile = profile or "auto"
            _set_metadata(connection, "requested_profile", requested_profile)
            _set_metadata(connection, "profile", "general" if requested_profile == "auto" else requested_profile)
            _set_metadata(connection, "target_lang", conversion_metadata.get("output_lang", ""))
            _set_metadata(connection, "source_lang", conversion_metadata.get("input_lang", ""))
            _set_metadata(connection, "database_filename", DATABASE_FILENAME)
            _insert_segments(connection, segments)
            _select_automatic_profile(connection)
            legacy_terms = _legacy_glossary_terms(glossary_data, "glossary.json")
            legacy_map = _insert_legacy_terms(connection, legacy_terms)
            _set_metadata(
                connection,
                "legacy_glossary_hash",
                "" if glossary_data is None else _content_hash(glossary_data),
            )
            run_state = _validate_run_state(run_state_data, "run_state.json")
            _insert_initial_translation_state(connection, run_state, legacy_map)
            # Canonicalize dependency hashes without forcing legacy outputs to
            # retranslate.  They remain review-required because reviewed_hash
            # is empty, while untracked chunks stay dirty.
            refresh_chunk_dependencies(connection, initialize=True)
            _audit(
                connection,
                "initialize",
                "database",
                DATABASE_FILENAME,
                {
                    "segments": len(segments),
                    "legacy_terms": len(legacy_terms),
                    "legacy_chunks": len(run_state),
                },
            )
        _validate_database(connection)
    finally:
        connection.close()
    _fsync_file(path)


def _migrate_existing_database(path: Path) -> None:
    """Atomically migrate an older DB copy, leaving the original on failure."""

    fd, temp_name = tempfile.mkstemp(
        prefix=".translation_state.migrate.", suffix=".sqlite3", dir=path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    _cleanup_sqlite_family(temp_path)
    try:
        source = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        destination = sqlite3.connect(str(temp_path), timeout=5.0, isolation_level=None)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        connection = sqlite3.connect(str(temp_path), timeout=5.0, isolation_level=None)
        _configure_connection(connection, readonly=False)
        try:
            with write_transaction(connection):
                _create_schema(connection)
                _set_metadata(connection, "schema_version", SCHEMA_VERSION)
                if get_metadata(connection, "memory_version") is None:
                    _set_metadata(connection, "memory_version", 1)
                _audit(
                    connection,
                    "migrate",
                    "database",
                    DATABASE_FILENAME,
                    {"to_schema_version": SCHEMA_VERSION},
                )
            _validate_database(connection)
        finally:
            connection.close()
        _fsync_file(temp_path)
        os.replace(temp_path, path)
    finally:
        _cleanup_sqlite_family(temp_path)


def initialize_database(
    temp_dir: os.PathLike[str] | str,
    profile: str | None = None,
) -> dict[str, Any]:
    """Initialize the store atomically and import legacy state without edits."""

    root = _root_path(temp_dir)
    if profile is not None and (not isinstance(profile, str) or len(profile) > 1000):
        raise ValueError("profile must be a string of at most 1000 characters")
    database = _database_path(root)
    if database.is_symlink():
        raise ValueError(f"database may not be a symbolic link: {database}")
    if database.exists():
        connection = open_database(root, readonly=True)
        try:
            raw_version = get_metadata(connection, "schema_version")
        finally:
            connection.close()
        if raw_version != SCHEMA_VERSION:
            if isinstance(raw_version, int) and raw_version > SCHEMA_VERSION:
                raise ValueError(
                    f"database schema {raw_version} is newer than supported {SCHEMA_VERSION}"
                )
            _migrate_existing_database(database)
        writable = open_database(root, readonly=False)
        try:
            _sync_legacy_glossary_overrides(root, writable)
        finally:
            writable.close()
        connection = open_database(root, readonly=True)
        try:
            _validate_database(connection)
        finally:
            connection.close()
        return status(root)

    # Parse and validate every source before any authoritative DB path exists.
    segments = _discover_segments(root)
    glossary_data = _load_optional_legacy_json(root, "glossary.json", MAX_SIDECAR_BYTES)
    run_state_data = _load_optional_legacy_json(root, "run_state.json", MAX_SIDECAR_BYTES)
    conversion_metadata = _load_conversion_metadata(root)

    fd, temp_name = tempfile.mkstemp(
        prefix=".translation_state.init.", suffix=".sqlite3", dir=root
    )
    os.close(fd)
    temp_path = Path(temp_name)
    _cleanup_sqlite_family(temp_path)
    try:
        _create_database_file(
            temp_path, segments, glossary_data, run_state_data, profile, conversion_metadata
        )
        os.replace(temp_path, database)
    finally:
        _cleanup_sqlite_family(temp_path)
    return status(root)


# Public alias kept short for importers that mirror the CLI verb.
init = initialize_database


def _reject_unknown(value: Mapping[str, Any], allowed: set[str] | frozenset[str], where: str) -> None:
    extras = set(value) - set(allowed)
    if extras:
        raise ValueError(f"{where}: unknown field(s) {sorted(extras)!r}")


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    return value


def _confidence(value: Any, where: str) -> str:
    if value not in {"low", "medium", "high"}:
        raise ValueError(f"{where} must be one of low, medium, high")
    return str(value)


def _evidence_basis(value: Any, where: str) -> str:
    if value not in VALID_EVIDENCE_BASES:
        raise ValueError(
            f"{where}.evidence_basis must be one of {sorted(VALID_EVIDENCE_BASES)!r}"
        )
    return str(value)


def _array(data: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{where}.{key} must be an array")
    if len(value) > MAX_ARRAY_ITEMS:
        raise ValueError(
            f"{where}.{key} has {len(value)} entries; maximum is {MAX_ARRAY_ITEMS}"
        )
    return value


def _validate_evidence(
    evidence: Any,
    where: str,
    chunk_id: str,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    if not isinstance(evidence, dict):
        raise ValueError(f"{where}.evidence must be an object")
    _reject_unknown(evidence, {"segment_id", "quote"}, f"{where}.evidence")
    if set(evidence) != {"segment_id", "quote"}:
        raise ValueError(f"{where}.evidence requires segment_id and quote")
    segment_id = _nonempty_string(evidence["segment_id"], f"{where}.evidence.segment_id")
    quote = _nonempty_string(evidence["quote"], f"{where}.evidence.quote")
    if len(quote) > MAX_QUOTE_CHARS:
        raise ValueError(
            f"{where}.evidence.quote exceeds {MAX_QUOTE_CHARS} characters"
        )
    segment = segment_lookup.get(segment_id)
    if segment is None:
        raise ValueError(f"{where}.evidence references unknown segment {segment_id!r}")
    if segment["chunk_id"] != chunk_id:
        raise ValueError(
            f"{where}.evidence segment belongs to {segment['chunk_id']}, not {chunk_id}"
        )
    if quote not in segment["source_text"]:
        raise ValueError(
            f"{where}.evidence.quote is not an exact substring of canonical source"
        )
    return {"segment_id": segment_id, "quote": quote}


def _validate_term_item(
    item: Any, where: str, chunk_id: str, segments: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    allowed = {
        "surface", "sense", "target", "target_proposal", "category", "domain",
        "usage_note", "forbidden_variants", "aliases", "confidence", "notes",
        "authority", "version", "evidence", "evidence_basis",
    }
    _reject_unknown(item, allowed, where)
    surface = _nonempty_string(item.get("surface"), f"{where}.surface")
    sense = _nonempty_string(item.get("sense", "default"), f"{where}.sense")
    target_a = item.get("target")
    target_b = item.get("target_proposal")
    if target_a is not None and target_b is not None and target_a != target_b:
        raise ValueError(f"{where}.target and target_proposal disagree")
    target = _string(
        target_a if target_a is not None else target_b,
        f"{where}.target_proposal",
    )
    aliases = item.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > MAX_ARRAY_ITEMS:
        raise ValueError(f"{where}.aliases must be an array of at most {MAX_ARRAY_ITEMS}")
    clean_aliases: list[str] = []
    for index, alias in enumerate(aliases):
        clean_aliases.append(_nonempty_string(alias, f"{where}.aliases[{index}]"))
    if len(set(clean_aliases)) != len(clean_aliases):
        raise ValueError(f"{where}.aliases contains duplicates")
    forbidden = item.get("forbidden_variants", [])
    if not isinstance(forbidden, list) or len(forbidden) > MAX_ARRAY_ITEMS:
        raise ValueError(
            f"{where}.forbidden_variants must be an array of at most {MAX_ARRAY_ITEMS}"
        )
    clean_forbidden = [
        _nonempty_string(value, f"{where}.forbidden_variants[{index}]")
        for index, value in enumerate(forbidden)
    ]
    if len(set(clean_forbidden)) != len(clean_forbidden):
        raise ValueError(f"{where}.forbidden_variants contains duplicates")
    version = item.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{where}.version must be a positive integer")
    return {
        "term_id": _hash_id("term", surface, sense),
        "surface": surface,
        "sense": sense,
        "target": target,
        "category": _string(item.get("category", ""), f"{where}.category"),
        "domain": _string(item.get("domain", ""), f"{where}.domain"),
        "usage_note": _string(
            item.get("usage_note", item.get("notes", "")), f"{where}.usage_note"
        ),
        "forbidden_variants": clean_forbidden,
        "aliases": clean_aliases,
        "confidence": _confidence(item.get("confidence", "medium"), f"{where}.confidence"),
        "notes": _string(item.get("notes", ""), f"{where}.notes"),
        "authority": _nonempty_string(
            item.get("authority", "model_observation"), f"{where}.authority"
        ),
        "evidence_basis": _evidence_basis(item.get("evidence_basis", "book_usage"), where),
        "version": version,
        "evidence": _validate_evidence(item.get("evidence"), where, chunk_id, segments),
    }


def _validate_fact_item(
    item: Any, where: str, chunk_id: str, segments: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    _reject_unknown(
        item,
        {
            "subject", "predicate", "object", "object_value", "polarity", "modality",
            "scope", "confidence", "authority", "version", "evidence",
            "evidence_basis",
        },
        where,
    )
    subject = _nonempty_string(item.get("subject"), f"{where}.subject")
    predicate = _nonempty_string(item.get("predicate"), f"{where}.predicate")
    object_a = item.get("object")
    object_b = item.get("object_value")
    if object_a is not None and object_b is not None and object_a != object_b:
        raise ValueError(f"{where}.object and object_value disagree")
    obj = _nonempty_string(
        object_a if object_a is not None else object_b, f"{where}.object"
    )
    polarity = _nonempty_string(item.get("polarity", "affirmed"), f"{where}.polarity")
    modality = _nonempty_string(item.get("modality", "certain"), f"{where}.modality")
    if polarity not in VALID_POLARITIES:
        raise ValueError(f"{where}.polarity is invalid")
    if modality not in VALID_MODALITIES:
        raise ValueError(f"{where}.modality is invalid")
    scope = _nonempty_string(item.get("scope", "book"), f"{where}.scope")
    version = item.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{where}.version must be a positive integer")
    return {
        "fact_id": _hash_id("fact", subject, predicate, scope),
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "object_value": obj,
        "polarity": polarity,
        "modality": modality,
        "scope": scope,
        "confidence": _confidence(item.get("confidence", "medium"), f"{where}.confidence"),
        "authority": _nonempty_string(
            item.get("authority", "model_observation"), f"{where}.authority"
        ),
        "evidence_basis": _evidence_basis(item.get("evidence_basis", "book_usage"), where),
        "version": version,
        "evidence": _validate_evidence(item.get("evidence"), where, chunk_id, segments),
    }


def _validate_claim_item(
    item: Any, where: str, chunk_id: str, segments: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    allowed = {
        "claim", "source", "subject", "predicate", "object", "holder", "proposition",
        "polarity", "modality", "scope", "target_gloss", "confidence", "authority",
        "version", "evidence",
        "evidence_basis",
    }
    _reject_unknown(item, allowed, where)
    if "proposition" in item:
        proposition = _nonempty_string(item["proposition"], f"{where}.proposition")
        holder = _string(item.get("holder", ""), f"{where}.holder")
        claim = proposition
    elif "claim" in item:
        claim = _nonempty_string(item["claim"], f"{where}.claim")
        proposition = claim
        holder = _string(item.get("holder", ""), f"{where}.holder")
    elif "source" in item:
        claim = _nonempty_string(item["source"], f"{where}.source")
        proposition = claim
        holder = _string(item.get("holder", ""), f"{where}.holder")
    elif all(key in item for key in ("subject", "predicate", "object")):
        claim = " | ".join(
            _nonempty_string(item[key], f"{where}.{key}")
            for key in ("subject", "predicate", "object")
        )
        proposition = claim
        holder = _string(item.get("holder", item.get("subject", "")), f"{where}.holder")
    else:
        raise ValueError(f"{where} requires claim (or source, or subject/predicate/object)")
    target_gloss = _string(item.get("target_gloss", ""), f"{where}.target_gloss")
    polarity = _nonempty_string(item.get("polarity", "affirmed"), f"{where}.polarity")
    modality = _nonempty_string(item.get("modality", "certain"), f"{where}.modality")
    if polarity not in VALID_POLARITIES:
        raise ValueError(f"{where}.polarity is invalid")
    if modality not in VALID_MODALITIES:
        raise ValueError(f"{where}.modality is invalid")
    scope = _nonempty_string(item.get("scope", "book"), f"{where}.scope")
    version = item.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{where}.version must be a positive integer")
    return {
        "claim_id": _hash_id("claim", holder, proposition, scope),
        "claim": claim,
        "holder": holder,
        "proposition": proposition,
        "polarity": polarity,
        "modality": modality,
        "scope": scope,
        "target_gloss": target_gloss,
        "confidence": _confidence(item.get("confidence", "medium"), f"{where}.confidence"),
        "authority": _nonempty_string(
            item.get("authority", "model_observation"), f"{where}.authority"
        ),
        "evidence_basis": _evidence_basis(item.get("evidence_basis", "book_usage"), where),
        "version": version,
        "evidence": _validate_evidence(item.get("evidence"), where, chunk_id, segments),
    }


def _validate_style_item(
    item: Any, where: str, chunk_id: str, segments: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    _reject_unknown(
        item,
        {
            "rule", "rule_text", "observation", "scope", "profile", "priority",
            "authority", "version", "evidence",
        },
        where,
    )
    raw_rule = item.get("rule", item.get("rule_text", item.get("observation")))
    rule = _nonempty_string(raw_rule, f"{where}.rule")
    scope = _nonempty_string(item.get("scope", "book"), f"{where}.scope")
    priority = item.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError(f"{where}.priority must be an integer")
    version = item.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{where}.version must be a positive integer")
    rule_id = _hash_id("rule", scope, rule, item.get("profile", "general"))
    return {
        "style_rule_id": rule_id,
        "rule_id": rule_id,
        "rule": rule,
        "rule_text": rule,
        "scope": scope,
        "profile": _nonempty_string(item.get("profile", "general"), f"{where}.profile"),
        "priority": priority,
        "authority": _nonempty_string(
            item.get("authority", "model_observation"), f"{where}.authority"
        ),
        "version": version,
        "evidence": _validate_evidence(item.get("evidence"), where, chunk_id, segments),
    }


def _validate_unresolved_item(
    item: Any, where: str, chunk_id: str, segments: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    allowed = {
        "segment_id", "issue_type", "summary", "description", "question", "item_type",
        "item_key", "options", "needed_evidence", "impact", "version", "evidence",
    }
    _reject_unknown(item, allowed, where)
    issue_type = _nonempty_string(item.get("issue_type"), f"{where}.issue_type")
    summary = item.get("summary", item.get("description", item.get("question")))
    summary = _nonempty_string(summary, f"{where}.summary")
    options = item.get("options", [])
    if not isinstance(options, list) or len(options) > MAX_ARRAY_ITEMS:
        raise ValueError(f"{where}.options must be an array of at most {MAX_ARRAY_ITEMS}")
    evidence = _validate_evidence(item.get("evidence"), where, chunk_id, segments)
    supplied_segment = item.get("segment_id", evidence["segment_id"])
    if supplied_segment != evidence["segment_id"]:
        raise ValueError(f"{where}.segment_id must match evidence.segment_id")
    impact = _nonempty_string(item.get("impact", "medium"), f"{where}.impact")
    if impact not in VALID_IMPACTS:
        raise ValueError(f"{where}.impact is invalid")
    version = item.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{where}.version must be a positive integer")
    return {
        "issue_type": issue_type,
        "summary": summary,
        "question": _nonempty_string(item.get("question", summary), f"{where}.question"),
        "segment_id": supplied_segment,
        "item_type": _string(item.get("item_type", "explicit"), f"{where}.item_type"),
        "item_key": _string(item.get("item_key", ""), f"{where}.item_key"),
        "options": options,
        "needed_evidence": _string(
            item.get("needed_evidence", ""), f"{where}.needed_evidence"
        ),
        "impact": impact,
        "version": version,
        "evidence": evidence,
    }


def validate_analysis(
    data: Any,
    *,
    path: str,
    chunk_id: str,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Strictly validate and normalize an analysis sidecar payload."""

    if not isinstance(data, dict):
        raise ValueError(f"{path}: analysis root must be an object")
    _reject_unknown(data, ANALYSIS_TOP_LEVEL_KEYS, path)
    if data.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version must be {ANALYSIS_SCHEMA_VERSION}, "
            f"got {data.get('schema_version')!r}"
        )
    if "chunk_id" in data and data["chunk_id"] != chunk_id:
        raise ValueError(
            f"{path}: payload chunk_id {data['chunk_id']!r} does not match filename {chunk_id!r}"
        )
    validators = {
        "terms": _validate_term_item,
        "facts": _validate_fact_item,
        "claims": _validate_claim_item,
        "style_observations": _validate_style_item,
        "unresolved": _validate_unresolved_item,
    }
    normalized: dict[str, Any] = {"schema_version": ANALYSIS_SCHEMA_VERSION}
    for key, validator in validators.items():
        normalized[key] = [
            validator(item, f"{path}.{key}[{index}]", chunk_id, segment_lookup)
            for index, item in enumerate(_array(data, key, path))
        ]
    return normalized


def validate_translation_meta(
    data: Any,
    *,
    path: str,
    chunk_id: str,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], str, dict[str, list[str]], list[dict[str, str]]]:
    """Validate meta.py v2 and normalize its new knowledge to analysis v1."""

    meta_mod.validate_meta(data, path)
    if data.get("schema_version") != 2:
        raise ValueError(f"{path}: knowledge-store ingest requires translation meta v2")
    analysis: dict[str, Any] = {
        "schema_version": 1,
        "terms": [],
        "facts": [],
        "claims": [],
        "style_observations": [],
        "unresolved": [],
    }
    for item in data.get("new_terms", []):
        analysis["terms"].append(
            {
                "surface": item["surface"],
                "sense": item["sense"],
                "target_proposal": item["target_proposal"],
                "category": item.get("category", ""),
                "domain": item.get("domain", ""),
                "usage_note": item.get("usage_note", ""),
                "forbidden_variants": item.get("forbidden_variants", []),
                "evidence_basis": item.get("evidence_basis", "book_usage"),
                "evidence": item["evidence"],
            }
        )
    for item in data.get("new_entities", []):
        analysis["terms"].append(
            {
                "surface": item["source"],
                "sense": item.get("category") or "entity",
                "target_proposal": item["target_proposal"],
                "category": item.get("category", ""),
                "evidence": item["evidence"],
            }
        )
    analysis["facts"].extend(data.get("new_facts", []))
    for item in data.get("attribute_hypotheses", []):
        analysis["facts"].append(
            {
                "subject": item["entity_source"],
                "predicate": item["attribute"],
                "object": item["value"],
                "polarity": "affirmed",
                "modality": "possible",
                "scope": "book",
                "confidence": item.get("confidence", "medium"),
                "evidence": item["evidence"],
            }
        )
    analysis["claims"].extend(data.get("new_claims", []))
    analysis["unresolved"].extend(data.get("unresolved", []))
    for item in data.get("conflicts", []):
        analysis["unresolved"].append(
            {
                "segment_id": item["evidence"]["segment_id"],
                "issue_type": "translation_memory_conflict",
                "question": (
                    f"For {item['entity_source']!r} field {item['field']!r}, should the "
                    "injected or newly observed value govern?"
                ),
                "options": [item["injected"], item["observed_better"]],
                "needed_evidence": "Independent source evidence or an explicit user decision",
                "impact": "high",
                "evidence": item["evidence"],
            }
        )
    for item in data.get("alias_hypotheses", []):
        analysis["unresolved"].append(
            {
                "segment_id": item["evidence"]["segment_id"],
                "issue_type": "alias_hypothesis",
                "question": (
                    f"Is {item['variant']!r} an alias of "
                    f"{item['may_be_alias_of_source']!r}?"
                ),
                "options": ["accept alias", "keep distinct"],
                "needed_evidence": "A direct definition or consistent independent usage",
                "impact": "medium",
                "evidence": item["evidence"],
            }
        )
    normalized = validate_analysis(
        analysis, path=path, chunk_id=chunk_id, segment_lookup=segment_lookup
    )
    used = {
        kind: list(data.get("used_memory_ids", {}).get(kind, []))
        for kind in MEMORY_KINDS
    }
    expected_segments = [
        segment["segment_id"]
        for segment in sorted(
            (
                segment
                for segment in segment_lookup.values()
                if segment["chunk_id"] == chunk_id
            ),
            key=lambda segment: (segment["ordinal"], segment["segment_id"]),
        )
    ]
    translations = [dict(item) for item in data.get("segment_translations", [])]
    supplied_segments = [item["segment_id"] for item in translations]
    if supplied_segments != expected_segments:
        missing = [item for item in expected_segments if item not in supplied_segments]
        extra = [item for item in supplied_segments if item not in expected_segments]
        raise ValueError(
            f"{path}: segment_translations must contain every {chunk_id} segment exactly "
            f"once in ordinal order; missing={missing!r}, extra={extra!r}"
        )
    return normalized, data["memory_dependency_hash"], used, translations


def _validate_translation_output_binding(
    root: Path,
    chunk_id: str,
    segment_translations: Sequence[Mapping[str, str]],
) -> str:
    """Bind segment memory to the exact translated Markdown being ingested."""

    output_name = f"output_{chunk_id}.md"
    output_path = _resolve_input_file(
        root,
        output_name,
        max_bytes=MAX_OUTPUT_CHUNK_BYTES,
        label="translated output",
    )
    payload = _read_limited_file(
        output_path, MAX_OUTPUT_CHUNK_BYTES, "translated output"
    )
    try:
        output_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{output_name} is not valid UTF-8") from exc

    supplied_paragraphs: list[str] = []
    for index, translation in enumerate(segment_translations):
        paragraphs = _paragraphs(str(translation["target_text"]))
        if len(paragraphs) != 1:
            raise ValueError(
                f"output_{chunk_id}.meta.json: segment_translations #{index} "
                "must represent exactly one output paragraph"
            )
        supplied_paragraphs.append(paragraphs[0])

    output_paragraphs = _paragraphs(output_text)
    if supplied_paragraphs != output_paragraphs:
        first_difference = next(
            (
                index
                for index, (supplied, actual) in enumerate(
                    zip(supplied_paragraphs, output_paragraphs)
                )
                if supplied != actual
            ),
            min(len(supplied_paragraphs), len(output_paragraphs)),
        )
        raise ValueError(
            f"output_{chunk_id}.meta.json: segment_translations do not match "
            f"the actual {output_name} paragraph sequence "
            f"(first difference at index {first_difference}; "
            f"meta={len(supplied_paragraphs)}, output={len(output_paragraphs)})"
        )
    return hashlib.sha256(payload).hexdigest()


def _segment_lookup(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["segment_id"]: dict(row)
        for row in connection.execute(
            "SELECT segment_id, chunk_id, ordinal, source_text, source_hash "
            "FROM segments ORDER BY chunk_id, ordinal"
        )
    }


def _insert_evidence(
    connection: sqlite3.Connection,
    owner_type: str,
    owner_id: str,
    evidence: Mapping[str, str],
) -> bool:
    evidence_id = _hash_id(
        "evidence", owner_type, owner_id, evidence["segment_id"], evidence["quote"]
    )
    segment = connection.execute(
        "SELECT chunk_id, source_hash FROM segments WHERE segment_id = ?",
        (evidence["segment_id"],),
    ).fetchone()
    if segment is None:
        raise ValueError(f"evidence references unknown segment {evidence['segment_id']!r}")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO evidence(evidence_id, owner_type, owner_id, item_kind, item_id, "
        "segment_id, chunk_id, quote, source_hash, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            owner_type,
            owner_id,
            owner_type,
            owner_id,
            evidence["segment_id"],
            segment["chunk_id"],
            evidence["quote"],
            segment["source_hash"],
            _utc_now(),
        ),
    )
    return cursor.rowcount > 0


def _insert_unresolved(
    connection: sqlite3.Connection,
    *,
    issue_type: str,
    chunk_id: str,
    item_type: str,
    item_key: str,
    summary: str,
    existing: Any,
    proposed: Any,
    evidence: Mapping[str, str],
    attach_evidence: bool = True,
    segment_id: str | None = None,
    question: str | None = None,
    options: Sequence[Any] | None = None,
    needed_evidence: str = "",
    impact: str = "medium",
    version: int = 1,
) -> tuple[str, bool]:
    issue_id = _hash_id(
        "issue", issue_type, chunk_id, item_type, item_key, existing, proposed
    )
    now = _utc_now()
    public_segment_id = segment_id if segment_id is not None else evidence.get("segment_id", "")
    public_question = question if question is not None else summary
    public_options = list(options or [])
    cursor = connection.execute(
        "INSERT OR IGNORE INTO unresolved(issue_id, issue_type, chunk_id, segment_id, "
        "item_type, item_key, summary, question, options_json, needed_evidence, impact, "
        "existing_json, proposed_json, evidence_json, status, resolution, resolution_json, "
        "version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'open', '', '{}', ?, ?, ?)",
        (
            issue_id,
            issue_type,
            chunk_id,
            public_segment_id,
            item_type,
            item_key,
            summary,
            public_question,
            _canonical_json(public_options),
            needed_evidence,
            impact,
            _canonical_json(existing),
            _canonical_json(proposed),
            _canonical_json(evidence),
            version,
            now,
            now,
        ),
    )
    if attach_evidence:
        _insert_evidence(connection, "unresolved", issue_id, evidence)
    return issue_id, cursor.rowcount > 0


def _explicit_definition_is_verifiable(item: Mapping[str, Any], item_type: str) -> bool:
    if item.get("evidence_basis") != "explicit_definition":
        return False
    quote = unicodedata.normalize(
        "NFKC", str(item.get("evidence", {}).get("quote") or "")
    ).casefold()
    markers = (
        " means ", " is defined as ", " refers to ", " denotes ", " signifies ",
        " hereby means ", "是指", "定义为", "意为", "即指", "称为",
    )
    if not any(marker in quote for marker in markers):
        return False
    if item_type == "term":
        anchors = (item.get("surface"),)
    elif item_type == "fact":
        anchors = (item.get("subject"), item.get("object_value"))
    else:
        anchors = (item.get("holder"), item.get("proposition"))
    meaningful = [
        unicodedata.normalize("NFKC", str(anchor)).casefold()
        for anchor in anchors
        if str(anchor or "").strip()
    ]
    return bool(meaningful) and all(anchor in quote for anchor in meaningful[:2])


def _ensure_confirmation_issue(
    connection: sqlite3.Connection,
    *,
    chunk_id: str,
    item_type: str,
    item_id: str,
    item: Mapping[str, Any],
) -> tuple[str | None, bool]:
    """Keep uncorroborated model knowledge usable but publication-blocking."""

    if _explicit_definition_is_verifiable(item, item_type):
        connection.execute(
            f"UPDATE { {'term': 'terms', 'fact': 'facts', 'claim': 'claims'}[item_type] } "
            "SET status = 'active', authority = 'source_definition', version = version + 1, "
            "updated_at = ? WHERE "
            f"{ {'term': 'term_id', 'fact': 'fact_id', 'claim': 'claim_id'}[item_type] } = ? "
            "AND status <> 'active'",
            (_utc_now(), item_id),
        )
        connection.execute(
            "UPDATE unresolved SET status = 'resolved', resolution = 'explicit_definition', "
            "resolution_json = ?, version = version + 1, updated_at = ? "
            "WHERE issue_type = 'knowledge_confirmation' AND item_type = ? "
            "AND item_key = ? AND status = 'open'",
            (
                _canonical_json({"basis": "explicit_definition"}),
                _utc_now(),
                item_type,
                item_id,
            ),
        )
        return None, bool(connection.execute("SELECT changes()").fetchone()[0])

    existing = connection.execute(
        "SELECT issue_id FROM unresolved WHERE issue_type = 'knowledge_confirmation' "
        "AND item_type = ? AND item_key = ? AND status = 'open' LIMIT 1",
        (item_type, item_id),
    ).fetchone()
    if existing is not None:
        return str(existing["issue_id"]), False
    label = {
        "term": "terminology or sense",
        "fact": "entity fact",
        "claim": "claim semantics, holder, polarity, or modality",
    }[item_type]
    issue_id, changed = _insert_unresolved(
        connection,
        issue_type="knowledge_confirmation",
        chunk_id=chunk_id,
        item_type=item_type,
        item_key=item_id,
        summary=f"Uncorroborated {label} requires stronger evidence",
        existing={},
        proposed=dict(item),
        evidence=item["evidence"],
        question=f"Should this {label} be confirmed for final publication?",
        options=["confirm", "reject or revise"],
        needed_evidence=(
            "A verifiable explicit definition, an explicit user decision, or consistent "
            "evidence from two distinct chunks followed by independent current reviews"
        ),
        impact="high",
    )
    return issue_id, changed


def _initial_knowledge_status(item: Mapping[str, Any], item_type: str) -> tuple[str, str]:
    authority = str(item.get("authority") or "model_observation")
    if authority in {"user_decision", "trusted_user_source", "legacy_glossary"}:
        return "active", authority
    if _explicit_definition_is_verifiable(item, item_type):
        return "active", "source_definition"
    return "provisional", authority


def _ingest_term(
    connection: sqlite3.Connection, chunk_id: str, item: Mapping[str, Any]
) -> tuple[str, bool]:
    now = _utc_now()
    existing = connection.execute(
        "SELECT * FROM terms WHERE surface = ? AND sense = ?",
        (item["surface"], item["sense"]),
    ).fetchone()
    changed = False
    owner_id = item["term_id"]
    if existing is not None and existing["target"] != item["target"]:
        owner_id, changed = _insert_unresolved(
            connection,
            issue_type="term_target_conflict",
            chunk_id=chunk_id,
            item_type="term",
            item_key=existing["term_id"],
            summary=f"Conflicting target for {item['surface']!r} sense {item['sense']!r}",
            existing=dict(existing),
            proposed=dict(item),
            evidence=item["evidence"],
        )
        return owner_id, changed
    status, authority = _initial_knowledge_status(item, "term")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO terms(term_id, surface, sense, target, category, domain, "
        "usage_note, forbidden_json, confidence, notes, status, authority, version, "
        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["term_id"], item["surface"], item["sense"], item["target"],
            item["category"], item["domain"], item["usage_note"],
            _canonical_json(item["forbidden_variants"]), item["confidence"], item["notes"],
            status, authority, item["version"], now, now,
        ),
    )
    changed = cursor.rowcount > 0
    for alias in item["aliases"]:
        alias_cursor = connection.execute(
            "INSERT OR IGNORE INTO aliases(alias_id, term_id, surface, created_at) VALUES(?, ?, ?, ?)",
            (_hash_id("alias", item["term_id"], alias), item["term_id"], alias, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO term_aliases(term_id, alias) VALUES(?, ?)",
            (item["term_id"], alias),
        )
        changed = changed or alias_cursor.rowcount > 0
    changed = _insert_evidence(connection, "term", item["term_id"], item["evidence"]) or changed
    _, confirmation_changed = _ensure_confirmation_issue(
        connection,
        chunk_id=chunk_id,
        item_type="term",
        item_id=item["term_id"],
        item=item,
    )
    changed = changed or confirmation_changed
    normalized_surface = unicodedata.normalize("NFKC", item["surface"]).casefold()
    colliding = [
        dict(row)
        for row in connection.execute(
            "SELECT term_id, surface, sense, target FROM terms WHERE term_id <> ? "
            "AND lower(status) NOT IN ('deleted', 'rejected', 'superseded', 'obsolete')",
            (item["term_id"],),
        )
        if unicodedata.normalize("NFKC", str(row["surface"])).casefold()
        == normalized_surface
    ]
    ambiguity_key = f"surface:{normalized_surface}"
    if colliding and connection.execute(
        "SELECT 1 FROM unresolved WHERE issue_type = 'term_sense_ambiguity' "
        "AND item_key = ? AND status = 'open' LIMIT 1",
        (ambiguity_key,),
    ).fetchone() is None:
        _, ambiguity_changed = _insert_unresolved(
            connection,
            issue_type="term_sense_ambiguity",
            chunk_id=chunk_id,
            item_type="explicit",
            item_key=ambiguity_key,
            summary=f"Multiple semantic senses share source form {item['surface']!r}",
            existing={"candidates": colliding},
            proposed={
                "term_id": item["term_id"],
                "surface": item["surface"],
                "sense": item["sense"],
                "target": item["target"],
            },
            evidence=item["evidence"],
            question=(
                "Should these entries remain distinct senses, and which contextual cues "
                "select each target?"
            ),
            options=["keep distinct senses", "merge equivalent entries", "revise senses"],
            needed_evidence="Contextual usage or an explicit definition for each sense",
            impact="high",
        )
        changed = changed or ambiguity_changed
    return item["term_id"], changed


def _ingest_fact(
    connection: sqlite3.Connection, chunk_id: str, item: Mapping[str, Any]
) -> tuple[str, bool]:
    existing = connection.execute(
        "SELECT * FROM facts WHERE fact_id = ?", (item["fact_id"],)
    ).fetchone()
    if existing is not None and any(
        existing[field] != item[field]
        for field in ("object_value", "polarity", "modality")
    ):
        return _insert_unresolved(
            connection,
            issue_type="fact_conflict",
            chunk_id=chunk_id,
            item_type="fact",
            item_key=existing["fact_id"],
            summary=f"Conflicting fact for {item['subject']!r}/{item['predicate']!r}",
            existing=dict(existing),
            proposed=dict(item),
            evidence=item["evidence"],
        )
    now = _utc_now()
    status, authority = _initial_knowledge_status(item, "fact")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO facts(fact_id, subject, predicate, object, object_value, polarity, "
        "modality, scope, confidence, status, authority, version, source_chunk_id, created_at, "
        "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["fact_id"], item["subject"], item["predicate"], item["object"],
            item["object_value"], item["polarity"], item["modality"], item["scope"],
            item["confidence"], status, authority, item["version"], chunk_id, now, now,
        ),
    )
    changed = cursor.rowcount > 0
    changed = _insert_evidence(connection, "fact", item["fact_id"], item["evidence"]) or changed
    _, confirmation_changed = _ensure_confirmation_issue(
        connection,
        chunk_id=chunk_id,
        item_type="fact",
        item_id=item["fact_id"],
        item=item,
    )
    changed = changed or confirmation_changed
    return item["fact_id"], changed


def _ingest_claim(
    connection: sqlite3.Connection, chunk_id: str, item: Mapping[str, Any]
) -> tuple[str, bool]:
    existing = connection.execute(
        "SELECT * FROM claims WHERE claim_id = ?", (item["claim_id"],)
    ).fetchone()
    if existing is not None and any(
        existing[field] != item[field]
        for field in ("target_gloss", "polarity", "modality")
    ):
        return _insert_unresolved(
            connection,
            issue_type="claim_conflict",
            chunk_id=chunk_id,
            item_type="claim",
            item_key=existing["claim_id"],
            summary=f"Conflicting target gloss for claim {item['claim']!r}",
            existing=dict(existing),
            proposed=dict(item),
            evidence=item["evidence"],
        )
    now = _utc_now()
    status, authority = _initial_knowledge_status(item, "claim")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO claims(claim_id, claim, holder, proposition, polarity, modality, "
        "scope, target_gloss, confidence, status, authority, version, source_chunk_id, created_at, "
        "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["claim_id"], item["claim"], item["holder"], item["proposition"],
            item["polarity"], item["modality"], item["scope"], item["target_gloss"],
            item["confidence"], status, authority, item["version"], chunk_id, now, now,
        ),
    )
    changed = cursor.rowcount > 0
    changed = _insert_evidence(connection, "claim", item["claim_id"], item["evidence"]) or changed
    _, confirmation_changed = _ensure_confirmation_issue(
        connection,
        chunk_id=chunk_id,
        item_type="claim",
        item_id=item["claim_id"],
        item=item,
    )
    changed = changed or confirmation_changed
    new_tokens = set(
        _relevance_tokens(
            " ".join(
                str(item.get(field) or "")
                for field in ("holder", "proposition", "scope")
            )
        )
    )
    for existing_claim in connection.execute(
        "SELECT claim_id, holder, proposition, polarity, modality, scope, target_gloss "
        "FROM claims WHERE claim_id <> ? AND lower(status) NOT IN "
        "('deleted', 'rejected', 'superseded', 'obsolete') ORDER BY claim_id",
        (item["claim_id"],),
    ):
        old = dict(existing_claim)
        old_tokens = set(
            _relevance_tokens(
                " ".join(
                    str(old.get(field) or "")
                    for field in ("holder", "proposition", "scope")
                )
            )
        )
        similarity = len(new_tokens & old_tokens) / max(len(new_tokens | old_tokens), 1)
        if similarity < 0.35:
            continue
        pair = sorted((item["claim_id"], old["claim_id"]))
        pair_key = "claim_pair:" + ":".join(pair)
        if connection.execute(
            "SELECT 1 FROM unresolved WHERE issue_type = 'claim_semantic_candidate' "
            "AND item_key = ? AND status = 'open' LIMIT 1",
            (pair_key,),
        ).fetchone() is not None:
            continue
        semantic_difference = any(
            item.get(field) != old.get(field)
            for field in ("holder", "polarity", "modality")
        )
        _, candidate_changed = _insert_unresolved(
            connection,
            issue_type="claim_semantic_candidate",
            chunk_id=chunk_id,
            item_type="explicit",
            item_key=pair_key,
            summary="Distant claims may be paraphrases or a meaningful semantic contrast",
            existing=old,
            proposed=dict(item),
            evidence=item["evidence"],
            question=(
                "Do these claims express the same canonical proposition, or must holder, "
                "polarity, modality, scope, and target gloss remain distinct?"
            ),
            options=["same canonical claim", "distinct claims", "needs more evidence"],
            needed_evidence="Restricted semantic comparison using both source contexts",
            impact="high" if semantic_difference else "medium",
        )
        changed = changed or candidate_changed
    return item["claim_id"], changed


def _ingest_style(
    connection: sqlite3.Connection, chunk_id: str, item: Mapping[str, Any]
) -> tuple[str, bool]:
    now = _utc_now()
    cursor = connection.execute(
        "INSERT OR IGNORE INTO style_rules(style_rule_id, rule_id, rule, rule_text, scope, profile, "
        "priority, status, authority, version, source_chunk_id, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            item["style_rule_id"], item["rule_id"], item["rule"], item["rule_text"],
            item["scope"], item["profile"], item["priority"], item["authority"],
            item["version"], chunk_id, now, now,
        ),
    )
    changed = cursor.rowcount > 0
    changed = _insert_evidence(
        connection, "style_rule", item["style_rule_id"], item["evidence"]
    ) or changed
    return item["style_rule_id"], changed


def _merge_chunk_memory_ids(
    connection: sqlite3.Connection, chunk_id: str, memory_ids: Mapping[str, str]
) -> None:
    row = connection.execute(
        "SELECT memory_ids_json FROM chunk_dependencies WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    current: dict[str, list[str]] = {kind: [] for kind in MEMORY_KINDS}
    if row is not None:
        decoded = json.loads(row["memory_ids_json"])
        if isinstance(decoded, dict):
            if all(kind in MEMORY_KINDS for kind in decoded):
                for kind in MEMORY_KINDS:
                    values = decoded.get(kind, [])
                    if isinstance(values, list):
                        current[kind] = [str(value) for value in values]
            else:
                # Upgrade the original flat implementation in memory.
                for object_id in decoded:
                    kind = _memory_kind_for_id(str(object_id))
                    current[kind].append(str(object_id))
    for object_id in memory_ids:
        kind = _memory_kind_for_id(str(object_id))
        if object_id not in current[kind]:
            current[kind].append(str(object_id))
    for kind in MEMORY_KINDS:
        current[kind] = sorted(set(current[kind]))
    dependency_hash = _content_hash(current)
    now = _utc_now()
    connection.execute(
        "INSERT INTO chunk_dependencies(chunk_id, dependency_hash, memory_ids_json, "
        "reviewed_hash, dirty, revision_count, updated_at) VALUES(?, ?, ?, '', 1, 0, ?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET dependency_hash=excluded.dependency_hash, "
        "memory_ids_json=excluded.memory_ids_json, dirty=1, updated_at=excluded.updated_at",
        (chunk_id, dependency_hash, _canonical_json(current), now),
    )
    connection.execute(
        "UPDATE translations SET dependency_hash = ?, dirty = 1, updated_at = ? WHERE chunk_id = ?",
        (dependency_hash, now, chunk_id),
    )


def _memory_kind_for_id(object_id: str) -> str:
    if object_id.startswith("term_"):
        return "terms"
    if object_id.startswith("fact_"):
        return "facts"
    if object_id.startswith("claim_"):
        return "claims"
    if object_id.startswith(("rule_", "style_")):
        return "style_rules"
    return "resolutions"


def _flatten_memory_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value}
    if not isinstance(value, dict):
        return set()
    result: set[str] = set()
    for key, nested in value.items():
        if key in MEMORY_KINDS and isinstance(nested, list):
            result.update(str(item) for item in nested)
        elif key not in MEMORY_KINDS:
            result.add(str(key))
    return result


def _record_translation_meta_dependencies(
    connection: sqlite3.Connection,
    chunk_id: str,
    dependency_hash: str,
    used_memory_ids: Mapping[str, Sequence[str]],
    segment_translations: Sequence[Mapping[str, str]],
    output_hash: str,
    *,
    knowledge_changed: bool,
) -> None:
    existing = connection.execute(
        "SELECT dependency_hash, dirty FROM chunk_dependencies WHERE chunk_id = ?",
        (chunk_id,),
    ).fetchone()
    stale = (
        existing is None
        or existing["dependency_hash"] != dependency_hash
    )
    dirty = 1 if stale or knowledge_changed else 0
    normalized = {
        kind: sorted(set(str(value) for value in used_memory_ids.get(kind, [])))
        for kind in MEMORY_KINDS
    }
    now = _utc_now()
    connection.execute(
        "INSERT INTO chunk_dependencies(chunk_id, dependency_hash, memory_ids_json, "
        "reviewed_hash, dirty, revision_count, updated_at) VALUES(?, ?, ?, '', ?, 0, ?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET dependency_hash=excluded.dependency_hash, "
        "memory_ids_json=excluded.memory_ids_json, dirty=excluded.dirty, "
        "reviewed_hash='', updated_at=excluded.updated_at",
        (chunk_id, dependency_hash, _canonical_json(normalized), dirty, now),
    )
    target_lang = str(get_metadata(connection, "target_lang") or "")
    profile = str(get_metadata(connection, "profile") or "general")
    for translation in segment_translations:
        cursor = connection.execute(
            "UPDATE translations SET target_text = ?, target_lang = ?, profile = ?, "
            "context_hash = ?, output_hash = ?, dependency_hash = ?, status = 'translated', dirty = ?, "
            "version = version + 1, updated_at = ? WHERE segment_id = ? AND chunk_id = ?",
            (
                translation["target_text"], target_lang, profile, dependency_hash,
                output_hash, dependency_hash, dirty, now,
                translation["segment_id"], chunk_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"translation meta references unknown segment {translation['segment_id']!r}"
            )


def ingest_sidecars(
    temp_dir: os.PathLike[str] | str,
    sidecars: Iterable[os.PathLike[str] | str],
) -> dict[str, Any]:
    """Validate all sidecars first, then ingest them in one atomic transaction."""

    root = _root_path(temp_dir)
    paths: list[tuple[str, str, Path]] = []
    seen_inputs: set[tuple[str, str]] = set()
    for supplied in sidecars:
        path = _resolve_input_file(
            root, supplied, max_bytes=MAX_SIDECAR_BYTES, label="knowledge sidecar"
        )
        match = ANALYSIS_FILE_RE.fullmatch(path.name)
        kind = "analysis"
        if match is None:
            match = TRANSLATION_META_FILE_RE.fullmatch(path.name)
            kind = "translation_meta"
        if match is None:
            raise ValueError(
                "knowledge sidecar filename must match analysis_chunkNNNN.json or "
                f"output_chunkNNNN.meta.json: {path.name!r}"
            )
        chunk_id = match.group(1)
        identity = (kind, chunk_id)
        if identity in seen_inputs:
            raise ValueError(f"duplicate {kind} sidecar for {chunk_id}")
        seen_inputs.add(identity)
        paths.append((kind, chunk_id, path))
    if not paths:
        raise ValueError("at least one analysis sidecar is required")

    connection = open_database(root, readonly=False)
    try:
        segments = _segment_lookup(connection)
        known_chunks = {segment["chunk_id"] for segment in segments.values()}
        normalized: list[dict[str, Any]] = []
        for kind, chunk_id, path in paths:
            if chunk_id not in known_chunks:
                raise ValueError(f"{path}: filename identifies unknown canonical chunk {chunk_id}")
            raw = _read_json_file(path, MAX_SIDECAR_BYTES, "knowledge sidecar")
            if kind == "analysis":
                data = validate_analysis(
                    raw, path=str(path), chunk_id=chunk_id, segment_lookup=segments
                )
                dependency_hash = None
                used_memory_ids = None
                segment_translations = None
            else:
                data, dependency_hash, used_memory_ids, segment_translations = validate_translation_meta(
                    raw, path=str(path), chunk_id=chunk_id, segment_lookup=segments
                )
                output_hash = _validate_translation_output_binding(
                    root, chunk_id, segment_translations
                )
                expected_bundle = compute_dependency_bundle(connection, chunk_id)
                if dependency_hash != expected_bundle["dependency_hash"]:
                    raise ValueError(
                        f"{path}: memory_dependency_hash is stale or not authoritative; "
                        f"expected {expected_bundle['dependency_hash']}"
                    )
                for memory_kind in MEMORY_KINDS:
                    unexpected = set(used_memory_ids[memory_kind]) - set(
                        expected_bundle["memory_ids"][memory_kind]
                    )
                    if unexpected:
                        raise ValueError(
                            f"{path}: used_memory_ids.{memory_kind} contains IDs absent "
                            f"from the authoritative packet: {sorted(unexpected)!r}"
                        )
            normalized.append(
                {
                    "kind": kind,
                    "chunk_id": chunk_id,
                    "path": path,
                    "data": data,
                    "sidecar_hash": _content_hash(raw),
                    "dependency_hash": dependency_hash,
                    "used_memory_ids": used_memory_ids,
                    "segment_translations": segment_translations,
                    "output_hash": output_hash if kind == "translation_meta" else None,
                }
            )

        ingested: list[str] = []
        skipped: list[str] = []
        issues: list[str] = []
        material_change = False
        with write_transaction(connection):
            for sidecar in normalized:
                kind = sidecar["kind"]
                chunk_id = sidecar["chunk_id"]
                path = sidecar["path"]
                data = sidecar["data"]
                sidecar_hash = sidecar["sidecar_hash"]
                meta_key = f"{kind}_hash:{chunk_id}"
                previous_hash = get_metadata(connection, meta_key)
                if previous_hash == sidecar_hash:
                    skipped.append(chunk_id)
                    continue
                chunk_memory: dict[str, str] = {}
                sidecar_knowledge_changed = False
                for item in data["terms"]:
                    object_id, changed = _ingest_term(connection, chunk_id, item)
                    chunk_memory[object_id] = _content_hash(item)
                    material_change = material_change or changed
                    sidecar_knowledge_changed = sidecar_knowledge_changed or changed
                    if object_id.startswith("issue_"):
                        issues.append(object_id)
                for item in data["facts"]:
                    object_id, changed = _ingest_fact(connection, chunk_id, item)
                    chunk_memory[object_id] = _content_hash(item)
                    material_change = material_change or changed
                    sidecar_knowledge_changed = sidecar_knowledge_changed or changed
                    if object_id.startswith("issue_"):
                        issues.append(object_id)
                for item in data["claims"]:
                    object_id, changed = _ingest_claim(connection, chunk_id, item)
                    chunk_memory[object_id] = _content_hash(item)
                    material_change = material_change or changed
                    sidecar_knowledge_changed = sidecar_knowledge_changed or changed
                    if object_id.startswith("issue_"):
                        issues.append(object_id)
                for item in data["style_observations"]:
                    object_id, changed = _ingest_style(connection, chunk_id, item)
                    chunk_memory[object_id] = _content_hash(item)
                    material_change = material_change or changed
                    sidecar_knowledge_changed = sidecar_knowledge_changed or changed
                for item in data["unresolved"]:
                    issue_id, changed = _insert_unresolved(
                        connection,
                        issue_type=item["issue_type"],
                        chunk_id=chunk_id,
                        item_type=item["item_type"],
                        item_key=item["item_key"],
                        summary=item["summary"],
                        existing={},
                        proposed={"options": item["options"]},
                        evidence=item["evidence"],
                        segment_id=item["segment_id"],
                        question=item["question"],
                        options=item["options"],
                        needed_evidence=item["needed_evidence"],
                        impact=item["impact"],
                        version=item["version"],
                    )
                    issues.append(issue_id)
                    chunk_memory[issue_id] = _content_hash(item)
                    material_change = material_change or changed
                    sidecar_knowledge_changed = sidecar_knowledge_changed or changed
                if kind == "translation_meta":
                    _record_translation_meta_dependencies(
                        connection,
                        chunk_id,
                        sidecar["dependency_hash"],
                        sidecar["used_memory_ids"],
                        sidecar["segment_translations"],
                        sidecar["output_hash"],
                        knowledge_changed=sidecar_knowledge_changed,
                    )
                    _set_metadata(
                        connection,
                        f"translation_meta_output_hash:{chunk_id}",
                        sidecar["output_hash"],
                    )
                else:
                    _merge_chunk_memory_ids(connection, chunk_id, chunk_memory)
                _set_metadata(connection, meta_key, sidecar_hash)
                _audit(
                    connection,
                    "ingest",
                    kind,
                    chunk_id,
                    {"file": path.name, "hash": sidecar_hash},
                )
                ingested.append(chunk_id)
            _select_automatic_profile(connection)
            dependency_changes = refresh_chunk_dependencies(connection)
            if material_change:
                new_version = _bump_memory_version(connection)
            else:
                new_version = memory_version(connection)
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "memory_version": new_version,
            "ingested_chunk_ids": ingested,
            "skipped_chunk_ids": skipped,
            "issue_ids": sorted(set(issues)),
            "dirty_chunk_ids": dependency_changes,
        }
    finally:
        connection.close()


# Public alias matching the CLI verb.
ingest = ingest_sidecars


def _issue_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "issue_id": row["issue_id"],
        "issue_type": row["issue_type"],
        "chunk_id": row["chunk_id"],
        "segment_id": row["segment_id"],
        "item_type": row["item_type"],
        "item_key": row["item_key"],
        "summary": row["summary"],
        "question": row["question"],
        "options": json.loads(row["options_json"]),
        "needed_evidence": row["needed_evidence"],
        "impact": row["impact"],
        "existing": json.loads(row["existing_json"]),
        "proposed": json.loads(row["proposed_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "status": row["status"],
        "resolution": row["resolution"],
        "version": row["version"],
    }


def _candidate_clusters(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Produce bounded candidates; semantic merge decisions remain external."""

    surfaces: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT term_id, surface, sense, target FROM terms "
        "WHERE lower(status) NOT IN ('deleted', 'rejected', 'superseded', 'obsolete') "
        "ORDER BY term_id"
    ):
        item = dict(row)
        forms = [item["surface"]]
        forms.extend(
            alias["alias"]
            for alias in connection.execute(
                "SELECT alias FROM term_aliases WHERE term_id = ? ORDER BY alias",
                (item["term_id"],),
            )
        )
        for form in forms:
            key = unicodedata.normalize("NFKC", str(form)).casefold()
            cluster = surfaces.setdefault(
                key,
                {"candidate_type": "term_surface_or_alias", "matched_form": form, "items": []},
            )
            if all(existing["term_id"] != item["term_id"] for existing in cluster["items"]):
                cluster["items"].append(item)
    term_clusters = [
        cluster for _, cluster in sorted(surfaces.items())
        if len(cluster["items"]) > 1
    ][:MAX_ARRAY_ITEMS]

    claims = [
        dict(row)
        for row in connection.execute(
            "SELECT claim_id, holder, proposition, polarity, modality, scope, target_gloss "
            "FROM claims WHERE lower(status) NOT IN "
            "('deleted', 'rejected', 'superseded', 'obsolete') ORDER BY claim_id"
        )
    ]
    claim_clusters: list[dict[str, Any]] = []
    for index, left in enumerate(claims):
        left_tokens = set(
            _relevance_tokens(
                " ".join(
                    str(left.get(field) or "")
                    for field in ("holder", "proposition", "scope")
                )
            )
        )
        if not left_tokens:
            continue
        for right in claims[index + 1:]:
            right_tokens = set(
                _relevance_tokens(
                    " ".join(
                        str(right.get(field) or "")
                        for field in ("holder", "proposition", "scope")
                    )
                )
            )
            union = left_tokens | right_tokens
            similarity = len(left_tokens & right_tokens) / max(len(union), 1)
            if similarity < 0.35:
                continue
            claim_clusters.append(
                {
                    "candidate_type": "claim_bm25_keyword_cjk_ngram",
                    "similarity": round(similarity, 6),
                    "items": [left, right],
                    "semantic_risk": (
                        "high"
                        if any(
                            left.get(field) != right.get(field)
                            for field in ("holder", "polarity", "modality")
                        )
                        else "medium"
                    ),
                }
            )
            if len(claim_clusters) >= MAX_ARRAY_ITEMS:
                break
        if len(claim_clusters) >= MAX_ARRAY_ITEMS:
            break
    return {"terms": term_clusters, "claims": claim_clusters}


def prepare_resolutions(temp_dir: os.PathLike[str] | str) -> dict[str, Any]:
    connection = open_database(temp_dir, readonly=True)
    try:
        issues = [
            _issue_output(row)
            for row in connection.execute(
                "SELECT * FROM unresolved WHERE status = 'open' ORDER BY created_at, issue_id"
            )
        ]
        return {
            "schema_version": 1,
            "memory_version": memory_version(connection),
            "issues": issues,
            "candidate_clusters": _candidate_clusters(connection),
            "decisions": [
                {"issue_id": issue["issue_id"], "action": "accept_proposed", "notes": ""}
                for issue in issues
            ],
        }
    finally:
        connection.close()


def _load_decisions(root: Path, supplied: os.PathLike[str] | str) -> list[dict[str, Any]]:
    path = _resolve_input_file(
        root, supplied, max_bytes=MAX_DECISIONS_BYTES, label="decisions file"
    )
    data = _read_json_file(path, MAX_DECISIONS_BYTES, "decisions file")
    if isinstance(data, list):
        decisions = data
    elif isinstance(data, dict):
        _reject_unknown(data, {"schema_version", "decisions"}, str(path))
        if data.get("schema_version", 1) != 1:
            raise ValueError(f"{path}: decisions schema_version must be 1")
        decisions = data.get("decisions")
    else:
        raise ValueError(f"{path}: decisions root must be an object or array")
    if not isinstance(decisions, list):
        raise ValueError(f"{path}: decisions must be an array")
    if len(decisions) > 1000:
        raise ValueError(f"{path}: decisions exceeds 1000 entries")
    allowed = {
        "issue_id", "unresolved_id", "action", "resolution", "choice", "target",
        "target_gloss", "value", "notes", "rationale"
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        where = f"{path}: decisions[{index}]"
        if not isinstance(decision, dict):
            raise ValueError(f"{where} must be an object")
        _reject_unknown(decision, allowed, where)
        issue_a = decision.get("issue_id")
        issue_b = decision.get("unresolved_id")
        if issue_a is not None and issue_b is not None and issue_a != issue_b:
            raise ValueError(f"{where}: issue_id and unresolved_id disagree")
        issue_id = _nonempty_string(issue_a or issue_b, f"{where}.issue_id")
        if issue_id in seen:
            raise ValueError(f"{where}: duplicate decision for {issue_id}")
        seen.add(issue_id)
        action = decision.get("action", decision.get("choice"))
        resolution = decision.get("resolution")
        if action is None and isinstance(resolution, str):
            action = resolution
        elif action is None and isinstance(resolution, dict):
            _reject_unknown(resolution, {"action", "choice", "target", "target_gloss", "value", "notes"}, where + ".resolution")
            action = resolution.get("action", resolution.get("choice"))
            merged = dict(decision)
            for key, value in resolution.items():
                if key not in {"action", "choice"}:
                    merged.setdefault(key, value)
            decision = merged
        if action is None and any(key in decision for key in ("target", "target_gloss", "value")):
            action = "set_value"
        action = _nonempty_string(action, f"{where}.action").lower().replace("-", "_")
        normalized.append({"issue_id": issue_id, "action": action, "payload": decision})
    return normalized


def _apply_proposed_value(
    connection: sqlite3.Connection,
    issue: sqlite3.Row,
    decision: Mapping[str, Any],
) -> set[str]:
    item_type = issue["item_type"]
    item_key = issue["item_key"]
    proposed = json.loads(issue["proposed_json"])
    payload = decision["payload"]
    action = decision["action"]
    now = _utc_now()
    changed_ids: set[str] = set()

    accept_proposed = action in {
        "accept_proposed", "proposed", "use_proposed", "resolve_proposed", "approve"
    }
    keep_existing = action in {
        "keep_existing", "existing", "reject", "dismiss", "ignore"
    }
    manual = action in {"set_value", "set_target", "manual"}
    if keep_existing:
        if item_type in {"term", "fact", "claim"} and item_key:
            _confirm_by_user_decision(connection, item_type, item_key, now)
            changed_ids.add(item_key)
        return changed_ids
    if not (accept_proposed or manual):
        raise ValueError(f"unsupported resolution action {action!r}")

    if item_type == "term":
        target = proposed.get("target") if accept_proposed else payload.get("target", payload.get("value"))
        target = _string(target, "term decision target")
        connection.execute(
            "UPDATE terms SET target = ?, authority = 'user_decision', version = version + 1, "
            "updated_at = ? WHERE term_id = ?",
            (target, now, item_key),
        )
        if connection.execute("SELECT changes()").fetchone()[0] == 0 and proposed:
            connection.execute(
                "INSERT INTO terms(term_id, surface, sense, target, category, domain, usage_note, "
                "forbidden_json, confidence, notes, status, authority, version, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'user_decision', 1, ?, ?)",
                (
                    proposed.get("term_id", item_key), proposed["surface"], proposed.get("sense", "default"),
                    target, proposed.get("category", ""), proposed.get("domain", ""),
                    proposed.get("usage_note", ""),
                    _canonical_json(proposed.get("forbidden_variants", [])),
                    proposed.get("confidence", "medium"), proposed.get("notes", ""), now, now,
                ),
            )
        changed_ids.add(item_key or proposed.get("term_id", ""))
    elif item_type == "fact":
        value = proposed.get("object") if accept_proposed else payload.get("value")
        value = _nonempty_string(value, "fact decision value")
        connection.execute(
            "UPDATE facts SET object = ?, object_value = ?, authority = 'user_decision', "
            "version = version + 1, updated_at = ? WHERE fact_id = ?",
            (value, value, now, item_key),
        )
        changed_ids.add(item_key)
    elif item_type == "claim":
        value = proposed.get("target_gloss") if accept_proposed else payload.get("target_gloss", payload.get("value"))
        value = _nonempty_string(value, "claim decision target_gloss")
        connection.execute(
            "UPDATE claims SET target_gloss = ?, authority = 'user_decision', "
            "version = version + 1, updated_at = ? WHERE claim_id = ?",
            (value, now, item_key),
        )
        changed_ids.add(item_key)
    elif item_type in {"style", "style_rule"}:
        value = proposed.get("rule") if accept_proposed else payload.get("value")
        value = _nonempty_string(value, "style decision value")
        connection.execute(
            "UPDATE style_rules SET rule = ?, rule_text = ?, authority = 'user_decision', "
            "version = version + 1, updated_at = ? WHERE style_rule_id = ?",
            (value, value, now, item_key),
        )
        changed_ids.add(item_key)
    elif item_type == "explicit":
        # Explicit questions can be closed without mutating a typed memory row.
        pass
    else:
        raise ValueError(f"unsupported unresolved item_type {item_type!r}")
    if item_type in {"term", "fact", "claim"} and item_key:
        _confirm_by_user_decision(connection, item_type, item_key, now)
    changed_ids.discard("")
    return changed_ids


def _confirm_by_user_decision(
    connection: sqlite3.Connection,
    item_type: str,
    item_id: str,
    now: str,
) -> None:
    table, id_column = {
        "term": ("terms", "term_id"),
        "fact": ("facts", "fact_id"),
        "claim": ("claims", "claim_id"),
    }[item_type]
    connection.execute(
        f"UPDATE {table} SET status = 'active', authority = 'user_decision', "
        f"version = version + 1, updated_at = ? WHERE {id_column} = ?",
        (now, item_id),
    )
    connection.execute(
        "UPDATE unresolved SET status = 'resolved', resolution = 'user_decision', "
        "resolution_json = ?, version = version + 1, updated_at = ? "
        "WHERE issue_type = 'knowledge_confirmation' AND item_type = ? "
        "AND item_key = ? AND status = 'open'",
        (
            _canonical_json({"basis": "explicit_user_decision"}),
            now,
            item_type,
            item_id,
        ),
    )


def _mark_dependent_chunks_dirty(
    connection: sqlite3.Connection,
    *,
    source_chunk_id: str,
    memory_ids: set[str],
) -> list[str]:
    dirty_chunks: set[str] = {source_chunk_id} if CHUNK_RE.fullmatch(source_chunk_id) else set()
    for row in connection.execute(
        "SELECT chunk_id, memory_ids_json FROM chunk_dependencies"
    ):
        try:
            ids = json.loads(row["memory_ids_json"])
        except json.JSONDecodeError:
            ids = {}
        keys = _flatten_memory_ids(ids)
        if keys & memory_ids:
            dirty_chunks.add(row["chunk_id"])
    now = _utc_now()
    for chunk_id in dirty_chunks:
        connection.execute(
            "UPDATE chunk_dependencies SET dirty = 1, updated_at = ? WHERE chunk_id = ?",
            (now, chunk_id),
        )
        connection.execute(
            "UPDATE translations SET dirty = 1, updated_at = ? WHERE chunk_id = ?",
            (now, chunk_id),
        )
    return sorted(dirty_chunks)


def apply_resolutions(
    temp_dir: os.PathLike[str] | str,
    decisions_file: os.PathLike[str] | str,
) -> dict[str, Any]:
    root = _root_path(temp_dir)
    decisions = _load_decisions(root, decisions_file)
    connection = open_database(root, readonly=False)
    try:
        applied: list[str] = []
        dirtied: set[str] = set()
        with write_transaction(connection):
            for decision in decisions:
                issue = connection.execute(
                    "SELECT * FROM unresolved WHERE issue_id = ?", (decision["issue_id"],)
                ).fetchone()
                if issue is None:
                    raise ValueError(f"unknown issue_id {decision['issue_id']!r}")
                if issue["status"] != "open":
                    raise ValueError(f"issue {decision['issue_id']!r} is not open")
                changed_ids = _apply_proposed_value(connection, issue, decision)
                decision_id = _hash_id("decision", issue["issue_id"], decision["payload"])
                connection.execute(
                    "INSERT INTO decisions(decision_id, issue_id, resolution, payload_json, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        decision_id, issue["issue_id"], decision["action"],
                        _canonical_json(decision["payload"]), _utc_now(),
                    ),
                )
                connection.execute(
                    "UPDATE unresolved SET status = 'resolved', resolution = ?, resolution_json = ?, "
                    "version = version + 1, updated_at = ? "
                    "WHERE issue_id = ?",
                    (
                        decision["action"], _canonical_json(decision["payload"]),
                        _utc_now(), issue["issue_id"],
                    ),
                )
                dirtied.update(
                    _mark_dependent_chunks_dirty(
                        connection,
                        source_chunk_id=issue["chunk_id"],
                        memory_ids=changed_ids | {issue["item_key"]},
                    )
                )
                _audit(
                    connection,
                    "resolve",
                    "unresolved",
                    issue["issue_id"],
                    decision["payload"],
                )
                applied.append(issue["issue_id"])
            dependency_changes = refresh_chunk_dependencies(connection)
            dirtied.update(dependency_changes)
            new_version = _bump_memory_version(connection) if applied else memory_version(connection)
        return {
            "schema_version": 1,
            "memory_version": new_version,
            "applied_issue_ids": applied,
            "dirty_chunk_ids": sorted(dirtied),
        }
    finally:
        connection.close()


def auto_confirm_supported_knowledge(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Promote only independently reviewed, multi-chunk corroboration.

    The caller must hold the single-writer transaction.  Model confidence is
    intentionally ignored.  Open semantic conflicts prevent promotion.
    """

    if not connection.in_transaction:
        raise ValueError("auto confirmation requires an active write transaction")
    promoted: list[str] = []
    table_specs = (
        ("term", "terms", "term_id"),
        ("fact", "facts", "fact_id"),
        ("claim", "claims", "claim_id"),
    )
    now = _utc_now()
    for item_type, table, id_column in table_specs:
        rows = connection.execute(
            f"SELECT {id_column} AS item_id FROM {table} WHERE status = 'provisional' "
            f"ORDER BY {id_column}"
        ).fetchall()
        for row in rows:
            item_id = str(row["item_id"])
            if connection.execute(
                "SELECT 1 FROM unresolved WHERE item_type = ? AND item_key = ? "
                "AND issue_type <> 'knowledge_confirmation' AND status = 'open' LIMIT 1",
                (item_type, item_id),
            ).fetchone() is not None:
                continue
            evidence_chunks = {
                str(evidence["chunk_id"])
                for evidence in connection.execute(
                    "SELECT DISTINCT chunk_id FROM evidence WHERE item_kind = ? AND item_id = ?",
                    (item_type, item_id),
                )
            }
            if len(evidence_chunks) < 2:
                continue
            reviewed_chunks = {
                str(review["chunk_id"])
                for review in connection.execute(
                    "SELECT chunk_id FROM chunk_dependencies WHERE dirty = 0 "
                    "AND reviewed_hash <> '' AND reviewed_hash = dependency_hash"
                )
                if str(review["chunk_id"]) in evidence_chunks
            }
            if len(reviewed_chunks) < 2:
                continue
            connection.execute(
                f"UPDATE {table} SET status = 'active', "
                "authority = 'corroborated_book_evidence', version = version + 1, "
                f"updated_at = ? WHERE {id_column} = ? AND status = 'provisional'",
                (now, item_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                continue
            connection.execute(
                "UPDATE unresolved SET status = 'resolved', "
                "resolution = 'corroborated_and_independently_reviewed', "
                "resolution_json = ?, version = version + 1, updated_at = ? "
                "WHERE issue_type = 'knowledge_confirmation' AND item_type = ? "
                "AND item_key = ? AND status = 'open'",
                (
                    _canonical_json(
                        {
                            "evidence_chunks": sorted(evidence_chunks),
                            "reviewed_chunks": sorted(reviewed_chunks),
                        }
                    ),
                    now,
                    item_type,
                    item_id,
                ),
            )
            _audit(
                connection,
                "auto_confirm",
                item_type,
                item_id,
                {
                    "rule": "two_distinct_chunks_plus_current_independent_reviews",
                    "evidence_chunks": sorted(evidence_chunks),
                    "reviewed_chunks": sorted(reviewed_chunks),
                },
            )
            promoted.append(item_id)
    dirty_chunks: list[str] = []
    if promoted:
        _bump_memory_version(connection)
        dirty_chunks = refresh_chunk_dependencies(connection)
    return {
        "promoted_ids": promoted,
        "dirty_chunk_ids": dirty_chunks,
        "memory_version": memory_version(connection),
    }


def _memory_rows(connection: sqlite3.Connection, table: str, order_by: str) -> list[dict[str, Any]]:
    # table/order_by are internal constants only, never user-controlled.
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]


def _relevance_tokens(text: str) -> list[str]:
    """Tokenize without network/language models, including CJK n-grams."""

    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    tokens = ASCII_TOKEN_RE.findall(normalized)
    for run in CJK_RUN_RE.findall(normalized):
        characters = list(run)
        tokens.extend(characters)
        tokens.extend("".join(characters[index:index + 2]) for index in range(len(characters) - 1))
        tokens.extend("".join(characters[index:index + 3]) for index in range(len(characters) - 2))
    return tokens


def _rank_relevant_rows(
    query: str,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Deterministic offline BM25 used only to choose dependency candidates."""

    query_terms = Counter(_relevance_tokens(query))
    if not query_terms or not rows or limit <= 0:
        return []
    documents = [
        _relevance_tokens(" ".join(str(row.get(field) or "") for field in fields))
        for row in rows
    ]
    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))
    average_length = max(
        sum(len(document) for document in documents) / max(len(documents), 1),
        1.0,
    )
    count = len(documents)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for row, document in zip(rows, documents):
        frequencies = Counter(document)
        score = 0.0
        for term, query_frequency in query_terms.items():
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0 + (count - df + 0.5) / (df + 0.5)
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * len(document) / average_length
            )
            score += (
                inverse_document_frequency
                * (frequency * 2.5 / denominator)
                * query_frequency
            )
        if score > 0:
            stable_id = next(
                (
                    str(row.get(key))
                    for key in (
                        "term_id", "fact_id", "claim_id", "rule_id", "issue_id",
                        "evidence_id", "segment_id",
                    )
                    if row.get(key) is not None
                ),
                _canonical_json(dict(row)),
            )
            scored.append((score, stable_id, dict(row)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:limit]]


def _dependency_query(connection: sqlite3.Connection, chunk_id: str) -> tuple[str, list[dict[str, Any]]]:
    segments = [
        dict(row)
        for row in connection.execute(
            "SELECT segment_id, chunk_id, chunk_order, ordinal, source_text, source_hash "
            "FROM segments WHERE chunk_id = ? ORDER BY ordinal",
            (chunk_id,),
        )
    ]
    if not segments:
        raise ValueError(f"unknown chunk_id {chunk_id!r}")
    chunk_order = segments[0]["chunk_order"]
    previous = connection.execute(
        "SELECT source_text FROM segments WHERE chunk_order = ? ORDER BY ordinal DESC LIMIT 1",
        (chunk_order - 1,),
    ).fetchone()
    following = connection.execute(
        "SELECT source_text FROM segments WHERE chunk_order = ? ORDER BY ordinal LIMIT 1",
        (chunk_order + 1,),
    ).fetchone()
    parts = []
    if previous is not None:
        parts.append(str(previous["source_text"])[-500:])
    parts.extend(str(segment["source_text"]) for segment in segments)
    if following is not None:
        parts.append(str(following["source_text"])[:500])
    return "\n".join(parts), segments


def _dependency_row(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def compute_dependency_bundle(
    source: sqlite3.Connection | os.PathLike[str] | str,
    chunk_id: str,
) -> dict[str, Any]:
    """Build a deterministic, content-addressed, chunk-relevant dependency.

    The global memory version is reported for observability but deliberately
    excluded from the hash.  A sidecar being consumed is not itself a semantic
    change; only relevant knowledge content, evidence, decisions, context, or
    reusable translation memory can invalidate a chunk.
    """

    if not isinstance(chunk_id, str) or not CHUNK_RE.fullmatch(chunk_id):
        raise ValueError(f"invalid chunk_id {chunk_id!r}")
    with _borrow_connection(source) as connection:
        query, segments = _dependency_query(connection, chunk_id)
        normalized_query = unicodedata.normalize("NFKC", query).casefold()

        all_terms = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM terms WHERE lower(status) NOT IN "
                "('deleted', 'rejected', 'superseded', 'obsolete') "
                "ORDER BY surface, sense, term_id"
            )
        ]
        aliases_by_term: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT term_id, alias FROM term_aliases ORDER BY alias, term_id"
        ):
            aliases_by_term.setdefault(str(row["term_id"]), []).append(str(row["alias"]))
        local_terms: list[dict[str, Any]] = []
        semantic_pool: list[dict[str, Any]] = []
        for term in all_terms:
            forms = [str(term.get("surface") or ""), *aliases_by_term.get(str(term["term_id"]), [])]
            if any(
                unicodedata.normalize("NFKC", form).casefold() in normalized_query
                for form in forms
                if form
            ):
                local_terms.append(term)
            else:
                semantic_pool.append(term)
        if len(local_terms) > MAX_DEPENDENCY_TERMS:
            # Context generation treats all exact local matches as mandatory;
            # an oversized set must be split rather than silently forgotten.
            raise ValueError(
                f"{chunk_id} has {len(local_terms)} mandatory local terms; "
                f"maximum is {MAX_DEPENDENCY_TERMS}; split the chunk"
            )
        semantic_terms = _rank_relevant_rows(
            query,
            semantic_pool,
            ("surface", "sense", "target", "domain", "usage_note"),
            max(0, min(32, MAX_DEPENDENCY_TERMS - len(local_terms))),
        )
        terms = local_terms + semantic_terms

        all_facts = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM facts WHERE lower(status) NOT IN "
                "('deleted', 'rejected', 'superseded', 'obsolete') ORDER BY fact_id"
            )
        ]
        facts = _rank_relevant_rows(
            query, all_facts, ("subject", "predicate", "object_value", "scope"),
            MAX_DEPENDENCY_FACTS,
        )
        all_claims = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM claims WHERE lower(status) NOT IN "
                "('deleted', 'rejected', 'superseded', 'obsolete') ORDER BY claim_id"
            )
        ]
        claims = _rank_relevant_rows(
            query, all_claims,
            ("holder", "proposition", "polarity", "modality", "scope", "target_gloss"),
            MAX_DEPENDENCY_CLAIMS,
        )

        profile = str(get_metadata(connection, "profile") or "general")
        style_rules = []
        for row in connection.execute(
            "SELECT * FROM style_rules WHERE lower(status) NOT IN "
            "('deleted', 'rejected', 'superseded', 'obsolete') "
            "ORDER BY scope, priority DESC, style_rule_id"
        ):
            item = dict(row)
            row_profile = str(item.get("profile") or "").casefold()
            scope = str(item.get("scope") or "").casefold()
            if row_profile not in {"", "*", "all", "global", profile.casefold()}:
                continue
            if scope.startswith("chunk") and chunk_id.casefold() not in scope:
                continue
            style_rules.append(item)

        all_issues = [
            dict(row)
            for row in connection.execute("SELECT * FROM unresolved ORDER BY issue_id")
        ]
        local_issues = [row for row in all_issues if row.get("chunk_id") in {"", chunk_id}]
        remote_issues = [row for row in all_issues if row not in local_issues]
        ranked_remote_issues = _rank_relevant_rows(
            query,
            remote_issues,
            ("issue_type", "question", "needed_evidence", "resolution"),
            MAX_DEPENDENCY_RESOLUTIONS,
        )
        issues = local_issues + ranked_remote_issues

        selected_ids = {
            *(str(row["term_id"]) for row in terms),
            *(str(row["fact_id"]) for row in facts),
            *(str(row["claim_id"]) for row in claims),
            *(str(row["style_rule_id"]) for row in style_rules),
            *(str(row["issue_id"]) for row in issues),
        }
        all_evidence = [dict(row) for row in connection.execute("SELECT * FROM evidence ORDER BY evidence_id")]
        selected_evidence = [
            row for row in all_evidence
            if str(row.get("item_id") or row.get("owner_id") or "") in selected_ids
        ]
        extra_evidence = _rank_relevant_rows(
            query,
            [row for row in all_evidence if row not in selected_evidence],
            ("quote",),
            MAX_DEPENDENCY_EVIDENCE,
        )
        evidence = (selected_evidence + extra_evidence)[:MAX_DEPENDENCY_EVIDENCE]

        source_hashes = {segment["source_hash"] for segment in segments}
        current_segment_ids = {str(segment["segment_id"]) for segment in segments}
        exact_memory = [
            dict(row)
            for row in connection.execute(
                "SELECT segment_id, target_text, target_lang, profile, context_hash, status, version, "
                "source_hash FROM translations WHERE target_text <> '' ORDER BY segment_id"
            )
            if row["source_hash"] in source_hashes
            and str(row["segment_id"]) not in current_segment_ids
        ]

        memory_ids: dict[str, list[str]] = {
            "terms": sorted({str(row["term_id"]) for row in terms}),
            "facts": sorted({str(row["fact_id"]) for row in facts}),
            "claims": sorted({str(row["claim_id"]) for row in claims}),
            "style_rules": sorted({str(row["style_rule_id"]) for row in style_rules}),
            "resolutions": sorted({str(row["issue_id"]) for row in issues}),
        }
        dependency_content = {
            # Segment IDs and chunk positions are provenance, not semantics.
            # Excluding them lets truly identical fragments share a context
            # hash, while the TM gate still requires known speaker identity
            # (or the exact same canonical segment) before automatic reuse.
            "source_hashes": [segment["source_hash"] for segment in segments],
            "profile": profile,
            "target_lang": str(get_metadata(connection, "target_lang") or ""),
            "terms": [
                _dependency_row(
                    row,
                    (
                        "term_id", "surface", "sense", "target", "category", "domain",
                        "usage_note", "forbidden_json", "status", "authority", "version",
                    ),
                )
                for row in terms
            ],
            "aliases": {
                str(row["term_id"]): aliases_by_term.get(str(row["term_id"]), [])
                for row in terms
            },
            "facts": [
                _dependency_row(
                    row,
                    (
                        "fact_id", "subject", "predicate", "object_value", "polarity",
                        "modality", "scope", "status", "authority", "version",
                    ),
                )
                for row in facts
            ],
            "claims": [
                _dependency_row(
                    row,
                    (
                        "claim_id", "holder", "proposition", "polarity", "modality",
                        "scope", "target_gloss", "status", "authority", "version",
                    ),
                )
                for row in claims
            ],
            "style_rules": [
                _dependency_row(
                    row,
                    (
                        "style_rule_id", "rule_id", "scope", "rule_text", "profile",
                        "priority", "status", "authority", "version",
                    ),
                )
                for row in style_rules
            ],
            "issues": [
                _dependency_row(
                    row,
                    (
                        "issue_id", "chunk_id", "segment_id", "issue_type", "question",
                        "options_json", "needed_evidence", "impact", "status", "resolution",
                        "resolution_json", "version",
                    ),
                )
                for row in issues
            ],
            "evidence": [
                _dependency_row(
                    row,
                    ("evidence_id", "item_kind", "item_id", "segment_id", "source_hash", "quote"),
                )
                for row in evidence
            ],
        }
        bundle: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "memory_version": memory_version(connection),
            "chunk_id": chunk_id,
            "terms": terms,
            "aliases": [
                {"term_id": term_id, "alias": alias}
                for term_id, aliases in sorted(aliases_by_term.items())
                if term_id in memory_ids["terms"]
                for alias in aliases
            ],
            "facts": facts,
            "claims": claims,
            "style_rules": style_rules,
            "issues": issues,
            "evidence": evidence,
            "exact_translation_memory": exact_memory,
            "memory_ids": memory_ids,
            "dependency_hash": _content_hash(dependency_content),
        }
        return bundle


def refresh_chunk_dependencies(
    connection: sqlite3.Connection,
    *,
    initialize: bool = False,
) -> list[str]:
    """Recompute every chunk and dirty only content-addressed changes."""

    chunk_ids = [
        row["chunk_id"]
        for row in connection.execute(
            "SELECT DISTINCT chunk_id, chunk_order FROM segments ORDER BY chunk_order"
        )
    ]
    changed: list[str] = []
    now = _utc_now()
    for chunk_id in chunk_ids:
        bundle = compute_dependency_bundle(connection, chunk_id)
        row = connection.execute(
            "SELECT dependency_hash, dirty FROM chunk_dependencies WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        semantic_change = row is None or row["dependency_hash"] != bundle["dependency_hash"]
        if semantic_change:
            changed.append(chunk_id)
        dirty = 0 if initialize and row is not None and not bool(row["dirty"]) else int(
            semantic_change or (row is not None and bool(row["dirty"]))
        )
        reviewed_hash = "" if semantic_change else None
        if row is None:
            connection.execute(
                "INSERT INTO chunk_dependencies(chunk_id, dependency_hash, memory_ids_json, "
                "reviewed_hash, dirty, revision_count, updated_at) VALUES(?, ?, ?, '', ?, 0, ?)",
                (
                    chunk_id,
                    bundle["dependency_hash"],
                    _canonical_json(bundle["memory_ids"]),
                    dirty,
                    now,
                ),
            )
        else:
            connection.execute(
                "UPDATE chunk_dependencies SET dependency_hash = ?, memory_ids_json = ?, "
                "reviewed_hash = CASE WHEN ? IS NULL THEN reviewed_hash ELSE ? END, "
                "dirty = ?, updated_at = ? WHERE chunk_id = ?",
                (
                    bundle["dependency_hash"],
                    _canonical_json(bundle["memory_ids"]),
                    reviewed_hash,
                    reviewed_hash or "",
                    dirty,
                    now,
                    chunk_id,
                ),
            )
        if semantic_change:
            connection.execute(
                "UPDATE translations SET dependency_hash = ?, dirty = ?, updated_at = ? "
                "WHERE chunk_id = ?",
                (bundle["dependency_hash"], dirty, now, chunk_id),
            )
        elif initialize:
            connection.execute(
                "UPDATE translations SET dependency_hash = ?, context_hash = ?, dirty = ?, "
                "updated_at = ? WHERE chunk_id = ?",
                (bundle["dependency_hash"], bundle["dependency_hash"], dirty, now, chunk_id),
            )
    return changed


def record_external_source(
    temp_dir: os.PathLike[str] | str,
    record_file: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Record provenance after, never instead of, explicit network approval."""

    root = _root_path(temp_dir)
    path = _resolve_input_file(
        root,
        record_file,
        max_bytes=MAX_SIDECAR_BYTES,
        label="external-source record",
    )
    data = _read_json_file(path, MAX_SIDECAR_BYTES, "external-source record")
    if not isinstance(data, dict):
        raise ValueError("external-source record must be an object")
    required = {
        "url", "allowed_domain", "retrieved_at", "content_hash", "conclusion",
        "authorized_by_user",
    }
    _reject_unknown(data, required, str(path))
    if set(data) != required:
        raise ValueError(
            f"external-source record requires exactly {sorted(required)!r}"
        )
    if data["authorized_by_user"] is not True:
        raise ValueError("external source must have explicit user authorization")
    url = _nonempty_string(data["url"], "external-source url")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("external-source url must be an absolute HTTP(S) URL")
    domain = _nonempty_string(data["allowed_domain"], "allowed_domain").casefold()
    if parsed.hostname.casefold() != domain:
        raise ValueError("external-source URL hostname must exactly match allowed_domain")
    retrieved_at = _nonempty_string(data["retrieved_at"], "retrieved_at")
    try:
        parsed_time = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("retrieved_at must be an ISO-8601 timestamp") from exc
    if parsed_time.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    content_hash = _nonempty_string(data["content_hash"], "content_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValueError("content_hash must be lowercase SHA-256 hex")
    conclusion = _nonempty_string(data["conclusion"], "conclusion")
    if len(conclusion) > 4096:
        raise ValueError("conclusion exceeds 4096 characters")
    source_id = _hash_id("external_source", url, content_hash)
    connection = open_database(root)
    try:
        with write_transaction(connection):
            cursor = connection.execute(
                "INSERT OR IGNORE INTO external_sources(source_id, url, domain, retrieved_at, "
                "content_hash, conclusion, authorized_by_user, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    source_id,
                    url,
                    domain,
                    parsed_time.isoformat(),
                    content_hash,
                    conclusion,
                    _utc_now(),
                ),
            )
            _audit(
                connection,
                "record_external_source",
                "external_source",
                source_id,
                {"url": url, "domain": domain, "content_hash": content_hash},
            )
        return {"source_id": source_id, "recorded": cursor.rowcount > 0}
    finally:
        connection.close()


def snapshot(temp_dir: os.PathLike[str] | str) -> dict[str, Any]:
    connection = open_database(temp_dir, readonly=True)
    try:
        unresolved_rows = connection.execute(
            "SELECT * FROM unresolved ORDER BY created_at, issue_id"
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "memory_version": memory_version(connection),
            "metadata": get_metadata(connection),
            "segments": _memory_rows(connection, "segments", "chunk_order, segment_order"),
            "terms": _memory_rows(connection, "terms", "surface, sense, term_id"),
            "aliases": _memory_rows(connection, "aliases", "surface, alias_id"),
            "entities": _memory_rows(connection, "entities", "name, entity_id"),
            "facts": _memory_rows(connection, "facts", "subject, predicate, fact_id"),
            "claims": _memory_rows(connection, "claims", "claim, claim_id"),
            "style_rules": _memory_rows(connection, "style_rules", "scope, priority DESC, style_rule_id"),
            "unresolved": [_issue_output(row) for row in unresolved_rows],
            "translations": _memory_rows(connection, "translations", "chunk_id, segment_id"),
            "chunk_dependencies": _memory_rows(connection, "chunk_dependencies", "chunk_id"),
            "reviews": _memory_rows(connection, "reviews", "id"),
            "decisions": _memory_rows(connection, "decisions", "created_at, decision_id"),
            "external_sources": _memory_rows(
                connection, "external_sources", "retrieved_at, source_id"
            ),
        }
    finally:
        connection.close()


def status(temp_dir: os.PathLike[str] | str) -> dict[str, Any]:
    connection = open_database(temp_dir, readonly=True)
    try:
        counts = {
            table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in TABLES
            if table not in {"metadata", "audit_log"}
        }
        open_issues = connection.execute(
            "SELECT COUNT(*) FROM unresolved WHERE status = 'open'"
        ).fetchone()[0]
        dirty_chunks = connection.execute(
            "SELECT COUNT(*) FROM chunk_dependencies WHERE dirty = 1"
        ).fetchone()[0]
        return {
            "database": str(_database_path(_root_path(temp_dir))),
            "schema_version": get_metadata(connection, "schema_version"),
            "memory_version": memory_version(connection),
            "counts": counts,
            "open_issue_count": open_issues,
            "dirty_chunk_count": dirty_chunks,
        }
    finally:
        connection.close()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evidence-backed translation knowledge store")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="atomically initialize the store")
    init_parser.add_argument("temp_dir")
    init_parser.add_argument("--profile")

    ingest_parser = subparsers.add_parser("ingest", help="ingest analysis sidecars")
    ingest_parser.add_argument("temp_dir")
    ingest_parser.add_argument("sidecars", nargs="+")

    prepare_parser = subparsers.add_parser(
        "prepare-resolutions", help="emit open issues and a decision template"
    )
    prepare_parser.add_argument("temp_dir")

    apply_parser = subparsers.add_parser("apply-resolutions", help="apply reviewed decisions")
    apply_parser.add_argument("temp_dir")
    apply_parser.add_argument("--decisions-file", required=True)

    source_parser = subparsers.add_parser(
        "record-source", help="record an explicitly authorized external source"
    )
    source_parser.add_argument("temp_dir")
    source_parser.add_argument("--record-file", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="emit a deterministic JSON snapshot")
    snapshot_parser.add_argument("temp_dir")

    status_parser = subparsers.add_parser("status", help="emit store health and counts")
    status_parser.add_argument("temp_dir")

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            result = initialize_database(arguments.temp_dir, profile=arguments.profile)
        elif arguments.command == "ingest":
            result = ingest_sidecars(arguments.temp_dir, arguments.sidecars)
        elif arguments.command == "prepare-resolutions":
            result = prepare_resolutions(arguments.temp_dir)
        elif arguments.command == "apply-resolutions":
            result = apply_resolutions(arguments.temp_dir, arguments.decisions_file)
        elif arguments.command == "record-source":
            result = record_external_source(arguments.temp_dir, arguments.record_file)
        elif arguments.command == "snapshot":
            result = snapshot(arguments.temp_dir)
        else:
            result = status(arguments.temp_dir)
        _print_json(result)
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
