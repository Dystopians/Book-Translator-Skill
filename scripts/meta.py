#!/usr/bin/env python3
"""
meta.py - Per-chunk sub-agent observation file.

Each translation sub-agent emits an `output_chunk<NNNN>.meta.json` alongside
its translated chunk. The main agent reads these after each batch and merges
them into the canonical glossary (see merge_meta.py).

The schema (v1):

    {
      "schema_version": 1,
      "new_entities":         [{"source": "...", "target_proposal": "...",
                                "category": "...", "evidence": "..."}],
      "alias_hypotheses":     [{"variant": "...", "may_be_alias_of_source": "...",
                                "evidence": "..."}],
      "attribute_hypotheses": [{"entity_source": "...", "attribute": "...",
                                "value": "...", "confidence": "...",
                                "evidence": "..."}],
      "used_term_sources":    ["..."],
      "conflicts":            [{"entity_source": "...", "field": "...",
                                "injected": "...", "observed_better": "...",
                                "evidence": "..."}]
    }

`chunk_id` is intentionally NOT a field — chunk identity is derived from the
filename (`output_chunk<NNNN>.meta.json` → `chunk<NNNN>`). Letting the
sub-agent fill it would create a hallucination hole.
"""

import hashlib
import json
import os
import re
import tempfile


META_SCHEMA_VERSION = 1
LATEST_META_SCHEMA_VERSION = 2
SUPPORTED_META_SCHEMA_VERSIONS = (META_SCHEMA_VERSION, LATEST_META_SCHEMA_VERSION)
EVIDENCE_MAX_LEN = 500
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_ARRAY_ITEMS = 100
MAX_TEXT_LEN = 4096

VALID_V1_TOP_LEVEL_KEYS = frozenset({
    'schema_version',
    'new_entities',
    'alias_hypotheses',
    'attribute_hypotheses',
    'used_term_sources',
    'conflicts',
})

VALID_V2_TOP_LEVEL_KEYS = VALID_V1_TOP_LEVEL_KEYS | frozenset({
    'memory_dependency_hash',
    'used_memory_ids',
    'new_terms',
    'new_facts',
    'new_claims',
    'unresolved',
    'segment_translations',
})

VALID_MEMORY_KINDS = frozenset({
    'terms', 'facts', 'claims', 'style_rules', 'resolutions',
})
VALID_POLARITIES = frozenset({'affirmed', 'negated', 'mixed', 'unknown'})
VALID_MODALITIES = frozenset({
    'certain', 'necessary', 'probable', 'possible', 'conditional', 'reported', 'unknown',
})
VALID_IMPACTS = frozenset({'critical', 'high', 'medium', 'low'})
VALID_EVIDENCE_BASES = frozenset({
    'explicit_definition', 'book_usage', 'trusted_user_source', 'model_inference',
})

VALID_ATTRIBUTE_CONFIDENCES = ('low', 'medium', 'high')

_CHUNK_ID_RE = re.compile(r'^output_(chunk\d+)\.meta\.json$')


def chunk_id_from_meta_path(path):
    """Extract the chunk identifier from a meta filename.

    Filename is the authoritative source of chunk identity — never trust the
    payload to provide it.
    """
    basename = os.path.basename(path)
    m = _CHUNK_ID_RE.match(basename)
    if not m:
        raise ValueError(
            f"Meta filename {basename!r} does not match expected pattern "
            f"'output_chunk<NNNN>.meta.json'. Cannot derive chunk_id."
        )
    return m.group(1)


def _canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def meta_content_hash(data):
    """SHA-256 of canonical JSON. Used by merge_meta.py to detect whether a
    given meta has already been applied to the glossary."""
    return hashlib.sha256(_canonical_json(data).encode('utf-8')).hexdigest()


def _check_evidence(value, where, path, schema_version=1):
    if schema_version == 1:
        if not isinstance(value, str):
            raise ValueError(
                f"Meta at {path}: 'evidence' in {where} must be a string, "
                f"got {type(value).__name__}"
            )
        quote = value
    else:
        if not isinstance(value, dict) or set(value) != {'segment_id', 'quote'}:
            raise ValueError(
                f"Meta at {path}: v2 evidence in {where} must be exactly "
                "{'segment_id': ..., 'quote': ...}"
            )
        _require_str(value['segment_id'], 'segment_id', where, path)
        if not value['segment_id'] or len(value['segment_id']) > 128:
            raise ValueError(f"Meta at {path}: invalid segment_id in {where}")
        if not isinstance(value['quote'], str):
            raise ValueError(f"Meta at {path}: evidence quote in {where} must be a string")
        quote = value['quote']
    if len(quote) > EVIDENCE_MAX_LEN:
        raise ValueError(
            f"Meta at {path}: 'evidence' in {where} is {len(quote)} chars; "
            f"limit is {EVIDENCE_MAX_LEN}. Quote a shorter excerpt."
        )


def _require_str(value, field, where, path):
    if not isinstance(value, str):
        raise ValueError(
            f"Meta at {path}: {where} field {field!r} must be a string, "
            f"got {type(value).__name__}"
        )
    if len(value) > MAX_TEXT_LEN:
        raise ValueError(
            f"Meta at {path}: {where} field {field!r} exceeds {MAX_TEXT_LEN} characters"
        )


def _validate_array(data, key, path):
    val = data.get(key, [])
    if not isinstance(val, list):
        raise ValueError(
            f"Meta at {path}: {key!r} must be a list, got {type(val).__name__}"
        )
    if len(val) > MAX_ARRAY_ITEMS:
        raise ValueError(
            f"Meta at {path}: {key!r} has {len(val)} items; limit is {MAX_ARRAY_ITEMS}"
        )
    return val


def _require_exact_keys(value, required, optional, where, path):
    if not isinstance(value, dict):
        raise ValueError(f"Meta at {path}: {where} must be an object")
    keys = set(value)
    missing = set(required) - keys
    extras = keys - set(required) - set(optional)
    if missing:
        raise ValueError(f"Meta at {path}: {where} missing field(s) {sorted(missing)!r}")
    if extras:
        raise ValueError(f"Meta at {path}: {where} has unknown field(s) {sorted(extras)!r}")


def _validate_v2_extensions(data, path):
    dependency_hash = data.get('memory_dependency_hash')
    if not isinstance(dependency_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', dependency_hash):
        raise ValueError(
            f"Meta at {path}: v2 'memory_dependency_hash' must be a SHA-256 hex string"
        )

    used = data.get('used_memory_ids')
    if not isinstance(used, dict):
        raise ValueError(f"Meta at {path}: v2 'used_memory_ids' must be an object")
    extras = set(used) - VALID_MEMORY_KINDS
    if extras:
        raise ValueError(f"Meta at {path}: unknown used_memory_ids keys {sorted(extras)!r}")
    for kind in VALID_MEMORY_KINDS:
        ids = used.get(kind, [])
        if not isinstance(ids, list) or len(ids) > MAX_ARRAY_ITEMS:
            raise ValueError(f"Meta at {path}: used_memory_ids.{kind} must be a bounded list")
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in ids):
            raise ValueError(f"Meta at {path}: used_memory_ids.{kind} contains an invalid id")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Meta at {path}: used_memory_ids.{kind} contains duplicates")

    for index, term in enumerate(_validate_array(data, 'new_terms', path)):
        where = f"new_terms #{index}"
        _require_exact_keys(
            term,
            {'surface', 'sense', 'target_proposal', 'evidence'},
            {'category', 'domain', 'usage_note', 'forbidden_variants', 'evidence_basis'},
            where,
            path,
        )
        for field in ('surface', 'sense', 'target_proposal'):
            _require_str(term[field], field, where, path)
        for field in ('category', 'domain', 'usage_note'):
            if field in term:
                _require_str(term[field], field, where, path)
        forbidden = term.get('forbidden_variants', [])
        if not isinstance(forbidden, list) or len(forbidden) > MAX_ARRAY_ITEMS:
            raise ValueError(f"Meta at {path}: {where}.forbidden_variants must be a bounded list")
        for item in forbidden:
            _require_str(item, 'forbidden_variants', where, path)
        _check_evidence(term['evidence'], where, path, 2)
        if term.get('evidence_basis', 'book_usage') not in VALID_EVIDENCE_BASES:
            raise ValueError(f"Meta at {path}: invalid evidence_basis in {where}")

    for index, fact in enumerate(_validate_array(data, 'new_facts', path)):
        where = f"new_facts #{index}"
        _require_exact_keys(
            fact,
            {'subject', 'predicate', 'object', 'polarity', 'modality', 'scope', 'evidence'},
            {'evidence_basis'},
            where,
            path,
        )
        for field in ('subject', 'predicate', 'object', 'scope'):
            _require_str(fact[field], field, where, path)
        if fact['polarity'] not in VALID_POLARITIES:
            raise ValueError(f"Meta at {path}: invalid polarity in {where}")
        if fact['modality'] not in VALID_MODALITIES:
            raise ValueError(f"Meta at {path}: invalid modality in {where}")
        _check_evidence(fact['evidence'], where, path, 2)
        if fact.get('evidence_basis', 'book_usage') not in VALID_EVIDENCE_BASES:
            raise ValueError(f"Meta at {path}: invalid evidence_basis in {where}")

    for index, claim in enumerate(_validate_array(data, 'new_claims', path)):
        where = f"new_claims #{index}"
        _require_exact_keys(
            claim,
            {'holder', 'proposition', 'polarity', 'modality', 'scope', 'evidence'},
            {'target_gloss', 'evidence_basis'},
            where,
            path,
        )
        for field in ('holder', 'proposition', 'scope'):
            _require_str(claim[field], field, where, path)
        if 'target_gloss' in claim:
            _require_str(claim['target_gloss'], 'target_gloss', where, path)
        if claim['polarity'] not in VALID_POLARITIES:
            raise ValueError(f"Meta at {path}: invalid polarity in {where}")
        if claim['modality'] not in VALID_MODALITIES:
            raise ValueError(f"Meta at {path}: invalid modality in {where}")
        _check_evidence(claim['evidence'], where, path, 2)
        if claim.get('evidence_basis', 'book_usage') not in VALID_EVIDENCE_BASES:
            raise ValueError(f"Meta at {path}: invalid evidence_basis in {where}")

    for index, issue in enumerate(_validate_array(data, 'unresolved', path)):
        where = f"unresolved #{index}"
        _require_exact_keys(
            issue,
            {'segment_id', 'issue_type', 'question', 'options', 'needed_evidence', 'impact', 'evidence'},
            set(),
            where,
            path,
        )
        for field in ('segment_id', 'issue_type', 'question', 'needed_evidence'):
            _require_str(issue[field], field, where, path)
        options = issue['options']
        if not isinstance(options, list) or not options or len(options) > 20:
            raise ValueError(f"Meta at {path}: {where}.options must contain 1-20 strings")
        for option in options:
            _require_str(option, 'options', where, path)
        if issue['impact'] not in VALID_IMPACTS:
            raise ValueError(f"Meta at {path}: invalid impact in {where}")
        _check_evidence(issue['evidence'], where, path, 2)

    seen_segments = set()
    for index, item in enumerate(_validate_array(data, 'segment_translations', path)):
        where = f"segment_translations #{index}"
        _require_exact_keys(item, {'segment_id', 'target_text'}, set(), where, path)
        _require_str(item['segment_id'], 'segment_id', where, path)
        if not item['segment_id'] or len(item['segment_id']) > 128:
            raise ValueError(f"Meta at {path}: invalid segment_id in {where}")
        if item['segment_id'] in seen_segments:
            raise ValueError(f"Meta at {path}: duplicate segment_id in segment_translations")
        seen_segments.add(item['segment_id'])
        if not isinstance(item['target_text'], str) or not item['target_text'].strip():
            raise ValueError(f"Meta at {path}: {where}.target_text must be non-blank")
        if len(item['target_text']) > 16000:
            raise ValueError(f"Meta at {path}: {where}.target_text exceeds 16000 characters")


def validate_meta(data, path='<meta>'):
    """Strict v1/v2 validation. Raises ValueError with actionable messages."""
    if not isinstance(data, dict):
        raise ValueError(
            f"Meta at {path} must be a JSON object, got {type(data).__name__}"
        )

    schema_version = data.get('schema_version')
    if schema_version not in SUPPORTED_META_SCHEMA_VERSIONS:
        raise ValueError(
            f"Meta at {path}: schema_version mismatch — expected one of "
            f"{list(SUPPORTED_META_SCHEMA_VERSIONS)}, got {schema_version!r}."
        )

    allowed_keys = (
        VALID_V1_TOP_LEVEL_KEYS
        if schema_version == META_SCHEMA_VERSION
        else VALID_V2_TOP_LEVEL_KEYS
    )
    extras = set(data.keys()) - allowed_keys
    if extras:
        # 'chunk_id' is the most likely offender; call it out specifically so
        # the fix is obvious.
        if 'chunk_id' in extras:
            raise ValueError(
                f"Meta at {path}: 'chunk_id' field is not allowed in the meta "
                f"payload — chunk identity is derived from the filename. Remove it."
            )
        raise ValueError(
            f"Meta at {path}: unknown top-level key(s) {sorted(extras)!r}. "
            f"Allowed keys: {sorted(allowed_keys)!r}."
        )

    for entity in _validate_array(data, 'new_entities', path):
        if not isinstance(entity, dict):
            raise ValueError(
                f"Meta at {path}: each new_entities entry must be an object, "
                f"got {type(entity).__name__}"
            )
        for required in ('source', 'target_proposal', 'evidence'):
            if required not in entity:
                raise ValueError(
                    f"Meta at {path}: new_entities entry missing required field "
                    f"{required!r}"
                )
        _require_str(entity['source'], 'source', 'new_entities', path)
        _require_str(entity['target_proposal'], 'target_proposal', 'new_entities', path)
        if 'category' in entity:
            _require_str(entity['category'], 'category', 'new_entities', path)
        _check_evidence(entity['evidence'], 'new_entities', path, schema_version)

    for alias in _validate_array(data, 'alias_hypotheses', path):
        if not isinstance(alias, dict):
            raise ValueError(
                f"Meta at {path}: each alias_hypotheses entry must be an object"
            )
        for required in ('variant', 'may_be_alias_of_source', 'evidence'):
            if required not in alias:
                raise ValueError(
                    f"Meta at {path}: alias_hypotheses entry missing field {required!r}"
                )
        _require_str(alias['variant'], 'variant', 'alias_hypotheses', path)
        _require_str(alias['may_be_alias_of_source'], 'may_be_alias_of_source',
                     'alias_hypotheses', path)
        _check_evidence(alias['evidence'], 'alias_hypotheses', path, schema_version)

    for attr in _validate_array(data, 'attribute_hypotheses', path):
        if not isinstance(attr, dict):
            raise ValueError(
                f"Meta at {path}: each attribute_hypotheses entry must be an object"
            )
        for required in ('entity_source', 'attribute', 'value', 'confidence', 'evidence'):
            if required not in attr:
                raise ValueError(
                    f"Meta at {path}: attribute_hypotheses entry missing field {required!r}"
                )
        _require_str(attr['entity_source'], 'entity_source', 'attribute_hypotheses', path)
        _require_str(attr['attribute'], 'attribute', 'attribute_hypotheses', path)
        _require_str(attr['value'], 'value', 'attribute_hypotheses', path)
        if attr['confidence'] not in VALID_ATTRIBUTE_CONFIDENCES:
            raise ValueError(
                f"Meta at {path}: attribute_hypotheses 'confidence' must be one of "
                f"{list(VALID_ATTRIBUTE_CONFIDENCES)}, got {attr['confidence']!r}"
            )
        _check_evidence(attr['evidence'], 'attribute_hypotheses', path, schema_version)

    for s_idx, src in enumerate(_validate_array(data, 'used_term_sources', path)):
        if not isinstance(src, str):
            raise ValueError(
                f"Meta at {path}: used_term_sources #{s_idx} must be a string, "
                f"got {type(src).__name__}"
            )

    for conflict in _validate_array(data, 'conflicts', path):
        if not isinstance(conflict, dict):
            raise ValueError(
                f"Meta at {path}: each conflicts entry must be an object"
            )
        for required in ('entity_source', 'field', 'injected', 'observed_better', 'evidence'):
            if required not in conflict:
                raise ValueError(
                    f"Meta at {path}: conflicts entry missing field {required!r}"
                )
        _require_str(conflict['entity_source'], 'entity_source', 'conflicts', path)
        _require_str(conflict['field'], 'field', 'conflicts', path)
        _require_str(conflict['injected'], 'injected', 'conflicts', path)
        _require_str(conflict['observed_better'], 'observed_better', 'conflicts', path)
        _check_evidence(conflict['evidence'], 'conflicts', path, schema_version)

    if schema_version == LATEST_META_SCHEMA_VERSION:
        _validate_v2_extensions(data, path)


def load_meta(path):
    """Load and strictly validate a meta file. Raises ValueError on any
    problem. Callers that want non-blocking behavior should wrap this in a
    quarantine wrapper (see merge_meta.py)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Meta not found: {path}")
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"Meta at {path} must be a regular, non-symbolic-link file")
    size = os.path.getsize(path)
    if size > MAX_SIDECAR_BYTES:
        raise ValueError(
            f"Meta at {path} is {size} bytes; limit is {MAX_SIDECAR_BYTES} bytes"
        )
    with open(path, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Meta at {path} is not valid JSON: {e}") from e
    validate_meta(data, path)
    return data


def save_meta(path, data):
    """Atomically write a meta file. Validates before writing."""
    validate_meta(data, path)

    dirname = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix='.meta-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
