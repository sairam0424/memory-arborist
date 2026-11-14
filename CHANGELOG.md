# Changelog

All notable changes are listed here. The format follows Keep a Changelog; versions follow Semantic Versioning once 1.0.0 ships. Until then a minor version may change behaviour.

## [Unreleased]

### Added
- First public extraction of the engine: safety net (snapshot, verify, restore, lint, secret scan, compare, log, prune plan), redacted evidence digests, an outside recall judge, tool-less proposal lanes with mechanical grounding, staged runs with digest-bound promote, and a read-only session-start headroom hook.
- Recall-question wizard (`recall/memory_recall_wizard.py`, key grammar in `recall/memory_recall_keys.py`): tool-less children propose questions from the snapshot's notes; every candidate is checked against the notes (literal quote, keys in a safe regex grammar that match the quote and neither the question nor their own answer, absent probe words for negative questions); the proposal and a report are written outside the memory directory, and `accept` installs exactly the bytes whose digest the user passes.
- The recall judge grades at most the first 8,000 characters of an answer, so a key cannot be made to run on an unbounded text.
- Claude Code plugin package (`.claude-plugin/`, `skills/shepherd/`, `hooks/hooks.json`) and a single-plugin marketplace file.
- Release guard test that fails when a home path, or a word from the maintainer's private deny-list (kept in a git-ignored file, not in the repository), appears in the content or the name of any non-binary file (LICENSE, NOTICE and .gitignore included).

### Changed
- Model choices are provider-neutral aliases (`sonnet`, `haiku`) overridable with `MEMORY_SHEPHERD_LANE_MODEL` and `MEMORY_SHEPHERD_JUDGE_MODEL`.
- Children receive a fixed short environment plus the names listed in `MEMORY_SHEPHERD_PASS_ENV`.
- The lint tripwire now only warns when the markdown file count drops below the last snapshot's (`memory-shrink-tripwire`).
- The evidence builder's run marker is `MEMORY-SHEPHERD-RUN`.

- `memory_egress.py`: a gate in front of every call that sends note text to the provider. A fresh scan of the notes the model can read refuses the run (exit 4) on a block-tier secret shape or an unscannable entry; `--allow-secret-shapes` overrides it for one run.
- Staging refuses a dream edit that adds a YAML hazard to a note's name or description (`yaml-*` findings), counted per file so a hazard the snapshot already had never blocks.
- At most 5 hunks per writer reach the paid verifier; a failed child's printed cost is counted, and a timed-out child is charged its per-call cap.
- Scanner: GitHub `gho_`/`ghu_`/`ghr_`, Stripe `rk_` and `sk_org_`, Slack `xapp-` and `xoxe-`, OpenAI project, service-account and admin keys, AWS `ABIA`/`ACCA` ids, database schemes `postgresql`, `mongodb+srv`, `rediss`, `mysqlx`, and the Bearer scheme with the RFC 6750 characters (other casings need a digit).
- `CLAUDE_CONFIG_DIR` is followed (the data root moves with it).
- The override flag cannot be given as an argparse abbreviation; a failed child that reports a tool still aborts the run; an absurdly large cost in a result is ignored instead of crashing; the dream run's temporary folder and the judge's are removed with the run; the Anthropic and Stripe key patterns are anchored on the left; database URLs with a `+driver` suffix are matched; a description emptied to `""` or `''` counts as missing.
- Test fixtures that look like credentials are assembled at runtime everywhere; the release guard now scans the repo's own source with the repo's own scanner and refuses the synthetic fake body as one piece, because an external secret scanner flagged it.
- Mutation harnesses refuse to run on a red baseline and report ambiguous patterns; 36 more mutants.

### Fixed
- `dream/memory_dream_canary.py --help` and a mistyped option used to start a paid run; the canary now parses its arguments before it creates anything.
- Failures look like failures. A dream run in which model calls failed used to exit 0 and write "all clean"; it now prints `INCOMPLETE`, puts a banner at the top of `report.md` and exits 5. A failed child is reported with the first line it printed ("claude exited with 1: Not logged in") instead of a bare number (a line that mentions a credential is withheld, with a hint to read it yourself). The recall judge stops after 6 failed calls when none has answered, and prints `TOOLS ok=` only once a child really answered. The dream run now looks for `pinned.txt` before the first paid call instead of after the lanes.
