#!/usr/bin/env python3
"""
manifest.py - Manifest management for chunk tracking and merge validation.
"""

import os
import json
import hashlib
import re


CHUNK_ID_RE = re.compile(r'^chunk\d+$')
SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_CHUNKS = 100_000


def file_hash(filepath):
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for block in iter(lambda: f.read(8192), b''):
            h.update(block)
    return h.hexdigest()


def read_output_text(filepath):
    """Read a translated output chunk as UTF-8 text.

    Returns None when the file cannot be read or decoded. Callers treat None
    the same as blank content: the chunk has no usable translation.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def create_manifest(temp_dir, chunk_files, source_md_path):
    """Create manifest.json after splitting.

    Args:
        temp_dir: temp directory path
        chunk_files: list of chunk filenames (e.g. ['chunk0001.md', ...])
        source_md_path: path to the source input.md
    """
    source_hash = file_hash(source_md_path) if os.path.exists(source_md_path) else ""

    chunks = []
    for order, filename in enumerate(chunk_files, 1):
        chunk_id = os.path.splitext(filename)[0]  # e.g. "chunk0001"
        if not CHUNK_ID_RE.fullmatch(chunk_id) or filename != f"{chunk_id}.md":
            raise ValueError(f"Invalid canonical chunk filename: {filename!r}")
        filepath = os.path.join(temp_dir, filename)
        # Derive output filename: chunk0001.md -> output_chunk0001.md
        output_filename = f"output_{filename}"

        chunks.append({
            "id": chunk_id,
            "order": order,
            "source_file": filename,
            "source_hash": file_hash(filepath) if os.path.exists(filepath) else "",
            "output_file": output_filename,
        })

    manifest = {
        "chunk_count": len(chunks),
        "source_hash": source_hash,
        "chunks": chunks,
    }
    _validate_manifest_data(manifest, "<generated manifest>")

    manifest_path = os.path.join(temp_dir, "manifest.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Created manifest.json ({len(chunks)} chunks)")
    return manifest


def _validate_manifest_data(manifest, path):
    """Validate manifest structure and canonical, path-free chunk names."""
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: manifest root must be an object")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError(f"{path}: 'chunks' must be an array")
    if len(chunks) > MAX_MANIFEST_CHUNKS:
        raise ValueError(
            f"{path}: too many chunks ({len(chunks)} > {MAX_MANIFEST_CHUNKS})"
        )
    chunk_count = manifest.get("chunk_count")
    if chunk_count is not None and (
        isinstance(chunk_count, bool)
        or not isinstance(chunk_count, int)
        or chunk_count != len(chunks)
    ):
        raise ValueError(f"{path}: chunk_count must equal len(chunks)")

    source_hash = manifest.get("source_hash", "")
    if source_hash and (
        not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash)
    ):
        raise ValueError(f"{path}: source_hash must be an empty string or SHA-256 hex")

    seen_ids = set()
    seen_orders = set()
    for index, chunk in enumerate(chunks):
        where = f"{path}: chunks[{index}]"
        if not isinstance(chunk, dict):
            raise ValueError(f"{where} must be an object")
        chunk_id = chunk.get("id")
        source_file = chunk.get("source_file")
        output_file = chunk.get("output_file")
        order = chunk.get("order")

        if not isinstance(chunk_id, str) or not CHUNK_ID_RE.fullmatch(chunk_id):
            raise ValueError(f"{where}.id must match chunkNNNN")
        if source_file != f"{chunk_id}.md":
            raise ValueError(
                f"{where}.source_file must be exactly {chunk_id}.md"
            )
        if output_file != f"output_{chunk_id}.md":
            raise ValueError(
                f"{where}.output_file must be exactly output_{chunk_id}.md"
            )
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise ValueError(f"{where}.order must be a positive integer")
        if chunk_id in seen_ids:
            raise ValueError(f"{path}: duplicate chunk id {chunk_id!r}")
        if order in seen_orders:
            raise ValueError(f"{path}: duplicate chunk order {order}")
        seen_ids.add(chunk_id)
        seen_orders.add(order)

        chunk_hash = chunk.get("source_hash", "")
        if chunk_hash and (
            not isinstance(chunk_hash, str) or not SHA256_RE.fullmatch(chunk_hash)
        ):
            raise ValueError(
                f"{where}.source_hash must be an empty string or SHA-256 hex"
            )
    return manifest


def load_manifest(temp_dir):
    """Load manifest.json from temp_dir. Returns None if not found."""
    manifest_path = os.path.join(temp_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    if os.path.islink(manifest_path):
        raise ValueError(f"{manifest_path}: symbolic-link manifests are not allowed")
    if os.path.getsize(manifest_path) > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"{manifest_path}: manifest exceeds {MAX_MANIFEST_BYTES} bytes"
        )
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    return _validate_manifest_data(manifest, manifest_path)


def _unsafe_file_reason(temp_dir, path):
    """Return why a chunk path is unsafe, or None for a confined regular file."""
    if os.path.islink(path):
        return "symbolic links are not allowed"
    if not os.path.isfile(path):
        return "not a regular file"
    root = os.path.realpath(temp_dir)
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath([root, resolved]) != root:
            return "resolved path escapes temp directory"
    except ValueError:
        return "resolved path is on a different filesystem"
    return None


def validate_for_merge(temp_dir):
    """Validate that all chunks have been translated before merging.

    Returns (ok, ordered_output_files, warnings) where:
        ok: True if merge can proceed
        ordered_output_files: list of output file paths in order
        warnings: list of warning strings
    """
    try:
        manifest = load_manifest(temp_dir)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Invalid manifest.json: {e}")
        return False, None, []
    if manifest is None:
        # No manifest — fall back to legacy glob-based merge
        return True, None, ["No manifest.json found, using legacy merge"]

    errors = []
    warnings = []
    ordered_output_files = []

    for chunk in sorted(manifest["chunks"], key=lambda c: c["order"]):
        output_path = os.path.join(temp_dir, chunk["output_file"])
        source_path = os.path.join(temp_dir, chunk["source_file"])

        # Check source file exists — reject outputs without source chunks
        if not os.path.exists(source_path):
            errors.append(
                f"Missing source: {chunk['source_file']} (chunk {chunk['id']}) — "
                f"cannot verify output integrity without source chunk"
            )
            continue
        source_unsafe = _unsafe_file_reason(temp_dir, source_path)
        if source_unsafe:
            errors.append(
                f"Unsafe source: {chunk['source_file']} (chunk {chunk['id']}) — "
                f"{source_unsafe}"
            )
            continue

        # Check source hash matches — detect stale outputs from changed sources
        if chunk.get("source_hash"):
            current_hash = file_hash(source_path)
            if current_hash != chunk["source_hash"]:
                errors.append(
                    f"Source changed since splitting: {chunk['source_file']} "
                    f"(chunk {chunk['id']}). "
                    f"Expected hash {chunk['source_hash'][:12]}..., "
                    f"got {current_hash[:12]}... — "
                    f"delete output and re-translate, or re-run convert.py to re-split"
                )
                continue

        # Check output exists
        if not os.path.exists(output_path):
            errors.append(f"Missing output: {chunk['output_file']} (chunk {chunk['id']})")
            continue
        output_unsafe = _unsafe_file_reason(temp_dir, output_path)
        if output_unsafe:
            errors.append(
                f"Unsafe output: {chunk['output_file']} (chunk {chunk['id']}) — "
                f"{output_unsafe}"
            )
            continue

        # Check non-empty. Whitespace-only files have bytes on disk but merge
        # to nothing after strip(), silently dropping the chunk's content —
        # treat them exactly like empty files.
        output_size = os.path.getsize(output_path)
        if output_size == 0:
            errors.append(f"Empty output: {chunk['output_file']} (chunk {chunk['id']})")
            continue
        output_text = read_output_text(output_path)
        if output_text is None:
            errors.append(
                f"Unreadable output: {chunk['output_file']} (chunk {chunk['id']}) — "
                f"not valid UTF-8 text"
            )
            continue
        if not output_text.strip():
            errors.append(
                f"Blank output: {chunk['output_file']} (chunk {chunk['id']}) — "
                f"whitespace-only content would be silently dropped on merge"
            )
            continue

        # Check abnormally short
        if os.path.exists(source_path):
            source_size = os.path.getsize(source_path)
            if source_size > 0 and output_size < source_size * 0.1:
                warnings.append(
                    f"Suspiciously short: {chunk['output_file']} "
                    f"({output_size} bytes vs source {source_size} bytes)"
                )

        ordered_output_files.append(output_path)

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return False, None, warnings

    for w in warnings:
        print(f"WARNING: {w}")

    return True, ordered_output_files, warnings
