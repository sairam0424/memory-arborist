## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checks

- [ ] Every commit is signed off (`git commit -s`).
- [ ] A bug fix has a test that failed before the change; a feature has tests for the behaviour and the error paths.
- [ ] No existing test, threshold or fixture was loosened, skipped or deleted to make this pass.
- [ ] All four suites pass on my Python (see the README, "Tests").
- [ ] For each Claude Code behaviour this depends on, the public page or `claude --help` line is cited, and an undocumented one is in section 3 of `docs/PROVENANCE.md`.
- [ ] No notes, transcript text, keys or personal data in the diff, the tests or this description.
