# memory-shepherd

**A memory upgrade for your Claude Code harness.** memory-shepherd is a Claude Code plugin that looks after the auto-memory directory (`MEMORY.md` plus one note per topic). It backs the memory up, tells you when the index is filling up, cleans out stale and contradicted notes using what you actually said in past sessions, and checks that the clean-up did not make Claude forget anything. It never changes your memory behind your back: every edit is shown to you as a diff and installed only after you say yes, and any change can be restored from a snapshot.

**What you get**

- **Verified snapshots and restore.** A hash-checked backup of the memory directory before anything touches it, and a one-file restore.
- **A heads-up before the index overflows.** A read-only session-start hint when `MEMORY.md` is close to Claude Code's documented read limit.
- **Clean-up from your own history.** Redacted digests of your past sessions show which notes are stale, wrong or superseded. Model lanes without tools draft the fixes, and every fix is checked mechanically against the notes and the evidence before you see it.
- **A recall check.** An outside judge asks the same fixed questions of the old and the new memory and flags anything Claude would now forget or get wrong. A wizard proposes the question set from your own notes, checks it against them, and you read it before it is installed.
- **A secret check before anything is sent.** The notes a model is about to read are scanned first; a run that would send a credential-shaped string is refused.

Under the hood it is a skill, a session-start hook and a Python standard-library engine, described in the sections below.

> **Status: pre-release (0.1.0), not published yet.** macOS and Linux only (it relies on POSIX file semantics; Windows is not supported). Needs `python3` 3.11 or newer (CI covers 3.11 to 3.15; the suites also pass on 3.9), standard library only, and the `claude` CLI on `PATH`; it was developed and tested with Claude Code 2.1.322 (it uses `--safe-mode` and exec-form hook arguments) and older versions are untested. The recall judge needs a question set of 25 to 32 questions: the question wizard proposes most of it from your notes for you to read and accept, and you add the questions about stale or replaced facts by hand (see `docs/recall-questions.md`).

## Install

Once the repository is public:

```
claude plugin marketplace add sairam0424/memory-shepherd
claude plugin install memory-shepherd@memory-shepherd
```

Then ask Claude to run the `memory-shepherd:shepherd` skill ("consolidate my memory"). The plugin also adds a read-only `SessionStart` hook that tells the session when `MEMORY.md` is close to Claude Code's documented read limit. It prints nothing until the index is within about 1,500 characters or 20 lines of the linter's warning levels; then it prints one fixed two-sentence message with four numbers filled in (no note text, no paths) that tells the model to inform you and not to act unless you ask.

## What it does

| Stage | Command (in `engine/`) | Writes to your memory? |
|---|---|---|
| Safety net: snapshot, verify, restore, lint, secret scan, compare, log, prune plan | `memory_hygiene.py` | only `restore --apply`, which puts back one named file from a named snapshot (a dry run without `--apply`) |
| Evidence: redacted digests of idle past sessions | `memory_evidence.py` | no |
| Outside recall judge: fixed questions, regex-graded answers, baseline / staged / empty arms | `recall/memory_recall.py` | no |
| Question wizard: proposes the question set from your notes, checks it against them, installs it only for the digest you read | `recall/memory_recall_wizard.py` | no |
| Proposal lanes (finder, writer, verifier): headless `claude` children with no tools | `dream/memory_dream_run.py` | no |
| Ground, stage, promote | `memory_dream.py` | only `promote --apply`, with a digest that matches the staged content |

The lanes run `claude --safe-mode --tools ""`: no built-in tools, and `--safe-mode` also turns off MCP servers, plugins, hooks and CLAUDE.md for the child (see `claude --help`). A run aborts if a lane ever reports using a tool. A planted-injection canary (`dream/memory_dream_canary.py`) checks that a true update comes out as a grounded hunk and an injected instruction does not; it spends a few cents, so rerun it after Claude Code or model changes.

## What leaves your machine

The lanes, the question wizard and the judge send note text, and the lanes also send redacted transcript excerpts, to the model provider your `claude` CLI is configured for. Redaction is best effort and fails closed on cue words, but it **cannot** catch: rot13 or base85 text, look-alike characters, a bare random string shorter than 32 characters with no cue word, bare UUIDs, bare hex of 32 to 63 characters, or any secret that has no recognisable shape. Read `SECURITY.md` before you run it on a memory directory that holds anything you would not send to your provider.

Each digest also carries the session's working-directory path, and a judge question appears in the process list for the duration of its call. Children get only `PATH`, `HOME`, `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`, `AWS_DEFAULT_REGION` and `AWS_PROFILE`. If you authenticate with an API key variable, list its name in `MEMORY_SHEPHERD_PASS_ENV`; its value then goes to the child and the provider.

## Secret shapes are checked before anything is sent

Before a lane or the judge sends any note text to your provider, `memory_egress.py` scans the exact notes the model can read (the verified snapshot copy, or the staged directory). A block-tier secret shape, or an entry that could not be scanned (a symlink, an unreadable file), refuses the run with exit code 4 and prints counts and file names only. `--allow-secret-shapes` on `dream/memory_dream_run.py run` and `recall/memory_recall.py` overrides it for that run and prints one warning; there is deliberately no environment variable for it. The shapes are those defined in `engine/memory_scan.py`; `docs/PROVENANCE.md` section 5 names the public source of each. A secret with no recognisable shape still passes, and so do plain lines such as `password: value` (the digest filter is stricter than the note scan because a note may legitimately mention a password). File names are not scanned.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MEMORY_SHEPHERD_MEMORY_DIR` | memory directory the commands work on, overriding detection (the session-start hook ignores it; evidence reads transcripts from the directory's parent) | the current project's `~/.claude/projects/<project>/memory` |
| `MEMORY_SHEPHERD_HOME` | where snapshots, logs, digests, baselines and staged runs live (same filesystem as the memory directory) | `<Claude config dir>/memory-shepherd`, normally `~/.claude/memory-shepherd` |
| `MEMORY_SHEPHERD_CLAUDE_HOME` | the Claude Code configuration directory | Claude Code's own `CLAUDE_CONFIG_DIR` if set, else `~/.claude` |
| `MEMORY_SHEPHERD_LANE_MODEL` | model for the proposal lanes | `sonnet` |
| `MEMORY_SHEPHERD_JUDGE_MODEL` | model for the recall judge | `haiku` |
| `MEMORY_SHEPHERD_PASS_ENV` | comma-separated variable names to forward to the children | none |

`CLAUDE_CONFIG_DIR` is followed because Claude Code documents it as moving every `~/.claude` path; this tool's data root moves with it. After you set it, keep using your old data with `MEMORY_SHEPHERD_HOME=<old data root>` rather than moving snapshots (a moved snapshot no longer matches its recorded source and later snapshots and restores refuse it); only `pinned.txt` and `recall/questions.jsonl` are safe to copy. The headless children do not receive it, so if a child needs it to find your credentials, list `CLAUDE_CONFIG_DIR` in `MEMORY_SHEPHERD_PASS_ENV`. `CLAUDE_CODE_PROJECT_DIR_NAME` and the `autoMemoryDirectory` setting are not read; point `MEMORY_SHEPHERD_MEMORY_DIR` at that directory.

Model aliases resolve to different versions on different providers, so a recall baseline is only comparable with runs on the same provider. Pin a full model id in the two model variables for reproducible baselines.

## Where the Claude Code facts come from

`docs/PROVENANCE.md` lists, for every behaviour of Claude Code that this code or these docs rely on, the public page, the `claude --help` line, the observation, or the assumption behind it, and where this project deliberately differs.

## Tests

```
cd engine
python3 -I -m unittest discover -s tests -p 'test_memory_*.py'
python3 -I -m unittest discover -s recall/tests -p 'test_memory_recall*.py'
python3 -I -m unittest discover -s dream/tests -p 'test_dream_*.py'
```

`dev/harness/` holds mutation checks that edit a temporary copy of the engine and expect the tests to fail. Two of the session-start hook's mutants are documented as equivalent (they change memory use or an empty string, not behaviour).

## Not affiliated

memory-shepherd is an independent project. It is not made, endorsed or supported by Anthropic. "Claude" and "Claude Code" are names used by Anthropic, PBC (see its Trademark Guidelines at https://www.anthropic.com/legal/trademark-guidelines) and appear here only to say which tool this works with. Licensed under Apache-2.0 (see `LICENSE` and `NOTICE`).
