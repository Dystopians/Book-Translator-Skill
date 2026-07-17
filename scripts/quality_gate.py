#!/usr/bin/env python3
"""Independent review gate for translated chunks.

The gate is intentionally deterministic: it validates the merge manifest,
ingests strictly validated per-chunk review JSON into the translation-state
database, and checks that every chunk is reviewed against its current
dependency hash before a final build may run.

This module does not invoke an LLM and never creates publication artifacts.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys
from html.parser import HTMLParser
from pathlib import Path

import manifest


SCHEMA_VERSION = 1
LATEST_REVIEW_SCHEMA_VERSION = 2
MAX_REVIEW_BYTES = 1024 * 1024
MAX_FINDINGS = 100
MAX_QUOTE_CHARS = 500
MAX_MESSAGE_CHARS = 4000

REVIEW_NAME_RE = re.compile(r"^review_(chunk\d+)\.json$")
REVIEW_KEYS_BY_VERSION = {
    1: frozenset({"schema_version", "dependency_hash", "findings"}),
    2: frozenset(
        {"schema_version", "dependency_hash", "output_hash", "findings"}
    ),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FINDING_KEYS = frozenset(
    {"type", "severity", "source_quote", "target_quote", "message"}
)
FINDING_TYPES = frozenset(
    {
        "omission",
        "addition",
        "terminology",
        "entity",
        "claim",
        "polarity",
        "modality",
        "attribution",
        "number",
        "citation",
        "format",
        "style",
    }
)
SEVERITIES = frozenset({"critical", "high", "medium", "low"})
BLOCKING_SEVERITIES = frozenset({"critical", "high"})
NONBLOCKING_STATUSES = frozenset({"resolved", "closed", "dismissed"})

_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\n]*\](?:\([^\)\n]+\)|\[[^\]\n]*\])"
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[[^\]\n]+\](?:\([^\)\n]+\)|\[[^\]\n]*\])"
)
_HTML_IMAGE_RE = re.compile(r"<img\b", re.IGNORECASE)
_HTML_LINK_RE = re.compile(r"<a\b[^>]*\bhref\s*=", re.IGNORECASE)
_FENCE_RE = re.compile(r"(?m)^[ \t]{0,3}(?:`{3,}|~{3,})")
_ATX_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+", re.MULTILINE)
_SETEXT_RE = re.compile(r"(?m)^(?![ \t]*$)[^\n]+\n[ \t]*(=+|-+)[ \t]*$")
_INLINE_DEST_RE = re.compile(
    r"(?P<image>!)?\[[^\]\n]*\]\(\s*(?P<dest><[^>\n]+>|[^\s)\n]+)"
)
_REFERENCE_DEST_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?P<dest><[^>\n]+>|\S+)"
)
_AUTOLINK_RE = re.compile(r"<(?P<dest>(?:https?|mailto):[^>\n]+)>", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_MATH_RE = re.compile(
    r"(?s)(\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|"
    r"(?<!\\)\$(?![\s$])(?:\\.|[^$\n])*?(?<![\s\\])\$)"
)
_CITATION_RE = re.compile(
    r"(?:\[\^[^\]\n]+\]|\[@[^\]\n]+\]|"
    r"\[(?:\d+(?:[ \t]*[-–—,;][ \t]*\d+)*)\](?![ \t]*[\[(])|"
    r"\\cite[a-zA-Z]*\{[^}\n]+\})"
)
_NUMBER_RE = re.compile(
    r"(?<!\d)[-+−]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?%?(?!\d)"
)
_MARKDOWN_ANCHOR_ID_RE = re.compile(r"\{#([A-Za-z][A-Za-z0-9_.:-]*)[\s}]")
_HTML_ANCHOR_ID_RE = re.compile(
    r"\bid\s*=\s*(['\"])([A-Za-z][A-Za-z0-9_.:-]*)\1", re.IGNORECASE
)
_PRESERVATION_CODES = {
    "headings": "heading_structure_mismatch",
    "tables": "table_structure_mismatch",
    "html_structure": "html_structure_mismatch",
    "formulas": "formula_token_mismatch",
    "citations": "citation_mismatch",
    "numbers": "numeric_token_mismatch",
    "link_destinations": "link_destination_mismatch",
    "image_destinations": "image_destination_mismatch",
    "anchor_ids": "anchor_target_mismatch",
    "fenced_code": "fenced_code_mismatch",
    "inline_code": "inline_code_mismatch",
    "unbalanced_fences": "unbalanced_fence",
}


class ReviewValidationError(ValueError):
    """Raised when a review file violates the strict on-disk schema."""


def _blocker(code, message, **details):
    item = {"code": code, "message": message}
    item.update({key: value for key, value in details.items() if value is not None})
    return item


def _finalize_report(report, mode):
    report["blocker_count"] = len(report["blockers"])
    candidate = not report["blockers"]
    report["would_be_ready_for_final"] = candidate
    # Draft mode is observational by definition.  It may say a final run would
    # pass, but it must never itself authorize publication.
    report["ready_for_final"] = bool(mode == "final" and candidate)
    return report


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_review_bytes(path):
    """Read a small ordinary file without following a pre-existing symlink."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReviewValidationError(f"cannot stat review file: {exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ReviewValidationError("symbolic-link review files are not allowed")
    if not stat.S_ISREG(before.st_mode):
        raise ReviewValidationError("review path is not an ordinary regular file")
    if before.st_size > MAX_REVIEW_BYTES:
        raise ReviewValidationError(
            f"review exceeds the {MAX_REVIEW_BYTES}-byte limit"
        )

    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            # Detect a replace-between-check-and-open race where supported.
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ReviewValidationError("review file changed while being opened")
            if not stat.S_ISREG(opened.st_mode):
                raise ReviewValidationError("review path is not an ordinary regular file")
            payload = handle.read(MAX_REVIEW_BYTES + 1)
    except ReviewValidationError:
        raise
    except OSError as exc:
        raise ReviewValidationError(f"cannot read review file: {exc}") from exc
    if len(payload) > MAX_REVIEW_BYTES:
        raise ReviewValidationError(
            f"review exceeds the {MAX_REVIEW_BYTES}-byte limit"
        )
    return payload


def _load_review_json(path):
    payload = _read_review_bytes(path)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewValidationError("review is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_strict_object)
    except ReviewValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ReviewValidationError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc


def _require_exact_keys(value, allowed, where):
    if not isinstance(value, dict):
        raise ReviewValidationError(f"{where} must be a JSON object")
    extras = set(value) - allowed
    if extras:
        raise ReviewValidationError(
            f"{where} contains unknown key(s): {sorted(extras)!r}"
        )
    missing = set(allowed) - set(value)
    # `message` is the sole optional finding field.
    if where.startswith("finding"):
        missing.discard("message")
    if missing:
        raise ReviewValidationError(
            f"{where} is missing required key(s): {sorted(missing)!r}"
        )


def _require_quote(value, field, where):
    if not isinstance(value, str):
        raise ReviewValidationError(f"{where}.{field} must be a string")
    if len(value) > MAX_QUOTE_CHARS:
        raise ReviewValidationError(
            f"{where}.{field} exceeds {MAX_QUOTE_CHARS} characters"
        )


def _validate_review(path, chunk_id, source_text, target_text, actual_output_hash):
    data = _load_review_json(path)
    if not isinstance(data, dict):
        raise ReviewValidationError("review must be a JSON object")
    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version not in REVIEW_KEYS_BY_VERSION
    ):
        raise ReviewValidationError(
            "schema_version must be integer 1 or 2"
        )
    _require_exact_keys(data, REVIEW_KEYS_BY_VERSION[schema_version], "review")

    dependency_hash = data["dependency_hash"]
    if not isinstance(dependency_hash, str) or not dependency_hash:
        raise ReviewValidationError("dependency_hash must be a non-empty string")
    if len(dependency_hash) > 256 or "\x00" in dependency_hash:
        raise ReviewValidationError("dependency_hash is not a valid bounded string")

    output_hash = data.get("output_hash")
    if schema_version == LATEST_REVIEW_SCHEMA_VERSION:
        if not isinstance(output_hash, str) or SHA256_RE.fullmatch(output_hash) is None:
            raise ReviewValidationError(
                "output_hash must be a lowercase 64-character SHA-256 digest"
            )
        if output_hash != actual_output_hash:
            raise ReviewValidationError(
                f"output_hash does not match the current output_{chunk_id}.md"
            )

    findings = data["findings"]
    if not isinstance(findings, list):
        raise ReviewValidationError("findings must be an array")
    if len(findings) > MAX_FINDINGS:
        raise ReviewValidationError(
            f"findings exceeds the {MAX_FINDINGS}-item limit"
        )

    validated = []
    for index, finding in enumerate(findings):
        where = f"finding[{index}]"
        _require_exact_keys(finding, FINDING_KEYS, where)
        finding_type = finding["type"]
        severity = finding["severity"]
        if not isinstance(finding_type, str):
            raise ReviewValidationError(f"{where}.type must be a string")
        if not isinstance(severity, str):
            raise ReviewValidationError(f"{where}.severity must be a string")
        if finding_type not in FINDING_TYPES:
            raise ReviewValidationError(
                f"{where}.type must be one of {sorted(FINDING_TYPES)!r}"
            )
        if severity not in SEVERITIES:
            raise ReviewValidationError(
                f"{where}.severity must be one of {sorted(SEVERITIES)!r}"
            )
        _require_quote(finding["source_quote"], "source_quote", where)
        _require_quote(finding["target_quote"], "target_quote", where)

        source_quote = finding["source_quote"]
        target_quote = finding["target_quote"]
        if finding_type != "format" and not (source_quote or target_quote):
            raise ReviewValidationError(
                f"{where} must provide at least one non-empty evidence quote"
            )
        if source_quote and source_quote not in source_text:
            raise ReviewValidationError(
                f"{where}.source_quote is not an exact substring of {chunk_id}.md"
            )
        if target_quote and target_quote not in target_text:
            raise ReviewValidationError(
                f"{where}.target_quote is not an exact substring of output_{chunk_id}.md"
            )

        if "message" in finding:
            message = finding["message"]
            if not isinstance(message, str):
                raise ReviewValidationError(f"{where}.message must be a string")
            if len(message) > MAX_MESSAGE_CHARS:
                raise ReviewValidationError(
                    f"{where}.message exceeds {MAX_MESSAGE_CHARS} characters"
                )
        validated.append(dict(finding))

    return {
        "chunk_id": chunk_id,
        "schema_version": schema_version,
        "dependency_hash": dependency_hash,
        "output_hash": output_hash,
        "findings": validated,
        "path": str(path),
    }


def _structure_counts(text):
    return {
        "images": len(_MARKDOWN_IMAGE_RE.findall(text))
        + len(_HTML_IMAGE_RE.findall(text)),
        "links": len(_MARKDOWN_LINK_RE.findall(text))
        + len(_HTML_LINK_RE.findall(text)),
        "code_fences": len(_FENCE_RE.findall(text)),
    }


def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mask_ranges(text, ranges):
    characters = list(text)
    for start, end in ranges:
        for index in range(max(0, start), min(len(characters), end)):
            if characters[index] not in "\r\n":
                characters[index] = " "
    return "".join(characters)


def _scan_fenced_blocks(text):
    """Return exact fenced-code fingerprints, masked ranges, and balance."""
    blocks = []
    ranges = []
    opened = None
    offset = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", raw)
        if opened is None:
            if match:
                fence = match.group(1)
                opened = {
                    "char": fence[0],
                    "length": len(fence),
                    "info": match.group(2).strip(),
                    "start": offset,
                    "body_start": offset + len(line),
                }
        elif match:
            fence = match.group(1)
            remainder = match.group(2)
            if (
                fence[0] == opened["char"]
                and len(fence) >= opened["length"]
                and not remainder.strip()
            ):
                body = text[opened["body_start"]:offset].replace("\r\n", "\n").replace("\r", "\n")
                blocks.append(_fingerprint(opened["info"] + "\0" + body))
                ranges.append((opened["start"], offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        ranges.append((opened["start"], len(text)))
    return tuple(blocks), ranges, int(opened is not None)


def _pipe_table_shapes(text):
    shapes = []
    for line in text.splitlines():
        candidate = _INLINE_CODE_RE.sub("", line.strip())
        pipes = [
            index
            for index, char in enumerate(candidate)
            if char == "|" and (index == 0 or candidate[index - 1] != "\\")
        ]
        if len(pipes) < 2:
            continue
        stripped = candidate.strip().strip("|")
        cells = re.split(r"(?<!\\)\|", stripped)
        separator = all(re.fullmatch(r"[ \t]*:?-{3,}:?[ \t]*", cell) for cell in cells)
        alignment = tuple(
            (cell.strip().startswith(":"), cell.strip().endswith(":"))
            for cell in cells
        ) if separator else ()
        shapes.append((len(cells), separator, alignment))
    return tuple(shapes)


class _StructuralHTMLParser(HTMLParser):
    STRUCTURAL_TAGS = frozenset(
        {
            "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead",
            "tbody", "tfoot", "tr", "th", "td", "ol", "ul", "li",
            "blockquote", "pre", "code", "figure", "figcaption", "sup", "sub",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.headings = []
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag in self.STRUCTURAL_TAGS:
            span = (
                attributes.get("colspan", ""), attributes.get("rowspan", "")
            ) if tag in {"th", "td"} else ("", "")
            self.events.append(("start", tag, span))
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self.headings.append(int(tag[1]))
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"])

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self.STRUCTURAL_TAGS:
            self.events.append(("end", tag, ("", "")))


def _preservation_signature(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    fenced, fenced_ranges, unbalanced = _scan_fenced_blocks(normalized)
    masked = _mask_ranges(normalized, fenced_ranges)

    inline_code_matches = list(_INLINE_CODE_RE.finditer(masked))
    inline_code = tuple(_fingerprint(match.group(1)) for match in inline_code_matches)
    masked = _mask_ranges(masked, [match.span() for match in inline_code_matches])

    html_parser = _StructuralHTMLParser()
    try:
        html_parser.feed(masked)
        html_parser.close()
    except Exception:
        # HTMLParser is tolerant; retain a deterministic failure signature if
        # a future runtime nevertheless rejects malformed input.
        html_parser.events.append(("parse_error", "", ("", "")))

    headings = [len(match.group(1)) for match in _ATX_HEADING_RE.finditer(masked)]
    headings.extend(1 if match.group(1).startswith("=") else 2 for match in _SETEXT_RE.finditer(masked))
    headings.extend(html_parser.headings)

    links = []
    images = []
    destination_ranges = []
    for match in _INLINE_DEST_RE.finditer(masked):
        destination = match.group("dest").strip("<>")
        (images if match.group("image") else links).append(destination)
        destination_ranges.append(match.span("dest"))
    for match in _REFERENCE_DEST_RE.finditer(masked):
        links.append(match.group("dest").strip("<>"))
        destination_ranges.append(match.span("dest"))
    for match in _AUTOLINK_RE.finditer(masked):
        links.append(match.group("dest"))
        destination_ranges.append(match.span("dest"))
    links.extend(html_parser.links)
    images.extend(html_parser.images)

    math_matches = list(_MATH_RE.finditer(masked))
    formulas = tuple(
        _fingerprint(re.sub(r"\s+", "", match.group(0))) for match in math_matches
    )
    secondary_mask = _mask_ranges(
        masked,
        destination_ranges + [match.span() for match in math_matches],
    )
    citations = tuple(sorted(_fingerprint(match.group(0)) for match in _CITATION_RE.finditer(secondary_mask)))
    numbers = tuple(sorted(_fingerprint(match.group(0)) for match in _NUMBER_RE.finditer(secondary_mask)))

    return {
        "headings": tuple(headings),
        "tables": _pipe_table_shapes(masked),
        "html_structure": tuple(html_parser.events),
        "formulas": formulas,
        "citations": citations,
        "numbers": numbers,
        "link_destinations": tuple(sorted(_fingerprint(item) for item in links)),
        "image_destinations": tuple(sorted(_fingerprint(item) for item in images)),
        "anchor_ids": tuple(
            sorted(
                _fingerprint(item)
                for item in (
                    [match.group(1) for match in _MARKDOWN_ANCHOR_ID_RE.finditer(masked)]
                    + [match.group(2) for match in _HTML_ANCHOR_ID_RE.finditer(masked)]
                )
            )
        ),
        "fenced_code": fenced,
        "inline_code": inline_code,
        "unbalanced_fences": unbalanced,
    }


def _preservation_changes(source_text, target_text):
    source = _preservation_signature(source_text)
    target = _preservation_signature(target_text)
    changes = []
    for invariant, code in _PRESERVATION_CODES.items():
        if source[invariant] == target[invariant]:
            continue
        source_value = source[invariant]
        target_value = target[invariant]
        changes.append(
            {
                "code": code,
                "invariant": invariant,
                "source_count": len(source_value) if isinstance(source_value, tuple) else int(source_value),
                "target_count": len(target_value) if isinstance(target_value, tuple) else int(target_value),
            }
        )
    return changes


def _table_columns(connection, table):
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1] for row in rows}


def _state_schema_errors(connection):
    required = {
        "chunk_dependencies": {
            "chunk_id",
            "dependency_hash",
            "reviewed_hash",
            "dirty",
        },
        "reviews": {
            "chunk_id",
            "dependency_hash",
            "severity",
            "status",
            "payload_json",
            "created_at",
        },
        "unresolved": {"impact", "status"},
    }
    errors = []
    for table, columns in required.items():
        present = _table_columns(connection, table)
        if not present:
            errors.append(f"missing table {table}")
            continue
        missing = columns - present
        if missing:
            errors.append(
                f"table {table} is missing column(s) {sorted(missing)!r}"
            )
    return errors


def _load_dependencies(connection):
    rows = connection.execute(
        "SELECT chunk_id, dependency_hash, reviewed_hash, dirty "
        "FROM chunk_dependencies"
    ).fetchall()
    return {
        row["chunk_id"]: {
            "dependency_hash": row["dependency_hash"],
            "reviewed_hash": row["reviewed_hash"],
            "dirty": row["dirty"],
        }
        for row in rows
    }


def _ingest_reviews(connection, reviews, dependencies):
    """Atomically replace review findings and update per-chunk review state."""
    timestamp = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for review in reviews:
            chunk_id = review["chunk_id"]
            if chunk_id not in dependencies:
                raise ValueError(
                    f"cannot ingest review for {chunk_id}: dependency row is missing"
                )
            dependency_hash = review["dependency_hash"]
            connection.execute(
                "DELETE FROM reviews WHERE chunk_id = ? AND dependency_hash = ?",
                (chunk_id, dependency_hash),
            )
            findings = review["findings"]
            for finding in findings:
                payload = dict(finding)
                payload["review_schema_version"] = review["schema_version"]
                if review.get("output_hash"):
                    payload["review_output_hash"] = review["output_hash"]
                connection.execute(
                    "INSERT INTO reviews "
                    "(chunk_id, dependency_hash, severity, status, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        dependency_hash,
                        finding["severity"],
                        "open",
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        timestamp,
                    ),
                )
            if not findings:
                # Persist a positive, independently completed review as an
                # audit record too.  ``reviewed_hash`` is the current-state
                # index, while this row preserves the historical fact that a
                # reviewer inspected this exact dependency version and found
                # no reportable defect.
                connection.execute(
                    "INSERT INTO reviews "
                    "(chunk_id, dependency_hash, severity, status, payload_json, created_at) "
                    "VALUES (?, ?, 'none', 'resolved', ?, ?)",
                    (
                        chunk_id,
                        dependency_hash,
                        json.dumps(
                            {
                                "type": "review_complete",
                                "severity": "none",
                                "message": "Independent review completed with no findings.",
                                "review_schema_version": review["schema_version"],
                                **(
                                    {"review_output_hash": review["output_hash"]}
                                    if review.get("output_hash")
                                    else {}
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        timestamp,
                    ),
                )
            # A review may add a blocker, but it must never clear a dirty bit
            # created by changed knowledge. Only recording a new translation
            # against the current dependency hash can clear that bit.
            is_dirty = int(
                bool(dependencies[chunk_id]["dirty"])
                or any(
                    finding["severity"] in BLOCKING_SEVERITIES
                    for finding in findings
                )
            )
            cursor = connection.execute(
                "UPDATE chunk_dependencies "
                "SET reviewed_hash = ?, dirty = ? WHERE chunk_id = ?",
                (dependency_hash, is_dirty, chunk_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"cannot update review state for {chunk_id}: expected one row"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _blocking_unresolved_count(connection):
    placeholders = ",".join("?" for _ in NONBLOCKING_STATUSES)
    row = connection.execute(
        "SELECT COUNT(*) FROM unresolved "
        "WHERE lower(COALESCE(impact, '')) IN ('critical', 'high') "
        f"AND lower(COALESCE(status, '')) NOT IN ({placeholders})",
        tuple(sorted(NONBLOCKING_STATUSES)),
    ).fetchone()
    return int(row[0])


def _blocking_review_counts(connection):
    placeholders = ",".join("?" for _ in NONBLOCKING_STATUSES)
    rows = connection.execute(
        "SELECT r.chunk_id, COUNT(*) AS finding_count "
        "FROM reviews AS r "
        "JOIN chunk_dependencies AS d "
        "ON d.chunk_id = r.chunk_id "
        "AND d.dependency_hash = r.dependency_hash "
        "WHERE lower(COALESCE(r.severity, '')) IN ('critical', 'high') "
        f"AND lower(COALESCE(r.status, '')) NOT IN ({placeholders}) "
        "GROUP BY r.chunk_id ORDER BY r.chunk_id",
        tuple(sorted(NONBLOCKING_STATUSES)),
    ).fetchall()
    return {row["chunk_id"]: int(row["finding_count"]) for row in rows}


def evaluate_gate(temp_dir, mode, ingest_reviews=True):
    """Evaluate whether a translated temp directory is ready for final build.

    Args:
        temp_dir: Pipeline temp directory containing manifest/chunks/state.
        mode: ``draft`` (always observational) or ``final``.
        ingest_reviews: When true, import valid review JSON transactionally.

    Returns:
        A JSON-serializable report.  ``ready_for_final`` can only be true in
        final mode and only when no blocker remains.
    """
    if mode not in {"draft", "final"}:
        raise ValueError("mode must be 'draft' or 'final'")

    root = Path(temp_dir).resolve()
    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "temp_dir": str(root),
        "ready_for_final": False,
        "would_be_ready_for_final": False,
        "blocker_count": 0,
        "blockers": [],
        "warnings": [],
        "manifest": {"ok": False, "chunk_count": 0},
        "reviews": {
            "valid_files": 0,
            "invalid_files": 0,
            "ingested_files": 0,
            "nonblocking_findings": {},
        },
        "knowledge_confirmation": {"promoted_ids": [], "dirty_chunk_ids": []},
        "translation_meta": {"applied": 0, "legacy_adopted": 0},
        "format_checks": [],
    }

    # This is deliberately the first pipeline validator called by the gate.
    manifest_stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(manifest_stdout):
            manifest_ok, _, manifest_warnings = manifest.validate_for_merge(str(root))
    except Exception as exc:
        report["blockers"].append(
            _blocker(
                "manifest_validator_error",
                f"manifest.validate_for_merge failed unexpectedly: {exc}",
            )
        )
        return _finalize_report(report, mode)
    report["warnings"].extend(manifest_warnings or [])
    diagnostics = manifest_stdout.getvalue().strip()
    if not manifest_ok:
        report["blockers"].append(
            _blocker(
                "manifest_validation_failed",
                diagnostics or "manifest.validate_for_merge rejected the workspace",
            )
        )
        return _finalize_report(report, mode)

    try:
        manifest_data = manifest.load_manifest(str(root))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        report["blockers"].append(
            _blocker("manifest_invalid", f"cannot load manifest.json: {exc}")
        )
        return _finalize_report(report, mode)
    if manifest_data is None:
        report["blockers"].append(
            _blocker(
                "manifest_missing",
                "quality gating requires a validated manifest.json; legacy merge is not allowed",
            )
        )
        return _finalize_report(report, mode)

    chunks = sorted(manifest_data["chunks"], key=lambda item: item["order"])
    report["manifest"] = {"ok": True, "chunk_count": len(chunks)}
    chunks_by_id = {chunk["id"]: chunk for chunk in chunks}
    source_texts = {}
    target_texts = {}
    target_hashes = {}

    for chunk in chunks:
        chunk_id = chunk["id"]
        source_path = root / chunk["source_file"]
        target_path = root / chunk["output_file"]
        try:
            source_payload = source_path.read_bytes()
            target_payload = target_path.read_bytes()
            source_text = source_payload.decode("utf-8")
            target_text = target_payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report["blockers"].append(
                _blocker(
                    "chunk_read_failed",
                    f"cannot read source/output text for {chunk_id}: {exc}",
                    chunk_id=chunk_id,
                )
            )
            continue
        source_texts[chunk_id] = source_text
        target_texts[chunk_id] = target_text
        target_hashes[chunk_id] = hashlib.sha256(target_payload).hexdigest()
        source_counts = _structure_counts(source_text)
        target_counts = _structure_counts(target_text)
        check = {
            "chunk_id": chunk_id,
            "source": source_counts,
            "target": target_counts,
            "ok": True,
        }
        for structure_type in ("images", "links", "code_fences"):
            if target_counts[structure_type] < source_counts[structure_type]:
                check["ok"] = False
                report["blockers"].append(
                    _blocker(
                        "format_structure_loss",
                        f"{chunk_id} lost {structure_type}: "
                        f"{source_counts[structure_type]} source vs "
                        f"{target_counts[structure_type]} target",
                        chunk_id=chunk_id,
                        structure_type=structure_type,
                        source_count=source_counts[structure_type],
                        target_count=target_counts[structure_type],
                    )
                )
        preservation_changes = _preservation_changes(source_text, target_text)
        check["preservation_changes"] = preservation_changes
        for change in preservation_changes:
            check["ok"] = False
            report["blockers"].append(
                _blocker(
                    change["code"],
                    f"{chunk_id} changed required {change['invariant']} structure or content",
                    chunk_id=chunk_id,
                    invariant=change["invariant"],
                    source_count=change["source_count"],
                    target_count=change["target_count"],
                )
            )
        report["format_checks"].append(check)

    valid_reviews = []
    review_paths = sorted(root.glob("review_*.json"), key=lambda path: path.name)
    seen_review_chunks = set()
    for path in review_paths:
        match = REVIEW_NAME_RE.fullmatch(path.name)
        if not match:
            report["reviews"]["invalid_files"] += 1
            report["blockers"].append(
                _blocker(
                    "review_invalid_name",
                    f"review file name is not canonical: {path.name}",
                    review_file=path.name,
                )
            )
            continue
        chunk_id = match.group(1)
        if chunk_id not in chunks_by_id:
            report["reviews"]["invalid_files"] += 1
            report["blockers"].append(
                _blocker(
                    "review_unknown_chunk",
                    f"{path.name} does not correspond to a manifest chunk",
                    chunk_id=chunk_id,
                    review_file=path.name,
                )
            )
            continue
        if chunk_id in seen_review_chunks:
            # Defensive only: canonical filenames make duplicates impossible on
            # normal filesystems, but case-folding filesystems deserve a clear
            # failure rather than last-writer-wins behavior.
            report["reviews"]["invalid_files"] += 1
            report["blockers"].append(
                _blocker(
                    "review_duplicate_chunk",
                    f"multiple review files resolve to {chunk_id}",
                    chunk_id=chunk_id,
                )
            )
            continue
        seen_review_chunks.add(chunk_id)
        if chunk_id not in source_texts or chunk_id not in target_texts:
            report["reviews"]["invalid_files"] += 1
            report["blockers"].append(
                _blocker(
                    "review_unverifiable",
                    f"cannot verify evidence for {path.name} because chunk text is unavailable",
                    chunk_id=chunk_id,
                    review_file=path.name,
                )
            )
            continue
        try:
            review = _validate_review(
                path,
                chunk_id,
                source_texts[chunk_id],
                target_texts[chunk_id],
                target_hashes[chunk_id],
            )
        except ReviewValidationError as exc:
            report["reviews"]["invalid_files"] += 1
            report["blockers"].append(
                _blocker(
                    "review_invalid",
                    f"{path.name}: {exc}",
                    chunk_id=chunk_id,
                    review_file=path.name,
                )
            )
            continue
        valid_reviews.append(review)
        report["reviews"]["valid_files"] += 1

    db_path = root / "translation_state.sqlite3"
    try:
        db_lstat = db_path.lstat()
    except FileNotFoundError:
        report["blockers"].append(
            _blocker(
                "state_database_missing",
                "translation_state.sqlite3 is required for independent review gating",
            )
        )
        return _finalize_report(report, mode)
    except OSError as exc:
        report["blockers"].append(
            _blocker("state_database_unreadable", f"cannot stat state database: {exc}")
        )
        return _finalize_report(report, mode)
    if stat.S_ISLNK(db_lstat.st_mode) or not stat.S_ISREG(db_lstat.st_mode):
        report["blockers"].append(
            _blocker(
                "state_database_unsafe",
                "translation_state.sqlite3 must be an ordinary non-symlink file",
            )
        )
        return _finalize_report(report, mode)

    try:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        schema_errors = _state_schema_errors(connection)
        if schema_errors:
            report["blockers"].extend(
                _blocker("state_schema_invalid", message) for message in schema_errors
            )
            connection.close()
            return _finalize_report(report, mode)

        dependencies = _load_dependencies(connection)
        store_marker = connection.execute(
            "SELECT value FROM metadata WHERE key = 'database_filename'"
        ).fetchone()
        authoritative_store = bool(
            store_marker is not None
            and store_marker[0] == "translation_state.sqlite3"
        )
        ingestable_reviews = []
        for review in valid_reviews:
            if (
                authoritative_store
                and review["schema_version"] != LATEST_REVIEW_SCHEMA_VERSION
            ):
                report["reviews"]["valid_files"] -= 1
                report["reviews"]["invalid_files"] += 1
                report["blockers"].append(
                    _blocker(
                        "review_v2_required",
                        f"{Path(review['path']).name} must use schema v2 and bind the reviewed output hash",
                        chunk_id=review["chunk_id"],
                        review_file=Path(review["path"]).name,
                    )
                )
                continue
            if review["chunk_id"] not in dependencies:
                report["blockers"].append(
                    _blocker(
                        "dependency_state_missing",
                        f"no chunk_dependencies row for {review['chunk_id']}",
                        chunk_id=review["chunk_id"],
                    )
                )
            else:
                ingestable_reviews.append(review)

        if ingest_reviews and ingestable_reviews:
            try:
                _ingest_reviews(connection, ingestable_reviews, dependencies)
            except (sqlite3.Error, ValueError) as exc:
                report["blockers"].append(
                    _blocker("review_ingest_failed", f"review transaction failed: {exc}")
                )
            else:
                report["reviews"]["ingested_files"] = len(ingestable_reviews)

        if authoritative_store:
            import meta as meta_mod
            import run_state as run_state_mod

            try:
                run_records = run_state_mod.load_run_state(str(root)).get("chunks", {})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                run_records = {}
                report["blockers"].append(
                    _blocker("run_state_invalid", f"cannot validate run_state.json: {exc}")
                )
            for chunk in chunks:
                chunk_id = chunk["id"]
                hash_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    (f"translation_meta_hash:{chunk_id}",),
                ).fetchone()
                if hash_row is None:
                    if bool(run_records.get(chunk_id, {}).get("legacy_adopted")):
                        report["translation_meta"]["legacy_adopted"] += 1
                    else:
                        report["blockers"].append(
                            _blocker(
                                "translation_meta_missing",
                                f"{chunk_id} has neither applied v2 meta nor explicit legacy adoption",
                                chunk_id=chunk_id,
                            )
                        )
                    continue
                meta_path = root / f"output_{chunk_id}.meta.json"
                try:
                    meta_data = meta_mod.load_meta(str(meta_path))
                except (OSError, ValueError) as exc:
                    report["blockers"].append(
                        _blocker(
                            "translation_meta_invalid",
                            f"{meta_path.name}: {exc}",
                            chunk_id=chunk_id,
                        )
                    )
                    continue
                actual_hash = meta_mod.meta_content_hash(meta_data)
                output_hash_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    (f"translation_meta_output_hash:{chunk_id}",),
                ).fetchone()
                if (
                    meta_data.get("schema_version") != 2
                    or actual_hash != hash_row[0]
                    or output_hash_row is None
                    or output_hash_row[0] != target_hashes.get(chunk_id)
                ):
                    report["blockers"].append(
                        _blocker(
                            "translation_meta_stale",
                            f"{meta_path.name} does not match the applied v2 meta/output binding",
                            chunk_id=chunk_id,
                        )
                    )
                    continue
                report["translation_meta"]["applied"] += 1
            try:
                import knowledge_store

                with knowledge_store.write_transaction(connection):
                    confirmation = knowledge_store.auto_confirm_supported_knowledge(
                        connection
                    )
            except (sqlite3.Error, ValueError) as exc:
                report["blockers"].append(
                    _blocker(
                        "knowledge_confirmation_failed",
                        f"evidence confirmation transaction failed: {exc}",
                    )
                )
            else:
                report["knowledge_confirmation"] = {
                    "promoted_ids": confirmation["promoted_ids"],
                    "dirty_chunk_ids": confirmation["dirty_chunk_ids"],
                }

        dependencies = _load_dependencies(connection)
        for chunk in chunks:
            chunk_id = chunk["id"]
            state = dependencies.get(chunk_id)
            if state is None:
                report["blockers"].append(
                    _blocker(
                        "dependency_state_missing",
                        f"no chunk_dependencies row for {chunk_id}",
                        chunk_id=chunk_id,
                    )
                )
                continue
            dependency_hash = state["dependency_hash"]
            if not isinstance(dependency_hash, str) or not dependency_hash:
                report["blockers"].append(
                    _blocker(
                        "dependency_hash_invalid",
                        f"{chunk_id} has no valid current dependency_hash",
                        chunk_id=chunk_id,
                    )
                )
            if state["reviewed_hash"] != dependency_hash:
                report["blockers"].append(
                    _blocker(
                        "review_missing_or_stale",
                        f"{chunk_id} review hash does not match its current dependency hash",
                        chunk_id=chunk_id,
                        dependency_hash=dependency_hash,
                        reviewed_hash=state["reviewed_hash"],
                    )
                )
            dirty = state["dirty"]
            if isinstance(dirty, bool) or not isinstance(dirty, int) or dirty != 0:
                report["blockers"].append(
                    _blocker(
                        "chunk_dirty",
                        f"{chunk_id} remains dirty and must be revised/re-reviewed",
                        chunk_id=chunk_id,
                    )
                )

        unresolved_count = _blocking_unresolved_count(connection)
        if unresolved_count:
            report["blockers"].append(
                _blocker(
                    "blocking_unresolved",
                    f"{unresolved_count} unresolved high/critical knowledge issue(s) remain",
                    count=unresolved_count,
                )
            )

        for chunk_id, count in _blocking_review_counts(connection).items():
            report["blockers"].append(
                _blocker(
                    "blocking_review_findings",
                    f"{chunk_id} has {count} open high/critical review finding(s)",
                    chunk_id=chunk_id,
                    count=count,
                )
            )
        nonblocking = connection.execute(
            "SELECT lower(r.severity) AS severity, COUNT(*) AS finding_count "
            "FROM reviews AS r JOIN chunk_dependencies AS d "
            "ON d.chunk_id = r.chunk_id AND d.dependency_hash = r.dependency_hash "
            "WHERE lower(r.severity) IN ('medium', 'low') "
            "AND lower(r.status) NOT IN ('resolved', 'closed', 'dismissed') "
            "GROUP BY lower(r.severity) ORDER BY lower(r.severity)"
        ).fetchall()
        report["reviews"]["nonblocking_findings"] = {
            row["severity"]: int(row["finding_count"]) for row in nonblocking
        }
        connection.close()
    except sqlite3.Error as exc:
        report["blockers"].append(
            _blocker("state_database_error", f"cannot evaluate translation state: {exc}")
        )

    return _finalize_report(report, mode)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Evaluate the independent review gate for translated chunks"
    )
    parser.add_argument("temp_dir", help="Path to the translation temp directory")
    parser.add_argument("--mode", required=True, choices=("draft", "final"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_gate(args.temp_dir, args.mode, ingest_reviews=True)
    except Exception as exc:  # Last-resort JSON-only CLI contract.
        report = {
            "schema_version": SCHEMA_VERSION,
            "mode": args.mode,
            "ready_for_final": False,
            "would_be_ready_for_final": False,
            "blocker_count": 1,
            "blockers": [
                _blocker("quality_gate_internal_error", f"quality gate failed: {exc}")
            ],
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if args.mode == "draft":
        return 0
    return 0 if report.get("ready_for_final") else 1


if __name__ == "__main__":
    sys.exit(main())
