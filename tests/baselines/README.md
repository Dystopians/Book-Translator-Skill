# Integration fixtures

These EPUB files are stable, human-readable inputs for optional end-to-end
conversion and publishing checks. They are not used by the standard-library
unit-test suite.

| Fixture | Primary coverage | Approximate chunks at size 6000 |
|---|---|---:|
| `sleepy-hollow` | Fast smoke run and distant aliases | 21 |
| `standard-alice` | Chapters, internal links, and many illustrations | 38 |
| `diligent-dick` | Cross-chapter person aliases and ambiguous surnames | 57 |

Run generated work under `tests/.artifacts/`; that directory is ignored except
for its explanatory file and placeholder. Do not write generated translations
or built books back into a fixture directory.

Each fixture directory contains a `SOURCE.md` with provenance and the behavior
it is meant to exercise. Legal notices for the packaged editions are recorded
in the repository's `THIRD_PARTY_NOTICES.md` and inside the EPUB files.
