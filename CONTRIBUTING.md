# Contributing

Thanks for helping. Three rules matter most.

1. **Sign off every commit** (Developer Certificate of Origin, https://developercertificate.org): `git commit -s`. A pull request with unsigned commits will be asked to amend.
2. **Tests first.** A bug fix starts with a test that fails for the right reason; a feature needs tests for the behaviour and for the error paths. Do not loosen, skip or delete an existing test, threshold or fixture to make a change pass; if one looks wrong, open an issue that shows the failing output. Run all three suites (see the README) on your Python before you push.
3. **Clean provenance.** Rely only on public documentation, the public `claude --help`, and behaviour you can observe in your own sessions. Do not paste code, prompts, strings or text taken from Claude Code's binary, source code or any leaked material, and do not name internals that only its code could tell you. For each Claude Code behaviour a change depends on, cite the public page or the `--help` line in the pull request; behaviour that no page documents but anyone can see in their own sessions goes into section 3 of `docs/PROVENANCE.md` with how to observe it. `engine/tests/test_memory_release_hygiene.py` fails on home paths, and on every word in the maintainer's private deny-list (names, and names of undocumented internals) when that git-ignored file is present; never add an exception.

## Style

Standard library only. Functions under about 50 lines, files under about 800, early returns, no dead code, errors handled where they happen and never swallowed silently. Anything that quotes user text goes through the existing safe-snippet helpers, and anything that writes goes through the existing private-file helpers.

## Mutation checks

`dev/harness/` edits a temporary copy of the engine and expects the tests to fail. If you change code that a harness mutant targets, rerun it (`python3 -I dev/harness/harness_dream.py`, `python3 -I dev/harness/harness_nudge.py`, `python3 -I dev/harness/harness_wizard.py`); a surviving mutant means a missing test, unless it is listed as equivalent in the harness header.
