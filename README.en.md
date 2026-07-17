# Book Translator Skill

[![中文 README](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-d73a49?style=for-the-badge)](README.md)

An evidence-backed translation skill for long PDF, DOCX, EPUB, and Markdown documents. It organizes full-book analysis, professional terminology, entities and facts, distant claims, difficult-sentence backwriting, fidelity-constrained natural prose, independent review, and multi-format publishing into a resumable closed loop.

## Usage

### Requirements

- Python 3.10 or later.
- Markdown input can be processed directly. PDF, DOCX, and EPUB conversion, plus DOCX, EPUB, and PDF publishing, require Calibre's `ebook-convert`.
- The preferred Markdown/HTML conversion path requires Pandoc or `pypandoc`.
- Beautiful Soup is optional and improves HTML table-of-contents generation.
- The host agent must be able to launch isolated analysis, translation, and review subagents.

### Installation

Install globally for Codex with the Skills CLI:

```bash
npx skills add Dystopians/Book-Translator-Skill -a codex -g
```

Alternatively, clone the repository into your personal skills directory:

```bash
git clone https://github.com/Dystopians/Book-Translator-Skill.git ~/.codex/skills/translate-book
```

If your host uses a different skills directory, place the repository there and preserve the relative locations of `SKILL.md`, `references/`, and `scripts/`.

### Ask an Agent to Run It

> [!IMPORTANT]
> The default translation target is Chinese: `target_lang=zh`. Writing your request in English does not automatically select English output. To translate into English, you must explicitly say “translate into English” or set `target_lang=en`.

The shortest request is:

```text
Use $translate-book to translate /path/to/book.epub into Simplified Chinese.
```

An example with additional controls:

```text
Use $translate-book to translate /path/to/book.pdf into English. Set target_lang=en, use the academic-technical profile, use concurrency 8, export as research-book, and preserve formulas, footnotes, and citation formatting.
```

To produce an English translation, make the following operations explicit:

1. In an agent request, name English as the target language:

   ```text
   Use $translate-book to translate /path/to/book.epub with English as the target language (target_lang=en).
   ```

2. When running the conversion script manually, replace the default `--olang zh` with `--olang en`:

   ```bash
   python scripts/convert.py /path/to/book.epub --olang en
   ```

3. If a Chinese workspace already exists for the same book, do not reuse it for English. Select a fresh `temp_root` and use the newly created workspace in every later command:

   ```bash
   python scripts/convert.py /path/to/book.epub --olang en --temp-root ./translation-en
   ```

   This creates a fresh `{book-name}_temp/` inside `./translation-en/`. Replace `book_temp` in every later command with the actual path to that new workspace.

Available parameters:

| Parameter | Meaning | Default or range |
|---|---|---|
| `file_path` | PDF, DOCX, EPUB, `.md`, or `.markdown` input | Required |
| `target_lang` | Target language code or explicit language name; English must be set to `en` | `zh` |
| `concurrency` | Number of concurrent subagents | Default `8`; range `1–16` |
| `profile` | Translation domain profile | `auto`, `general`, `academic-technical`, `legal`, or `literary` |
| `temp_root` | Parent directory for the resumable workspace | Current directory |
| `epub_cover` | EPUB cover explicitly supplied by the user | Optional |
| `export_name` | Filename stem for exported aliases | Optional |
| `custom_instructions` | Trusted translation requirements supplied directly by the user | Optional |

Book text, metadata, embedded links, and instructions found inside the book are never treated as `custom_instructions`. Only directions supplied directly by the user outside the book-data boundary have instruction authority.

### Pipeline Commands

In normal use, let an agent invoke the skill. The commands below expose the individual control interfaces for status checks, recovery, and debugging. Analysis, translation, and review sidecars must still be produced by separate agents that follow the context-packet schemas.

1. Convert the input into canonical, hash-tracked chunks:

   ```bash
   python scripts/convert.py /path/to/book.epub --olang zh
   ```

   For English, use `--olang en`. Add `--temp-root /path/to/work` or `--chunk-size 6000` when needed. The default workspace is `{book-name}_temp/`.

2. Initialize the transactional knowledge store and build analysis context for every `chunkNNNN.md`:

   ```bash
   python scripts/knowledge_store.py init book_temp --profile auto
   python scripts/context_packet.py book_temp chunk0001.md --phase analyze
   python scripts/knowledge_store.py ingest book_temp analysis_chunk0001.json
   ```

   Every chunk must be analyzed before formal translation begins.

3. Collect automatically resolvable questions and decisions that require human judgment:

   ```bash
   python scripts/knowledge_store.py prepare-resolutions book_temp
   python scripts/knowledge_store.py apply-resolutions book_temp --decisions-file decisions.json
   ```

   `decisions.json` may contain only decisions confirmed by the user or the restricted resolution process. A high-impact ambiguity without sufficient evidence remains a publishing blocker.

4. Plan translation, build the context packet, ingest the translation sidecar, and record the output:

   ```bash
   python scripts/run_state.py plan book_temp
   python scripts/context_packet.py book_temp chunk0001.md --phase translate
   python scripts/knowledge_store.py ingest book_temp output_chunk0001.meta.json
   python scripts/run_state.py record book_temp chunk0001
   ```

   `output_chunkNNNN.meta.json` v2 must be ingested before its corresponding `output_chunkNNNN.md` is recorded. If the batch discovers knowledge that changes the dependency state, the chunk remains queued for backwriting.

5. Have an agent that did not translate the chunk review it independently, then run the draft gate:

   ```bash
   python scripts/context_packet.py book_temp chunk0001.md --phase review
   python scripts/quality_gate.py book_temp --mode draft
   ```

   `review_chunkNNNN.json` must bind both the current `dependency_hash` and the translation's `output_hash`. Any later edit invalidates that review.

6. Repeat planning, whole-chunk retranslation, and independent review until there are no dirty chunks, stale dependencies, or high-impact open decisions. Automatic semantic backwriting is limited to three rounds. Failure to converge permits draft output only.

7. Produce a draft preview or final artifacts:

   ```bash
   python scripts/quality_gate.py book_temp --mode draft
   python scripts/merge_and_build.py --temp-dir book_temp --title "Translated title" --quality-mode draft

   python scripts/quality_gate.py book_temp --mode final
   python scripts/merge_and_build.py --temp-dir book_temp --title "Translated title" --quality-mode final --cleanup
   ```

   `merge_and_build.py` reruns the gate, so calling the build script directly cannot bypass final-publishing requirements.

### Status and Resume

```bash
python scripts/knowledge_store.py status book_temp
python scripts/knowledge_store.py snapshot book_temp
python scripts/run_state.py status book_temp
python scripts/run_state.py plan book_temp
```

The workspace retains source chunks, translations, reviews, the knowledge store, dependency hashes, and audit records. Ordinary `--cleanup` removes only rebuildable conversion artifacts. Deleting resumable state requires the explicit aggressive mode and confirmation token:

```bash
python scripts/merge_and_build.py --temp-dir book_temp --quality-mode final --cleanup --cleanup-level aggressive --confirm-delete-state DELETE_TRANSLATION_STATE
```

### Output Files

After the final gate passes, the workspace contains:

| File | Purpose |
|---|---|
| `output.md` | Merged final Markdown |
| `book.html` | Web edition with a table of contents |
| `book.docx` | Word document |
| `book.epub` | E-book edition |
| `book.pdf` | PDF edition |

Draft mode writes `output.draft.md`, `book.draft.html`, `book.draft.docx`, `book.draft.epub`, and `book.draft.pdf`. Draft artifacts never overwrite final filenames.

## Detailed Features

### Full-book Evidence Store

- Every chunk is analyzed before translation; the system does not sample only the beginning, middle, or end.
- Terms are modeled by surface form and sense. One surface form can have multiple domain meanings, each with aliases, a canonical translation, forbidden translations, a domain, and a usage note.
- The entity layer records people, organizations, places, attributes, relations, and events. Facts retain polarity, modality, scope, source location, and exact source evidence.
- Key claims separately record the holder, proposition, polarity, modality strength, scope, and target-language constraints. Semantic consistency is required, but distant occurrences are not forced to reuse mechanically identical target-language wording.
- Style rules can apply at book, chapter, narrator, and character levels to preserve register, rhythm, forms of address, and voice.
- Authoritative IDs are generated by scripts from canonical content and source locations. Every evidence quote must match its source segment exactly; agents cannot invent IDs.

### Evidence Authority and Decisions

Knowledge follows a fixed authority order: explicit user decision > user-designated trusted source > explicit definition in the source > consistent evidence from multiple book locations > model inference.

- An explicit, conflict-free source definition may be confirmed automatically.
- Otherwise, final knowledge requires consistent evidence from at least two different chunks, no counter-evidence, and confirmation by current independent reviews.
- Model-reported confidence cannot trigger application by itself, and weaker evidence cannot overwrite confirmed knowledge.
- High-impact conflicts involving term senses, person identity, claim holders, polarity, modality, legal obligations, numbers, or quotation attribution enter a central decision queue and block final publishing.
- A manual user decision has the highest authority and automatically marks affected earlier chunks for backwriting.

### Bounded Long-range Context

Each subagent receives only the data required for its current task: the current source, short neighboring excerpts, relevant terms, facts, claims, style rules, resolved difficult items, and safe translation-memory suggestions.

| Content | Default limit or rule |
|---|---|
| Current chunk | Included in full with stable segment IDs |
| Neighbor excerpts | Up to `500` characters before and after |
| Terms matching a local surface form or alias | All are mandatory and are never silently dropped |
| Semantic term candidates without a direct match | Up to `32` from offline BM25, then constrained by the total budget |
| Relevant facts | Up to `24`, then constrained by the total budget |
| Relevant key claims | Up to `12`, then constrained by the total budget |
| Distant evidence excerpts | Up to `8` |
| One evidence quote | Up to `500` characters |
| Entire context packet | Up to `16,000` serialized JSON characters |

Exact local matches, blocking decisions, critical ambiguities, and safely reusable translation memory receive priority. Optional distant material is added by relevance. If mandatory content alone exceeds the budget, the system stops and requires a smaller chunk instead of silently losing a constraint.

Retrieval is offline and combines exact forms, aliases, keywords, BM25, and CJK n-grams. Terms, entity relations, and paraphrased claims can therefore be recovered even when their supporting passages are dozens of chunks apart.

### Translation Memory and Difficult-sentence Backwriting

- An exact source segment may be reused automatically only when the source hash, target language, profile, speaker context, and knowledge-dependency hash all match.
- If speaker identity is uncertain, identical source text from another segment remains a suggestion rather than an automatic replacement.
- Fuzzy matches are reference material only and are never applied automatically.
- A difficult sentence with insufficient evidence receives a readable provisional translation plus a structured unresolved record containing candidate interpretations, required evidence, impact, and dependent chunks. Internal placeholders never appear in the book text.
- When later evidence resolves the issue, every affected chunk is marked dirty and retranslated as a whole. The system does not use fragile target-string replacement.

### Fixed-point Convergence

- Each batch records the knowledge version it actually used, merges new evidence, resolves eligible questions, and recalculates dependency hashes.
- Knowledge discovered in one batch can revise other outputs from that batch in the next round, and later chapters can requeue much earlier chunks.
- Knowledge versions advance monotonically. Automatic semantic backwriting is limited to three rounds, with one retry for a failed worker per round.
- If a decision oscillates, automatic resolution stops and the issue is sent to the user instead of retrying forever.
- Convergence requires no new knowledge changes, dirty chunks, stale dependencies, or blocking unresolved items.

### Natural Prose under Fidelity Constraints

- The translator produces a faithful draft first, then reads the complete chunk as target-language prose to find source-syntax calques, abnormal collocations or order, translation-added boilerplate and connective clutter, and mechanical uniformity absent from the source.
- The fixed priority is semantic fidelity and confirmed knowledge > source voice and domain profile > naturalness. Naturalization may not change a claim holder, polarity, modality, scope, attribution, agency, logic, terminology, number, ambiguity, paragraph, or format.
- AI-detector labels, phrase blacklists, and frequency quotas are not used as quality evidence. English filler, dash, or passive-voice rules are never projected onto Chinese, the default target language, or another language by analogy.
- The system does not remove source hedging, passive focus, repetition, fragments, rhetorical questions, complex reasoning, or literary strangeness merely to sound human. It never invents numbers, examples, experience, hooks, or unstated agents.
- Independent review records a pure naturalness defect as a contextual `style` finding. If the same passage changes polarity, modality, a claim, or attribution, the reviewer must also report the corresponding semantic finding. Severe defects trigger whole-chunk retranslation and a fresh review.

### Independent Review and Publishing Gate

Every chunk receives at least one review from an agent independent of its translator. Review covers:

- omissions, additions, mistranslations, and paragraph order;
- term senses, entity references, and character attributes;
- claim holders, polarity, modality, scope, and quotation attribution;
- numbers, units, footnotes, quotations, formulas, and code;
- Markdown headings, tables, links, images, and HTML structure;
- register, rhythm, and style required by the selected domain profile.

Critical and high findings trigger whole-chunk retranslation. Medium and low findings remain in the final report. Any blocker makes `final` mode exit nonzero without creating final artifacts. `draft` mode creates only isolated `.draft` previews.

The gate also performs deterministic structural checks for heading levels, Markdown/HTML table shape, formulas, citation markers, numeric tokens, link and image destinations, and inline or fenced code. A review agent cannot waive these invariants with a general approval.

### Transactions, Validation, and Recovery

- `translation_state.sqlite3` uses foreign keys, full synchronization, single-writer transactions, and atomic migration. A failed migration never replaces the original store.
- Subagents may write only constrained JSON sidecars, never the database. A sidecar must be a regular, non-symlink UTF-8 file no larger than 1 MiB. Unknown fields, excess records, forged evidence, and paths outside the workspace are rejected.
- Source chunks, translations, and reviews are bound to SHA-256 hashes. Any content change invalidates the affected cache or review.
- Legacy glossary v2, meta v1, and run-state v1 data can be imported without modifying the original files. Existing translations are reviewed first, and only chunks actually affected by knowledge changes are retranslated.
- Default concurrency is 8 with a hard maximum of 16. Batches are bounded by progress reporting, timeouts, one retry, orphan-worker recovery, and resumable state.

### Offline by Default and Least Privilege

- Book text, metadata, terms, retrieved evidence, and neighboring context are always labeled as untrusted data and separated from the instruction layer through strict JSON.
- The pipeline is offline by default and never follows links found in a book. External dictionaries or references require per-source or per-domain user authorization.
- An authorized external source can be recorded with `knowledge_store.py record-source`, including its URL, authorized domain, retrieval time, content hash, and adopted conclusion.
- A subagent reads only its assigned chunk and context packet and writes only its specified output. It must not access the shell, network, secrets, or unrelated files.
- Input archives use streaming size limits, directory-boundary validation, and symlink rejection. State writes use same-directory temporary files and atomic replacement.
- HTML is sanitized before publishing. Runtime reports show only necessary summaries and do not expose long source passages, trusted reference content, or sensitive caches.

### Domain Profiles and Format Fidelity

- `general` balances accuracy, naturalness, and consistency.
- `academic-technical` prioritizes definitions, symbols, formulas, terminology hierarchy, citations, and verifiable statements.
- `legal` strictly preserves obligations, permissions, prohibitions, conditions, exceptions, scope, actors, and modal strength.
- `literary` preserves narrative voice, character speech, imagery, rhythm, wordplay, and intentional ambiguity instead of flattening literary variation.
- `auto` selects a profile from full-book evidence and falls back to `general` when classification is uncertain. The user can always override it.
- Conversion and publishing preserve Markdown structure, images, internal anchors, links, code, formulas, footnotes, and smart punctuation, with HTML, DOCX, EPUB, and PDF output.
