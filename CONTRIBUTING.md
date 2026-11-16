# Contributing

Thanks for helping. Three rules matter most.

1. **Sign off every commit** (Developer Certificate of Origin, https://developercertificate.org): `git commit -s`. A pull request with unsigned commits will be asked to amend.
2. **Tests first.** A bug fix starts with a test that fails for the right reason; a feature needs tests for the behaviour and for the error paths. Do not loosen, skip or delete an existing test, threshold or fixture to make a change pass; if one looks wrong, open an issue that shows the failing output. Run every suite listed in the README, and `dev/acceptance/tests`, on your Python before you push.
3. **Clean provenance.** Rely only on public documentation, the public `claude --help`, and behaviour you can observe in your own sessions. Do not paste code, prompts, strings or text taken from Claude Code's binary, source code or any leaked material, and do not name internals that only its code could tell you. For each Claude Code behaviour a change depends on, cite the public page or the `--help` line in the pull request; behaviour that no page documents but anyone can see in their own sessions goes into section 3 of `docs/PROVENANCE.md` with how to observe it. `engine/tests/test_memory_release_hygiene.py` fails on home paths, and, when the maintainer's git-ignored private word list is present, on every word in it; never add an exception.

## Style

Standard library only. Functions under about 50 lines, files under about 800, early returns, no dead code, errors handled where they happen and never swallowed silently. Anything that quotes user text goes through the existing safe-snippet helpers, and anything that writes goes through the existing private-file helpers.

## Mutation checks

`dev/harness/` edits a temporary copy of the engine and expects the tests to fail. If you change code that a harness mutant targets, rerun it (`python3 -I dev/harness/harness_dream.py`, `python3 -I dev/harness/harness_nudge.py`, `python3 -I dev/harness/harness_wizard.py`); a surviving mutant means a missing test, unless it is listed as equivalent in the harness header.

`dev/acceptance/run_seeded.py` is the seeded-bad-note test: it plants known-stale facts and traps in a synthetic memory and counts what the real pipeline fixed, missed and wrongly edited. Its unit tests (`dev/acceptance/tests`) run with a scripted model and spend nothing; the `--live` run uses your claude login and costs a few dollars at most.

## Issues and pull requests

One person maintains this, so everything is read as time allows. An issue that lacks the doctor output (see the bug form) will get a request for it before anything else. A pull request is easiest to take when it is small, has a failing test first, and cites its sources; larger changes are better discussed in an issue before you write them. Everyone taking part follows `CODE_OF_CONDUCT.md`. Please never paste notes, transcripts, keys or personal data into an issue or a pull request.

## Releasing (maintainer)

Claude Code installs a plugin's copy only when its `version` changes, so every release bumps the `version` in `.claude-plugin/plugin.json` (and only there: the marketplace entry must not carry a second one).

1. Update `CHANGELOG.md` (move the `[Unreleased]` entries under the new version) and bump the `version`; `engine/tests/test_memory_release_packaging.py` fails if the manifest, the marketplace entry, the hook paths, the skill and the changelog disagree.
2. Run the four suites and `claude plugin validate . --strict`, and run `engine/doctor/memory_doctor.py --live` once on a real login.
3. Tag with `claude plugin tag --push` (it validates, then creates and pushes the `memory-arborist--v<version>` tag).
4. Move the `stable` branch to that tag (`git branch -f stable <tag>` and push it). Testers add the marketplace as `sairam0424/memory-arborist#stable`, so they follow releases and not the work in progress on `main`.
5. Publish the GitHub release with the changelog section as its notes.
