# Translation profiles and review rules

## Contents

1. Common fidelity rules
2. Naturalness without semantic drift
3. Profile selection
4. Profile-specific rules
5. Independent review

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

## 2. Naturalness without semantic drift

Use this fixed priority:

1. Semantic fidelity and confirmed knowledge.
2. Source voice and the selected domain profile.
3. Natural target-language expression.

Naturalness is a translation-quality goal, not an attempt to evade an AI
detector. A detector label, phrase blacklist, word-frequency threshold, or a
construction's reputation as an "AI tell" is never evidence of a defect.
Diagnostics must be appropriate to the target language and selected profile.
Do not apply English filler lists, punctuation preferences, or voice rules to
Chinese or another target language by analogy.

Before revising for naturalness, lock these semantic anchors: claim holder,
proposition, polarity, modality, scope, conditions, causal and logical
relations, attribution, agency and information focus, entities, sense-specific
terms, numbers, citations, intentional ambiguity, rhetorical function,
register, paragraph boundaries, and structural markup.

Use this bounded pass after producing a faithful draft and before the worker
writes its first output files:

1. Read the complete chunk as target-language prose, not as isolated sentences.
2. Identify only defects introduced by the translation: source-syntax calques,
   abnormal collocations or word order, empty boilerplate, redundant connective
   scaffolding or filler, mechanically uniform rhythm, generic jargon, and
   unsupported rhetorical flourishes.
3. Recast the complete affected sentence or passage within its existing
   paragraph. Split or combine sentences only when every semantic anchor and
   rhetorical function remains intact. Never create or remove a blank-line
   paragraph because segment-to-translation alignment depends on it.
4. Perform a read-aloud-style cadence scan in the target language, then compare
   the revision with the source and current knowledge packet anchor by anchor.
5. Revert any edit that changes an anchor. If the faithful reading remains
   materially awkward or ambiguous, report a finding or unresolved item rather
   than inventing a smoother meaning.

After an independent review or a dependency change, any correction follows the
convergence workflow: retranslate the complete chunk and review it again. Do
not turn this pre-output editing pass into a post-review substring patch.

Remove redundancy, filler, generic transitions, a formulaic opening or ending,
or repeated scaffolding only when the translation itself introduced it and it
has no source function. Never use this pass to:

- delete, strengthen, or weaken a hedge, softener, denial, or modal expression;
- invent an agent, number, example, quotation, personal experience, hook,
  benefit, warning, urgency, or rhetorical question;
- force passive voice into active voice, expose an unstated agent, or change
  responsibility, attribution, focus, or information order;
- shorten complex reasoning, simplify a term, or make specialist prose
  conversational merely to sound more direct;
- remove source-motivated repetition, parallelism, contrast, fragments,
  questions, transitions, punctuation, marked syntax, or ambiguity; or
- homogenize narrator, character, chapter, genre, or domain-specific voice.

When the source uses a marked construction, reproduce its function in the
target language even if the surface form changes. A natural translation can be
formal, hesitant, repetitive, passive, fragmented, strange, or difficult when
the source deliberately is.

## 3. Profile selection

Honor an explicit user profile. Otherwise analyze the whole book and select:

- `academic-technical` for research, textbooks, standards, and technical
  exposition.
- `legal` for statutes, contracts, judgments, policies, and legal commentary.
- `literary` for fiction, memoir, drama, poetry, and voice-led narrative.
- `general` when evidence is mixed or insufficient.

Profile selection controls style and review checks, not security or evidence
rules. Record it in the state database so a profile change dirties all chunks.

## 4. Profile-specific rules

### General

Prefer idiomatic target-language prose while preserving information density,
stance, terminology, and paragraph structure. Preserve deliberate repetition.
Natural does not mean uniformly conversational; remove only target-language
stiffness or formulaic scaffolding introduced by the translation.

### Academic and technical

Preserve definitions, variable names, taxonomies, citations, hedges, causal
direction, necessary/sufficient conditions, experimental limitations, and the
difference between observation, hypothesis, and conclusion. Keep SI units and
standards identifiers exact; localize unit typography only when requested.
Use normal target-language disciplinary prose, but retain hedging,
nominalization, passive focus, and technical density whenever they carry
evidential strength, agency, information structure, or genre convention.
Preserve the contextual relative force of expressions such as *may*, *might*,
*could*, *likely*, and *generally*; do not collapse them into one generic marker
of uncertainty.

### Legal

Preserve defined terms, cross-references, clause numbering, exceptions,
conditions, scope, temporal effect, and deontic force. Distinguish shall, must,
may, may not, should, and is entitled to. Never merge distinct legal senses
because they share a surface word. An unresolved obligation or exception is a
release blocker.
Do not simplify or activate a legal sentence merely for directness. A
naturalness edit may not change a responsible party, defined term, condition,
exception, temporal effect, scope, or deliberate legal repetition.
Resolve the scope of *may not* before translating it: prohibition, absence of
permission, and negated possibility are not interchangeable. Determine whether
*shall* expresses obligation, declaration, or futurity from context instead of
using a fixed word-for-word mapping.

### Literary

Preserve narrator distance, character-specific diction, register, rhythm,
imagery, wordplay, intentional ambiguity, dialogue punctuation, and shifts in
viewpoint or tense. Do not make prose uniformly concise. Record recurring
motifs, names, forms of address, and speaker-specific choices as style/entity
memory rather than global terminology when appropriate.
Treat fragments, repetition, parallelism, contrast, rhyme, punctuation, and
marked syntax as voice signals to recreate, not patterns to ban. Literary
naturalness means a credible specific voice, not uniformly smooth prose.

## 5. Independent review

Review every chunk at least once against the current dependency hash. Check:

- omissions, additions, reordering, and unsupported explanation;
- sense-specific terminology, aliases, names, pronouns, and entity relations;
- key-claim holder, polarity, modality, attribution, temporal scope, and logic;
- numbers, units, dates, citations, footnotes, formulas, code, links, and images;
- Markdown/HTML integrity and profile-specific voice or register;
- source-unmotivated calques, abnormal target-language collocations or order,
  boilerplate, connective clutter, filler, and mechanical homogenization; and
- whether any naturalness edit altered a semantic anchor, rhetorical function,
  paragraph boundary, or formatting invariant.

A `style` finding must describe a contextual target-language or profile defect
and cite matching source and target evidence. Do not report a finding solely
because a word, construction, punctuation mark, or sentence length matches a
list. Use medium/low for a local naturalness defect; use high only when the
problem is pervasive or substantially damages intelligibility, narrator voice,
character voice, or domain register. If the same passage also changes meaning,
report the relevant claim, polarity, modality, attribution, terminology, or
other semantic finding separately. The reviewer reports findings and never
rewrites the output.

Mark critical/high findings dirty and require retranslation. Keep medium/low
findings in the final report. A review is stale as soon as any memory item used
by its chunk changes.
