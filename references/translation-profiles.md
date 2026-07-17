# Translation profiles and review rules

## Contents

1. Common fidelity rules
2. Profile selection
3. Profile-specific rules
4. Independent review

## 1. Common fidelity rules

Preserve every source claim, qualifier, denial, uncertainty marker, speaker,
number, unit, citation, footnote, equation, code span, URL, image path, table,
and intentional ambiguity. Do not add explanations, strengthen weak claims,
weaken obligations, normalize contradictions, or silently repair the author.

Preserve Markdown and raw structural attributes. Translate visible prose and
human-readable `alt`/`title` text only. Keep code, identifiers, filenames,
anchors, `src`, and `href` unchanged. Preserve ordering and do not invent or
relevel headings.

Use the selected sense-specific canonical term. If two senses share a surface,
use context and supplied evidence; if still ambiguous, record an unresolved
item instead of guessing. Maintain narrator and speaker voice without forcing
all repeated ideas into identical sentences. Key claims must preserve holder,
polarity, modality, scope, and causal/logical relationships.

## 2. Profile selection

Honor an explicit user profile. Otherwise analyze the whole book and select:

- `academic-technical` for research, textbooks, standards, and technical
  exposition.
- `legal` for statutes, contracts, judgments, policies, and legal commentary.
- `literary` for fiction, memoir, drama, poetry, and voice-led narrative.
- `general` when evidence is mixed or insufficient.

Profile selection controls style and review checks, not security or evidence
rules. Record it in the state database so a profile change dirties all chunks.

## 3. Profile-specific rules

### General

Prefer idiomatic target-language prose while preserving information density,
stance, terminology, and paragraph structure. Preserve deliberate repetition.

### Academic and technical

Preserve definitions, variable names, taxonomies, citations, hedges, causal
direction, necessary/sufficient conditions, experimental limitations, and the
difference between observation, hypothesis, and conclusion. Keep SI units and
standards identifiers exact; localize unit typography only when requested.

### Legal

Preserve defined terms, cross-references, clause numbering, exceptions,
conditions, scope, temporal effect, and deontic force. Distinguish shall, must,
may, may not, should, and is entitled to. Never merge distinct legal senses
because they share a surface word. An unresolved obligation or exception is a
release blocker.

### Literary

Preserve narrator distance, character-specific diction, register, rhythm,
imagery, wordplay, intentional ambiguity, dialogue punctuation, and shifts in
viewpoint or tense. Do not make prose uniformly concise. Record recurring
motifs, names, forms of address, and speaker-specific choices as style/entity
memory rather than global terminology when appropriate.

## 4. Independent review

Review every chunk at least once against the current dependency hash. Check:

- omissions, additions, reordering, and unsupported explanation;
- sense-specific terminology, aliases, names, pronouns, and entity relations;
- key-claim holder, polarity, modality, attribution, temporal scope, and logic;
- numbers, units, dates, citations, footnotes, formulas, code, links, and images;
- Markdown/HTML integrity and profile-specific voice or register.

Mark critical/high findings dirty and require retranslation. Keep medium/low
findings in the final report. A review is stale as soon as any memory item used
by its chunk changes.
