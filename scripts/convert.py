#!/usr/bin/env python3
"""
convert.py - Convert PDF/DOCX/EPUB through Calibre HTMLZ, or ingest Markdown directly.
Combines the original steps 1-2 into a single safe, resumable script.
"""

import os
import sys
import subprocess
import zipfile
import shutil
import tempfile
import argparse
import bisect
import glob
import json
import re
import stat
from pathlib import Path, PurePosixPath

from manifest import create_manifest, file_hash


MAX_HTMLZ_MEMBERS = 50_000
MAX_HTMLZ_MEMBER_BYTES = 512 * 1024 * 1024
MAX_HTMLZ_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_HTMLZ_COMPRESSION_RATIO = 200
_ZIP_COPY_BUFFER_BYTES = 1024 * 1024


def find_calibre_convert():
    """Find ebook-convert command from Calibre installation"""
    possible_paths = [
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        "/usr/bin/ebook-convert",
        "/usr/local/bin/ebook-convert",
        "ebook-convert"  # If in PATH
    ]

    for path in possible_paths:
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                print(f"Found Calibre ebook-convert: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return None


def convert_to_htmlz(input_file, htmlz_file, calibre_path):
    """Convert input file to HTMLZ using Calibre"""
    try:
        print(f"Converting {input_file} to HTMLZ...")
        cmd = [calibre_path, input_file, htmlz_file]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

        if result.returncode == 0:
            file_size = os.path.getsize(htmlz_file)
            print(f"HTMLZ conversion successful: {htmlz_file} ({file_size} bytes)")
            return True
        else:
            print(f"HTMLZ conversion failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("HTMLZ conversion timed out")
        return False
    except Exception as e:
        print(f"HTMLZ conversion error: {e}")
        return False


def extract_metadata_from_htmlz(extract_dir):
    """Extract metadata from metadata.opf file in HTMLZ"""
    try:
        import xml.etree.ElementTree as ET

        metadata_file = None
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                if file.lower() == 'metadata.opf':
                    metadata_file = os.path.join(root, file)
                    break
            if metadata_file:
                break

        if not metadata_file:
            return {}

        tree = ET.parse(metadata_file)
        root = tree.getroot()

        namespaces = {
            'opf': 'http://www.idpf.org/2007/opf',
            'dc': 'http://purl.org/dc/elements/1.1/',
            'dcterms': 'http://purl.org/dc/terms/'
        }

        metadata = {}

        title_elem = root.find('.//dc:title', namespaces)
        if title_elem is not None and title_elem.text:
            metadata['title'] = title_elem.text.strip()

        creator_elem = root.find('.//dc:creator', namespaces)
        if creator_elem is not None and creator_elem.text:
            metadata['creator'] = creator_elem.text.strip()

        publisher_elem = root.find('.//dc:publisher', namespaces)
        if publisher_elem is not None and publisher_elem.text:
            metadata['publisher'] = publisher_elem.text.strip()

        language_elem = root.find('.//dc:language', namespaces)
        if language_elem is not None and language_elem.text:
            metadata['language'] = language_elem.text.strip()

        return metadata

    except Exception as e:
        print(f"Warning: Error extracting metadata: {e}")
        return {}


def _safe_archive_target(root, member_name):
    """Resolve one ZIP member below root or reject an unsafe name."""
    if not isinstance(member_name, str) or not member_name or '\x00' in member_name:
        raise ValueError(f"unsafe empty or NUL-containing archive member: {member_name!r}")

    normalized = member_name.replace('\\', '/')
    if normalized.startswith('/') or normalized.startswith('//'):
        raise ValueError(f"absolute archive path is not allowed: {member_name!r}")
    if re.match(r'^[A-Za-z]:', normalized):
        raise ValueError(f"drive-qualified archive path is not allowed: {member_name!r}")

    parts = [part for part in PurePosixPath(normalized).parts if part not in ('', '.')]
    if not parts or any(part == '..' for part in parts):
        raise ValueError(f"path traversal is not allowed in archive member: {member_name!r}")

    root_path = Path(root).resolve()
    target = root_path.joinpath(*parts).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as e:
        raise ValueError(f"archive member escapes extraction root: {member_name!r}") from e
    return target


def _safe_extract_htmlz_archive(zip_file, temp_dir):
    """Extract a bounded HTMLZ archive without traversal or special files."""
    members = zip_file.infolist()
    if len(members) > MAX_HTMLZ_MEMBERS:
        raise ValueError(
            f"HTMLZ contains too many members ({len(members)} > {MAX_HTMLZ_MEMBERS})"
        )

    declared_total = 0
    seen_targets = set()
    extraction_plan = []
    for info in members:
        target = _safe_archive_target(temp_dir, info.filename)
        target_key = os.path.normcase(os.path.normpath(str(target)))
        if target_key in seen_targets:
            raise ValueError(f"duplicate archive destination: {info.filename!r}")
        seen_targets.add(target_key)

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise ValueError(f"symbolic links are not allowed in HTMLZ: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted HTMLZ member is not supported: {info.filename!r}")
        if info.file_size < 0 or info.file_size > MAX_HTMLZ_MEMBER_BYTES:
            raise ValueError(
                f"HTMLZ member is too large: {info.filename!r} ({info.file_size} bytes)"
            )

        declared_total += info.file_size
        if declared_total > MAX_HTMLZ_UNCOMPRESSED_BYTES:
            raise ValueError(
                "HTMLZ uncompressed size exceeds "
                f"{MAX_HTMLZ_UNCOMPRESSED_BYTES} bytes"
            )

        if (
            info.file_size > 10 * 1024 * 1024
            and info.file_size > max(info.compress_size, 1) * MAX_HTMLZ_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"suspicious compression ratio for HTMLZ member: {info.filename!r}"
            )
        extraction_plan.append((info, target))

    actual_total = 0
    for info, target in extraction_plan:
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        member_total = 0
        with zip_file.open(info, 'r') as source, open(target, 'wb') as destination:
            while True:
                block = source.read(_ZIP_COPY_BUFFER_BYTES)
                if not block:
                    break
                member_total += len(block)
                actual_total += len(block)
                if member_total > MAX_HTMLZ_MEMBER_BYTES:
                    raise ValueError(
                        f"HTMLZ member exceeded extraction limit: {info.filename!r}"
                    )
                if actual_total > MAX_HTMLZ_UNCOMPRESSED_BYTES:
                    raise ValueError("HTMLZ exceeded total extraction limit")
                destination.write(block)


def extract_htmlz(htmlz_file, temp_dir):
    """Safely extract HTMLZ and return paths to its HTML and image directory."""
    try:
        with zipfile.ZipFile(htmlz_file, 'r') as zip_file:
            _safe_extract_htmlz_archive(zip_file, temp_dir)

        html_file = None
        images_dir = None

        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.lower() in ['index.html', 'index.htm']:
                    html_file = os.path.join(root, file)
                    break
            for dir_name in dirs:
                if dir_name.lower() in ['images', 'image', 'pics', 'pictures']:
                    images_dir = os.path.join(root, dir_name)
                    break

        if not html_file:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.lower().endswith(('.html', '.htm')):
                        html_file = os.path.join(root, file)
                        break
                if html_file:
                    break

        return html_file, images_dir

    except Exception as e:
        print(f"Error extracting HTMLZ: {e}")
        return None, None


def build_temp_dir(input_file, temp_root=None):
    """Return the working directory path for an input file.

    Default is the historical cwd-local {book_name}_temp/. When temp_root is
    provided, only the root changes; the leaf directory name stays compatible.
    """
    base_name = os.path.splitext(os.path.basename(input_file))[0]
    leaf = f"{base_name}_temp"
    if temp_root:
        return os.path.join(temp_root, leaf)
    return leaf


SOURCE_FINGERPRINT_FILE = "source_fingerprint.json"

# Files whose presence means the temp dir carries conversion state derived
# from some source book — and must therefore be tied to the current one.
_SOURCE_CACHE_MARKERS = (
    "input.html",
    "input.md",
    "manifest.json",
    "run_state.json",
    "glossary.json",
    "output.md",
)


def source_fingerprint(input_file):
    """Stable identity of the exact source bytes being converted."""
    return {
        "path": os.path.realpath(input_file),
        "size": os.path.getsize(input_file),
        "sha256": file_hash(input_file),
    }


def _write_source_fingerprint(temp_dir, fingerprint):
    os.makedirs(temp_dir, exist_ok=True)
    path = os.path.join(temp_dir, SOURCE_FINGERPRINT_FILE)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(fingerprint, f, indent=2, sort_keys=True)
        f.write('\n')


def _load_source_fingerprint(temp_dir):
    path = os.path.join(temp_dir, SOURCE_FINGERPRINT_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _has_reusable_source_cache(temp_dir):
    if not os.path.isdir(temp_dir):
        return False
    for name in _SOURCE_CACHE_MARKERS:
        if os.path.exists(os.path.join(temp_dir, name)):
            return True
    for pattern in ('chunk*.md', 'output_chunk*.md'):
        if glob.glob(os.path.join(temp_dir, pattern)):
            return True
    return False


def check_source_cache(temp_dir, current_fingerprint):
    """Compare the temp dir's cached source identity against the current input.

    Returns (status, message):
      (None, None)        — fresh temp dir, or fingerprint matches; proceed.
      ('adopt', message)  — cache predates fingerprinting; adopt it and record
                            the current fingerprint (trust-on-first-use, keeps
                            pre-upgrade temp dirs resumable).
      ('mismatch', message) — cache was built from different source bytes;
                              the caller must abort.

    Only content identity (sha256 + size) is compared — moving or renaming the
    source file does not invalidate the cache.
    """
    if not _has_reusable_source_cache(temp_dir):
        return None, None

    stored = _load_source_fingerprint(temp_dir)
    if stored is None:
        return 'adopt', (
            f"{temp_dir}/ contains cached conversion artifacts without a "
            f"{SOURCE_FINGERPRINT_FILE} (created by an older version). "
            f"Assuming they were built from the current input file. "
            f"If you replaced the source file, delete {temp_dir}/ and re-run."
        )

    for key in ("sha256", "size"):
        if stored.get(key) != current_fingerprint.get(key):
            return 'mismatch', (
                f"{temp_dir}/ was created from different source bytes "
                f"(cached sha256 {str(stored.get('sha256', ''))[:12]}..., "
                f"current {current_fingerprint['sha256'][:12]}...). "
                f"Reusing its chunks would translate the wrong book."
            )
    return None, None


def _abort_on_source_cache_mismatch(status, message, temp_dir):
    if status == 'mismatch':
        print(f"Error: {message}")
        print(f"Delete {temp_dir}/ (or use a fresh --temp-root) and re-run.")
        sys.exit(1)
    if status == 'adopt':
        print(f"Warning: {message}")


def setup_temp_directory(input_file, html_file, images_dir, temp_root=None):
    """Setup temp directory with HTML and images"""
    try:
        temp_dir = build_temp_dir(input_file, temp_root)
        os.makedirs(temp_dir, exist_ok=True)

        input_html = os.path.join(temp_dir, "input.html")
        if os.path.exists(input_html):
            print(f"Skipping HTML copy - input.html already exists")
        else:
            shutil.copy2(html_file, input_html)
            print(f"Copied HTML to: {input_html}")

        if images_dir and os.path.exists(images_dir):
            target_images_dir = os.path.join(temp_dir, "images")
            if os.path.exists(target_images_dir):
                print(f"Skipping images copy - images directory already exists")
            else:
                shutil.copytree(images_dir, target_images_dir)
                print(f"Copied images to: {target_images_dir}")

        return temp_dir
    except Exception as e:
        print(f"Error setting up temp directory: {e}")
        return None


_CALIBRE_ELEMENT_ID_RE = re.compile(
    r'(?P<open><[A-Za-z][^<>]*?)\s+id=(?P<quote>["\'])'
    r'(?P<id>calibre_link-\d+)(?P=quote)(?P<tail>[^<>]*>)',
    re.IGNORECASE,
)
_CALIBRE_LINK_TARGET_RE = re.compile(r'\]\(#(?P<id>calibre_link-\d+)\)')
_CALIBRE_MARKDOWN_ANCHOR_RE = re.compile(r'\{#(?P<id>calibre_link-\d+)(?:[\s}])')
_CALIBRE_HTML_ANCHOR_RE = re.compile(
    r'\bid=["\'](?P<id>calibre_link-\d+)["\']', re.IGNORECASE
)


def _prepare_calibre_anchor_html(html_file):
    """Move Calibre wrapper IDs onto explicit empty spans Pandoc preserves."""
    source = Path(html_file)
    content = source.read_text(encoding="utf-8")

    def replace(match):
        return (
            match.group("open")
            + match.group("tail")
            + f'<span id="{match.group("id")}"></span>'
        )

    prepared, count = _CALIBRE_ELEMENT_ID_RE.subn(replace, content)
    if count == 0:
        return str(source), None

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        suffix=".html",
        prefix=".pandoc-anchors-",
        dir=source.parent,
        delete=False,
    )
    try:
        with handle:
            handle.write(prepared)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return handle.name, Path(handle.name)


def _missing_calibre_link_targets(markdown):
    links = {match.group("id") for match in _CALIBRE_LINK_TARGET_RE.finditer(markdown)}
    anchors = {
        match.group("id") for match in _CALIBRE_MARKDOWN_ANCHOR_RE.finditer(markdown)
    }
    anchors.update(
        match.group("id") for match in _CALIBRE_HTML_ANCHOR_RE.finditer(markdown)
    )
    return sorted(links - anchors)


def convert_html_to_markdown(html_file, md_file, strip_page_numbers=False):
    """Convert HTML to Markdown using pandoc"""
    try:
        import pypandoc

        prepared_html, temporary_html = _prepare_calibre_anchor_html(html_file)
        try:
            pypandoc.convert_file(
                prepared_html,
                'markdown-smart',
                outputfile=md_file,
                extra_args=['--wrap=none']
            )
        finally:
            if temporary_html is not None:
                temporary_html.unlink(missing_ok=True)

        if os.path.exists(md_file):
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()

            content = content.replace('\ufeff', '')
            content = content.replace('\u00a0', ' ')
            content = clean_calibre_markers(content, strip_page_numbers=strip_page_numbers)

            missing_targets = _missing_calibre_link_targets(content)
            if missing_targets:
                raise ValueError(
                    "converted Markdown lost internal Calibre anchor target(s): "
                    + ", ".join(missing_targets[:20])
                )

            with open(md_file, 'w', encoding='utf-8') as f:
                f.write(content)

            print(f"Markdown conversion successful: {md_file}")
            return True
        else:
            print("Markdown file was not created")
            return False
    except ImportError:
        print("pypandoc not found. Install with: pip install pypandoc")
        return False
    except Exception as e:
        print(f"HTML to Markdown conversion failed: {e}")
        return False


_PAGE_SEQUENCE_MIN_LENGTH = 4
_PAGE_SEQUENCE_MIN_RATIO = 0.5


def _detect_page_number_lines(lines):
    """Detect standalone-digit lines that form a monotonic page-number sequence.

    Returns a set of line indices that should be dropped as page numbers.

    Algorithm: collect every standalone-digit line in document order, find the
    Longest Non-Decreasing Subsequence (LNDS) of their integer values via
    bisect_right with parent-pointer reconstruction. If the LNDS is long enough
    and covers a large enough fraction of all standalone digits, treat those
    elements as page numbers. Outliers (years like 1984, chapter numbers,
    citation indices) sit off the monotonic spine and stay preserved.
    """
    digit_indices = []
    digit_values = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.isdigit():
            digit_indices.append(i)
            digit_values.append(int(s))

    n = len(digit_values)
    if n < _PAGE_SEQUENCE_MIN_LENGTH:
        return set()

    tails = []
    tails_idx = []
    parents = [-1] * n

    for i, v in enumerate(digit_values):
        pos = bisect.bisect_right(tails, v)
        if pos > 0:
            parents[i] = tails_idx[pos - 1]
        if pos == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[pos] = v
            tails_idx[pos] = i

    lnds = []
    cur = tails_idx[-1]
    while cur != -1:
        lnds.append(cur)
        cur = parents[cur]
    lnds.reverse()

    if len(lnds) < _PAGE_SEQUENCE_MIN_LENGTH:
        return set()
    if len(lnds) / n < _PAGE_SEQUENCE_MIN_RATIO:
        return set()

    return {digit_indices[i] for i in lnds}


def clean_calibre_markers(content, strip_page_numbers=False):
    """Clean up Calibre-specific markers from markdown content.

    Standalone digit lines are handled in two layers:
      1. If a line is adjacent to Calibre noise (::: fence, .ct}/.cn} marker),
         drop it — clearly leftover.
      2. Otherwise, run LNDS over all standalone digits to detect a monotonic
         page-number sequence and drop those. Outliers like years (1984),
         chapter numbers, and citation indices stay preserved.

    Pass strip_page_numbers=True to bypass both layers and aggressively delete
    every standalone-digit line (legacy behavior).
    """
    content = re.sub(r'\{\.calibre[^}]*\}', '', content)
    # Remove only orphaned Calibre tokens. A token immediately following a
    # Markdown link label is its real destination and must remain navigable.
    content = re.sub(r'(?<!\])\s*\(#calibre_link-\d+\)', '', content)

    # Drop generated Calibre classes while preserving IDs referenced by links.
    content = re.sub(
        r'\{(#calibre_link-\d+)(?:\s+\.calibre[\w-]*)+\}',
        r'{\1}',
        content,
    )

    # Clean [**text**] format to **text**
    content = re.sub(r'\[\*\*([^*]+)\*\*\]', r'**\1**', content)

    lines = content.split('\n')

    page_number_lines = set() if strip_page_numbers else _detect_page_number_lines(lines)

    def is_calibre_noise(line):
        s = line.strip()
        if not s:
            return False
        if s.startswith(':::'):
            return True
        if s.endswith('.ct}') or s.endswith('.cn}'):
            return True
        return False

    def prev_nonblank(idx):
        for j in range(idx - 1, -1, -1):
            if lines[j].strip():
                return lines[j]
        return None

    def next_nonblank(idx):
        for j in range(idx + 1, len(lines)):
            if lines[j].strip():
                return lines[j]
        return None

    cleaned_lines = []
    for i, line in enumerate(lines):
        stripped_line = line.strip()

        if stripped_line.startswith(':::'):
            continue
        if stripped_line.endswith('.ct}') or stripped_line.endswith('.cn}'):
            continue

        if re.match(r'^\s*\d+\s*$', line):
            if strip_page_numbers:
                continue
            if i in page_number_lines:
                continue
            prev = prev_nonblank(i)
            nxt = next_nonblank(i)
            if (prev is not None and is_calibre_noise(prev)) or \
               (nxt is not None and is_calibre_noise(nxt)):
                continue
            # else: preserve as real content

        cleaned_lines.append(line)

    content = '\n'.join(cleaned_lines)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content


# =============================================================================
# Structural block parsing and chunk splitting (Step 3)
# =============================================================================

def parse_structural_blocks(content):
    """Parse markdown into structural blocks that should not be split.

    Returns list of (text, block_type) tuples where block_type is one of:
    'heading', 'code_block', 'table', 'list', 'blockquote', 'image', 'paragraph'
    """
    blocks = []
    lines = content.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code block (fenced)
        if stripped.startswith('```'):
            block_lines = [line]
            i += 1
            while i < len(lines):
                block_lines.append(lines[i])
                if lines[i].strip().startswith('```') and len(block_lines) > 1:
                    i += 1
                    break
                i += 1
            blocks.append(('\n'.join(block_lines), 'code_block'))
            continue

        # Heading
        if re.match(r'^#{1,6}\s', stripped):
            blocks.append((line, 'heading'))
            i += 1
            continue

        # Blockquote
        if stripped.startswith('>'):
            block_lines = [line]
            i += 1
            while i < len(lines) and (lines[i].strip().startswith('>') or
                                       (lines[i].strip() and not re.match(r'^#{1,6}\s', lines[i].strip())
                                        and not lines[i].strip().startswith('```')
                                        and not lines[i].strip().startswith('|')
                                        and not re.match(r'^[-*+]\s', lines[i].strip())
                                        and not re.match(r'^\d+\.\s', lines[i].strip())
                                        and block_lines[-1].strip().startswith('>'))):
                block_lines.append(lines[i])
                i += 1
            blocks.append(('\n'.join(block_lines), 'blockquote'))
            continue

        # Table (lines starting with |)
        if stripped.startswith('|'):
            block_lines = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith('|'):
                block_lines.append(lines[i])
                i += 1
            blocks.append(('\n'.join(block_lines), 'table'))
            continue

        # List (unordered or ordered)
        if re.match(r'^[-*+]\s', stripped) or re.match(r'^\d+\.\s', stripped):
            block_lines = [line]
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                # Continue list: list items, indented continuation, or blank lines within list
                if (re.match(r'^[-*+]\s', s) or re.match(r'^\d+\.\s', s) or
                        (lines[i].startswith('  ') and s) or
                        (s == '' and i + 1 < len(lines) and
                         (re.match(r'^[-*+]\s', lines[i+1].strip()) or
                          re.match(r'^\d+\.\s', lines[i+1].strip()) or
                          lines[i+1].startswith('  ')))):
                    block_lines.append(lines[i])
                    i += 1
                else:
                    break
            blocks.append(('\n'.join(block_lines), 'list'))
            continue

        # Image line (standalone or with surrounding caption)
        if re.match(r'!\[', stripped):
            blocks.append((line, 'image'))
            i += 1
            continue

        # Empty line — just a paragraph separator
        if stripped == '':
            blocks.append((line, 'paragraph'))
            i += 1
            continue

        # Regular paragraph — collect contiguous non-empty, non-special lines
        block_lines = [line]
        i += 1
        while i < len(lines):
            s = lines[i].strip()
            if (s == '' or s.startswith('```') or re.match(r'^#{1,6}\s', s) or
                    s.startswith('>') or s.startswith('|') or
                    re.match(r'^[-*+]\s', s) or re.match(r'^\d+\.\s', s) or
                    re.match(r'!\[', s)):
                break
            block_lines.append(lines[i])
            i += 1
        blocks.append(('\n'.join(block_lines), 'paragraph'))
        continue

    return blocks


def merge_blocks_to_chunks(blocks, target_size=6000):
    """Merge structural blocks into chunks respecting target_size.

    Prefers to split at heading boundaries. Never splits within a single
    structural block unless the block itself exceeds target_size * 2.
    """
    chunks = []
    current_parts = []
    current_size = 0

    def flush():
        nonlocal current_parts, current_size
        if current_parts:
            chunks.append('\n'.join(current_parts))
            current_parts = []
            current_size = 0

    for text, btype in blocks:
        block_size = len(text)

        # If a single block is oversized, handle degradation
        if block_size > target_size * 2:
            flush()
            print(f"  WARNING: Oversized {btype} block ({block_size} chars), force-splitting")
            sub_chunks = _force_split_block(text, target_size)
            chunks.extend(sub_chunks)
            continue

        # Prefer to split at heading boundaries
        if btype == 'heading' and current_size > 0:
            flush()

        # Would adding this block exceed target?
        if current_size + block_size > target_size and current_parts:
            flush()

        current_parts.append(text)
        current_size += block_size

    flush()
    return chunks


def _force_split_block(text, target_size):
    """Force-split an oversized block by paragraph (empty lines), then by lines.

    For fenced code blocks, each resulting chunk gets proper opening/closing fences
    so it remains valid Markdown.
    """
    stripped = text.strip()
    is_fenced_code = stripped.startswith('```')

    # Extract fence info for code blocks
    fence_opener = ''
    if is_fenced_code:
        first_line = stripped.split('\n', 1)[0]
        fence_opener = first_line  # e.g. "```python"

    # Try splitting by empty lines first (not applicable for code blocks — no empty lines expected)
    if not is_fenced_code:
        paragraphs = re.split(r'\n\n+', text)
        if len(paragraphs) > 1:
            chunks = []
            current = []
            current_size = 0
            for para in paragraphs:
                para_size = len(para)
                if current_size + para_size > target_size and current:
                    chunks.append('\n\n'.join(current))
                    current = [para]
                    current_size = para_size
                else:
                    current.append(para)
                    current_size += para_size
            if current:
                chunks.append('\n\n'.join(current))
            return chunks

    # Split by lines
    lines = text.split('\n')

    # For code blocks, strip the opening and closing fences before splitting content
    if is_fenced_code:
        # Remove opening fence line
        content_lines = lines[1:]
        # Remove closing fence line if present
        if content_lines and content_lines[-1].strip().startswith('```'):
            content_lines = content_lines[:-1]
        lines = content_lines

    chunks = []
    current = []
    current_size = 0
    for line in lines:
        line_size = len(line) + 1
        if current_size + line_size > target_size and current:
            chunks.append('\n'.join(current))
            current = [line]
            current_size = line_size
        else:
            current.append(line)
            current_size += line_size
    if current:
        chunks.append('\n'.join(current))

    # Re-wrap each chunk in fences for code blocks
    if is_fenced_code:
        chunks = [f"{fence_opener}\n{chunk}\n```" for chunk in chunks]

    return chunks


def split_markdown_structured(md_file, temp_dir, target_size=6000):
    """Split markdown into structural chunks.

    Returns list of chunk filenames (e.g. ['chunk0001.md', ...]).
    """
    try:
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()

        blocks = parse_structural_blocks(content)
        chunk_texts = merge_blocks_to_chunks(blocks, target_size)

        chunk_files = []
        for i, chunk_text in enumerate(chunk_texts, 1):
            filename = f"chunk{i:04d}.md"
            chunk_file = os.path.join(temp_dir, filename)
            with open(chunk_file, 'w', encoding='utf-8') as f:
                f.write(chunk_text)
            chunk_files.append(filename)

        print(f"Split into {len(chunk_files)} chunks")
        for filename in chunk_files:
            filepath = os.path.join(temp_dir, filename)
            size = os.path.getsize(filepath)
            print(f"  {filename}: {size} characters")

        return chunk_files
    except Exception as e:
        print(f"Error splitting markdown: {e}")
        return []


def _find_existing_chunk_files(temp_dir):
    """Find existing chunk source filenames (excluding output_ prefixed), sorted."""
    chunk_files = glob.glob(os.path.join(temp_dir, 'chunk*.md'))
    chunk_files = [os.path.basename(f) for f in chunk_files if not os.path.basename(f).startswith('output_')]
    return sorted(chunk_files)


def create_config_file(
    temp_dir,
    input_file,
    input_lang,
    output_lang,
    metadata=None,
    conversion_method="calibre_htmlz",
):
    """Create config.txt file for the pipeline"""
    try:
        config_file = os.path.join(temp_dir, "config.txt")

        config_content = f"""# Translation Configuration
input_file={input_file}
input_lang={input_lang}
output_lang={output_lang}
conversion_method={conversion_method}
"""
        if metadata:
            config_content += f"\n# Book Metadata\n"
            if 'title' in metadata:
                config_content += f"original_title={metadata['title']}\n"
            if 'creator' in metadata:
                config_content += f"creator={metadata['creator']}\n"
            if 'publisher' in metadata:
                config_content += f"publisher={metadata['publisher']}\n"
            if 'language' in metadata:
                config_content += f"source_language={metadata['language']}\n"

        with open(config_file, 'w', encoding='utf-8') as f:
            f.write(config_content)

        print(f"Created config file: {config_file}")
        return True
    except Exception as e:
        print(f"Error creating config file: {e}")
        return False


def _do_split_and_manifest(temp_dir, input_md, chunk_size):
    """Split markdown and create manifest. Returns chunk count or 0 on failure."""
    existing = _find_existing_chunk_files(temp_dir)
    if existing:
        print(f"Skipping markdown splitting - found {len(existing)} existing chunk files")
        # Create/update manifest for existing files
        create_manifest(temp_dir, existing, input_md)
        return len(existing)

    chunk_files = split_markdown_structured(input_md, temp_dir, chunk_size)
    if not chunk_files:
        return 0
    create_manifest(temp_dir, chunk_files, input_md)
    return len(chunk_files)


def _copy_direct_markdown(input_file, input_md):
    """Validate and atomically copy a user-supplied UTF-8 Markdown source."""
    source = Path(input_file)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Markdown input must be an ordinary non-symlink file")
    payload = source.read_bytes()
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown input must be valid UTF-8") from exc

    destination = Path(input_md)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=".input-md-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _check_strip_page_numbers_cache_conflict(strip_flag, temp_dir, input_md):
    """Return list of cached files that would silently neutralize --strip-page-numbers.

    The flag only takes effect inside clean_calibre_markers, which runs during
    HTML→Markdown conversion. If input.md or chunk*.md already exist from a
    prior run, both are reused as-is and the flag becomes a no-op. Surface
    that conflict so the user knows to clean up.
    """
    if not strip_flag:
        return []
    if not os.path.isdir(temp_dir):
        return []

    blockers = []
    if os.path.exists(input_md):
        blockers.append(input_md)

    existing_chunks = [
        f for f in glob.glob(os.path.join(temp_dir, 'chunk*.md'))
        if not os.path.basename(f).startswith('output_')
    ]
    if existing_chunks:
        blockers.append(f"{len(existing_chunks)} chunk file(s) under {temp_dir}/")

    return blockers


def _abort_on_strip_cache_conflict(blockers, temp_dir):
    if not blockers:
        return
    print("Error: --strip-page-numbers cannot take effect because cached files exist:")
    for b in blockers:
        print(f"  - {b}")
    print(f"Delete the cached files (or remove the entire {temp_dir}/ directory) and re-run.")
    sys.exit(1)


def main():
    """Main conversion function"""
    parser = argparse.ArgumentParser(description="Convert PDF/DOCX/EPUB/Markdown to Markdown chunks")
    parser.add_argument("input_file", help="Input file (PDF, DOCX, EPUB, MD, or Markdown)")
    parser.add_argument("-l", "--ilang", default="auto", help="Input language (default: auto)")
    parser.add_argument("--olang", default="zh", help="Output language (default: zh)")
    parser.add_argument("--chunk-size", type=int, default=6000, help="Target chunk size in characters (default: 6000)")
    parser.add_argument(
        "--temp-root",
        default=None,
        help="Directory under which {book_name}_temp/ will be created (default: current working directory)",
    )
    parser.add_argument(
        "--strip-page-numbers",
        action="store_true",
        help="Aggressively delete every standalone-digit line (legacy behavior). "
             "Default is off: standalone digits are preserved unless adjacent to Calibre noise.",
    )

    args = parser.parse_args()
    input_file = args.input_file

    if args.chunk_size <= 0:
        print("Error: --chunk-size must be a positive integer")
        sys.exit(1)

    if not os.path.exists(input_file):
        print(f"Error: Input file not found: {input_file}")
        sys.exit(1)
    if Path(input_file).is_symlink() or not Path(input_file).is_file():
        print(f"Error: Input must be an ordinary non-symlink file: {input_file}")
        sys.exit(1)

    file_ext = os.path.splitext(input_file)[1].lower()
    if file_ext not in ['.pdf', '.docx', '.epub', '.md', '.markdown']:
        print(f"Error: Unsupported file type: {file_ext}")
        sys.exit(1)

    print("=== File Conversion via Calibre HTMLZ ===")
    print(f"Input file: {input_file}")
    print(f"Target chunk size: {args.chunk_size} characters")
    if args.temp_root:
        print(f"Temp root: {args.temp_root}")

    if file_ext in {'.md', '.markdown'}:
        if args.strip_page_numbers:
            print("Error: --strip-page-numbers is not valid for direct Markdown input")
            sys.exit(1)
        try:
            temp_dir = build_temp_dir(input_file, args.temp_root)
            current_fingerprint = source_fingerprint(input_file)
            _abort_on_source_cache_mismatch(
                *check_source_cache(temp_dir, current_fingerprint), temp_dir=temp_dir
            )
            os.makedirs(temp_dir, exist_ok=True)
            input_md = os.path.join(temp_dir, "input.md")
            if os.path.exists(input_md):
                print("Skipping direct Markdown copy - input.md already exists")
            else:
                _copy_direct_markdown(input_file, input_md)
            chunk_count = _do_split_and_manifest(temp_dir, input_md, args.chunk_size)
            if chunk_count == 0:
                sys.exit(1)
            metadata = {"title": Path(input_file).stem}
            create_config_file(
                temp_dir,
                input_file,
                args.ilang,
                args.olang,
                metadata,
                conversion_method="direct_markdown",
            )
            _write_source_fingerprint(temp_dir, current_fingerprint)
            print("Conversion completed successfully!")
            print(f"Temp directory: {temp_dir}")
            print(f"Markdown chunks: {chunk_count} files")
            return
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}")
            sys.exit(1)

    calibre_path = find_calibre_convert()
    if not calibre_path:
        print("Error: Calibre ebook-convert not found")
        print("Please install Calibre: https://calibre-ebook.com/")
        sys.exit(1)

    htmlz_workspace = None
    htmlz_file = None

    try:
        temp_dir = build_temp_dir(input_file, args.temp_root)
        current_fingerprint = source_fingerprint(input_file)
        _abort_on_source_cache_mismatch(
            *check_source_cache(temp_dir, current_fingerprint), temp_dir=temp_dir
        )
        input_html_path = os.path.join(temp_dir, "input.html")

        if os.path.exists(input_html_path):
            print(f"Skipping HTMLZ conversion - input.html already exists")

            metadata = {}
            config_file = os.path.join(temp_dir, "config.txt")
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            if '=' in line:
                                key, value = line.strip().split('=', 1)
                                if key == 'original_title':
                                    metadata['title'] = value
                                elif key == 'creator':
                                    metadata['creator'] = value
                                elif key == 'publisher':
                                    metadata['publisher'] = value
                                elif key == 'source_language':
                                    metadata['language'] = value
                except Exception as e:
                    print(f"Warning: Could not read metadata from config: {e}")

            input_md = os.path.join(temp_dir, "input.md")
            _abort_on_strip_cache_conflict(
                _check_strip_page_numbers_cache_conflict(args.strip_page_numbers, temp_dir, input_md),
                temp_dir,
            )
            if os.path.exists(input_md):
                print(f"Skipping HTML to Markdown conversion - input.md already exists")
            else:
                if not convert_html_to_markdown(input_html_path, input_md, strip_page_numbers=args.strip_page_numbers):
                    sys.exit(1)

            chunk_count = _do_split_and_manifest(temp_dir, input_md, args.chunk_size)
            if chunk_count == 0:
                sys.exit(1)

            create_config_file(temp_dir, input_file, args.ilang, args.olang, metadata)
            _write_source_fingerprint(temp_dir, current_fingerprint)
            print("Conversion completed successfully!")
            print(f"Temp directory: {temp_dir}")
            return

        # Keep the intermediate archive in a unique directory we own. The old
        # source-adjacent `<book>.htmlz` path could overwrite and then delete a
        # pre-existing user file with the same name.
        htmlz_workspace = tempfile.TemporaryDirectory(prefix="translate-book-htmlz-")
        htmlz_file = os.path.join(htmlz_workspace.name, "input.htmlz")

        if not convert_to_htmlz(input_file, htmlz_file, calibre_path):
            sys.exit(1)

        with tempfile.TemporaryDirectory() as extract_dir:
            html_file, images_dir = extract_htmlz(htmlz_file, extract_dir)
            if not html_file:
                sys.exit(1)

            metadata = extract_metadata_from_htmlz(extract_dir)

            temp_dir = setup_temp_directory(input_file, html_file, images_dir, temp_root=args.temp_root)
            if not temp_dir:
                sys.exit(1)

            input_html = os.path.join(temp_dir, "input.html")
            input_md = os.path.join(temp_dir, "input.md")

            _abort_on_strip_cache_conflict(
                _check_strip_page_numbers_cache_conflict(args.strip_page_numbers, temp_dir, input_md),
                temp_dir,
            )
            if os.path.exists(input_md):
                print(f"Skipping HTML to Markdown conversion - input.md already exists")
            else:
                if not convert_html_to_markdown(input_html, input_md, strip_page_numbers=args.strip_page_numbers):
                    sys.exit(1)

            chunk_count = _do_split_and_manifest(temp_dir, input_md, args.chunk_size)
            if chunk_count == 0:
                sys.exit(1)

            create_config_file(temp_dir, input_file, args.ilang, args.olang, metadata)
            _write_source_fingerprint(temp_dir, current_fingerprint)

            print("Conversion completed successfully!")
            print(f"Temp directory: {temp_dir}")
            print(f"Markdown chunks: {chunk_count} files")

    except KeyboardInterrupt:
        print("\nConversion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        if htmlz_workspace is not None:
            htmlz_workspace.cleanup()


if __name__ == "__main__":
    main()
