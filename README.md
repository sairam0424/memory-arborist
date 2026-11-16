# memory-arborist

*An independent project: not made, endorsed or supported by Anthropic.*

**A memory upgrade for your Claude Code harness.** Claude Code keeps an auto-memory directory for each project: an index (`MEMORY.md`) and one note per topic. Over months it fills with notes that are stale, contradicted or half-duplicated, and the index creeps toward the point where Claude Code stops reading it. memory-arborist looks after that directory the way an arborist looks after a tree: it takes a backup first, finds what has gone stale **using what you actually said in your own past sessions**, drafts small edits, checks that the clean-up did not make Claude forget anything, and shows you every change as a diff. Nothing is installed until you say yes, and any change can be undone.

> **Status: 0.1.0, invite-only beta.** The free half (snapshots, restore, lint, secret scan, the session-start hint, the doctor) is the stable part. The half that calls a model (the question wizard, the judge, the proposal lanes) is **experimental**: it has had one measured run on synthetic notes, one login route, and a ten-hunk cap per run. Read "Limits" below before you try it. macOS and Linux only.

## What you get

| | |
|---|---|
| **Snapshots and restore** | A hash-checked copy of the memory directory before anything touches it, and a one-file restore. |
| **A heads-up before the index overflows** | A read-only session-start hint when `MEMORY.md` is close to Claude Code's documented read limit. |
| **Clean-up from your own history** | Redacted digests of your past terminal sessions show which notes are stale, wrong or superseded. Model lanes with no tools draft the fixes. Every fix is checked mechanically against the notes and the evidence before you see it. |
| **A recall check** | An outside judge asks the same fixed questions of the old and the new memory and flags anything Claude would now forget or get wrong. A wizard proposes the question set from your own notes; you read it before it is installed. |
| **A secret check before anything is sent** | The notes a model is about to read are scanned first, and a run that holds one of 19 recognised credential shapes is refused (what that does and does not cover is below). |
| **A confirmation before anything changes** | A hook makes Claude Code ask you before the two commands that write to your notes. |

What a fix looks like in the report you read (this one is made up):

```
### h1  project-deploys.md  (edit)

Why: you moved deploys to Thursday.
Evidence:
  - session 11111111-…: "we moved the deploys to Thursday afternoons last week"
Old:   Deploys run every Tuesday at noon.
New:   Deploys run every Thursday afternoon.
```

## How safe is it, exactly?

What the code enforces, whatever Claude does:

- **Your notes are written by two commands only**: `memory_dream.py promote --apply` and `memory_hygiene.py restore --apply`. Everything else works on copies under the data root.
- **`promote --apply` needs the digest** a dry run printed for exactly the staged content, takes a fresh snapshot, archives each note it replaces, and refuses if a note changed since the snapshot.
- **The model lanes have no tools.** They run `claude --safe-mode --tools ""` and the run aborts if a lane ever reports a tool. What a lane returns is a proposal; nothing in it is trusted until it has been checked against the notes and your own words.
- **A secret check runs before any model call** (see below).

What rests on Claude Code's permission prompt and on you reading the diff:

- **The final yes.** The plugin's hook makes Claude Code ask before `promote --apply`, `restore --apply` and any `--allow-secret-shapes` command. That prompt appears in Claude Code's default and accept-edits permission modes, and it beats a broad "allow Bash" rule; Claude Code's documentation says it normally reaches you in auto mode too (with documented exceptions). In a headless `-p` run an ask counts as a refusal, and in a test on Claude Code 2.1.325 the hook's ask was still enforced under bypass-permissions mode (which Claude Code documents as skipping permission prompts); an interactive bypass session has not been tested. Claude Code's `disableAllHooks` setting switches the hook off. The hook reads commands written over several lines, with quotes, `$( )`, `${ }` or redirections, skips comments, and asks whenever it cannot read a command with confidence (a here-document, an unclosed quote); the scripts themselves refuse abbreviated flags. It matches command text, and Claude Code starts it only for commands whose raw text already carries the gated words, so a flag broken across lines inside a word (`--ap` then a backslash and a newline then `ply`) may never reach it (not tested live). A command assembled at run time (an `eval`, a variable holding the script name) is not recognised, and a command that only mentions these words, such as a commit message, gets a prompt too. It is a seat belt, not a sandbox.
- **The quality of a proposal.** The checks reject edits that the evidence does not support. They cannot know whether an edit that passes is the one you wanted. That is why you read the report.

## Install

0.1.0 is an invite-only beta, so the repository is private: you need read access first, and git on your machine must already hold a credential for it (for GitHub, `gh auth login` then `gh auth setup-git`). Then:

```
claude plugin marketplace add sairam0424/memory-arborist#stable
claude plugin install memory-arborist@memory-arborist
```

`#stable` is the branch that moves only when a release is published, so you follow releases and not the work in progress. Update later with `claude plugin marketplace update memory-arborist`, then `claude plugin update memory-arborist@memory-arborist`, and restart the session (or run `/reload-plugins`); Claude Code installs a new copy only when the plugin's version changes.

You start it by typing `/memory-arborist:tend` in the project whose memory you want to look after. The skill does not start on its own, and it asks you before every step that costs money. The plugin also adds a read-only `SessionStart` hook that prints nothing until the index is within about 1,500 characters or 20 lines of the linter's warning levels, then one fixed two-sentence message with four numbers (no note text, no paths).

## Your first run

Run it from the project whose memory you want to tidy. Claude drives these steps for you and asks before each one that spends money. To run a script yourself, use its full path from the project folder: the installed plugin lives under `~/.claude/plugins/cache/memory-arborist/memory-arborist/<version>/`, and the scripts are in its `engine/` folder (for example `python3 -I ~/.claude/plugins/cache/memory-arborist/memory-arborist/0.1.0/engine/doctor/memory_doctor.py`).

0. **Check the setup (free).** `doctor/memory_doctor.py` checks Python, Claude Code and its flags, the login variables the helper processes do not receive, the memory directory and the first-run files, and prints what to fix. Its output holds names, versions and counts only (the project folder is printed as `<project>`, the home directory as `~`), so it is safe to paste into an issue. `doctor/memory_doctor.py --live` adds one real call (at most $0.05, usually a fraction of a cent) that proves your login and the output format; add `--model <your judge model>` to test the model the judge uses.
1. **Safety net.** A verified snapshot, then a lint and a secret scan. Free.
2. **Evidence.** Redacted digests of your past terminal sessions that have been idle for at least six hours, newest first. Free.
3. **Set up the two small files.** `memory_dream.py pin --none` (recommended for a first run) or `--all` creates `pinned.txt`: the index lines the run must never edit. `--all` pins every non-blank line of `MEMORY.md`, so the run can correct a note but never the index line that repeats the old fact. The question wizard proposes the 25 to 32 recall questions from your own notes; you read them and accept. About $1 for a memory of a few hundred notes.
4. **Baseline.** The judge asks the questions of your current memory and saves the answers. Cents.
5. **Proposals.** Tool-less lanes read the digests and the notes and draft edits; everything that survives the checks is staged outside your memory directory.
6. **Recall check.** The judge asks the same questions of the staged copy. `REGRESSIONS n=0` and `RESURRECTED n=0` are expected. A regression on a question about a fact this run changed is the update working, not forgetting, so read the failing question against the report; a question that still passes although a hunk changed its fact usually means stale text remains elsewhere (a run stages at most ten hunks). The check finds forgetting, not leftover staleness.
7. **You read the report** and say yes or no to the whole run. Only then does `promote --apply` write to your notes, after a fresh snapshot. Afterwards the judge checks the notes that are now live against the same baseline (that check is part of the ceilings below). A run stages at most ten hunks, and one stale note can need three (its body, its description line and its index line), so a memory with many stale notes takes several rounds: promote, then run again.
8. **Undo.** `memory_hygiene.py snapshots` lists the backups; `restore` puts one file back.

## What it costs

Every paid command prints an `ESTIMATE` line first: how many model calls, the cap on each, and the ceiling at which the run stops. Default ceilings are $3 for the wizard, $5 for each judge run and $8 for the lanes, about $26 if every step used its full ceiling. Real runs cost far less: on one author's memory of about 400 notes, a full pass that found nothing to change cost about $0.33 and the wizard about $1.30 for eight batches. Yours will differ with the model you pick and the amount of history you have. The plugin itself adds about 100 tokens to every session and about 2.8k tokens when you start the skill (Claude Code's own estimate, from `claude plugin details memory-arborist`).

## What leaves your machine

The lanes, the question wizard and the judge send note text, and the lanes also send redacted transcript excerpts, to the model provider your `claude` CLI is configured for. Redaction is best effort and fails closed on cue words, but it **cannot** catch: rot13 or base85 text, look-alike characters, a bare random string shorter than 32 characters with no cue word, bare UUIDs, bare hex of 32 to 63 characters, or any secret that has no recognisable shape. Each digest also carries the session's working-directory path, and a judge question appears in the process list for the duration of its call. Read `SECURITY.md` before you run it on a memory directory that holds anything you would not send to your provider.

The helper processes get only `PATH`, `HOME`, `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`, `AWS_DEFAULT_REGION` and `AWS_PROFILE`. If you log in with an API key, a proxy, Vertex or a gateway, `doctor` names the variables that are set but not passed and prints the exact `MEMORY_ARBORIST_PASS_ENV=...` line; a listed variable's value then goes to the helper and on to your provider.

### The secret check, precisely

Before a lane or the judge sends note text, `memory_egress.py` scans the exact notes the model can read. It **refuses the run (exit code 4)** when it finds one of the 19 block-tier shapes in `engine/memory_scan.py` (AWS keys, GitHub, Anthropic, OpenAI, Stripe, Slack, npm and Google tokens, private-key blocks, JWTs, bearer tokens, database URLs and `secret = "…"` style assignments) or an entry it could not scan (a symlink, an unreadable file). It prints counts and file names only. `--allow-secret-shapes` overrides it for one run and prints a warning; there is deliberately no environment variable for it.

It does **not** refuse, because they are warnings in `memory_hygiene.py scan` rather than blocks: environment-style lines such as `SOME_KEY=value`, quoted key and value pairs, long hex strings, high-entropy strings, email addresses and phone numbers. Run `memory_hygiene.py scan` first and read what it reports. A secret with no recognisable shape passes, and file names are not scanned.

## Your data, and how to remove it

Everything lives under the data root, `<Claude config dir>/memory-arborist/<project>/` (normally `~/.claude/memory-arborist/<project>/`), outside the plugin folder so an update does not touch it: snapshots (full copies of your notes), evidence digests (redacted excerpts of your sessions), baselines, your question set and the proposals the wizard wrote (`recall/`), `pinned.txt`, staged runs and their inputs (`dream/`), the old copy of every note a promote or restore replaced (`archive/`), and a log. Nothing in it is ever deleted by this tool, so the folder only grows: `memory_hygiene.py prune-plan` prints removal lines for old snapshots, and `memory_evidence.py purge-plan` prints the one command that removes the whole evidence folder (digests rebuild from your transcripts at no cost). Nothing prints a plan for `archive/`, `dream/` or `recall/proposed/`: remove those by hand.

To remove everything: `claude plugin uninstall memory-arborist@memory-arborist`, `claude plugin marketplace remove memory-arborist`, and then delete the data root yourself (it holds copies of your notes and excerpts of your sessions, so uninstalling the plugin does not delete it).

## Limits and compatibility

- **Platforms:** macOS and Linux (it relies on POSIX file semantics). Windows is not supported.
- **Python:** 3.11 or newer, standard library only (CI covers 3.11 to 3.15).
- **Claude Code:** the session-start hook needs 2.1.139 or newer and the model-backed steps need 2.1.169 or newer (`--safe-mode`). Tested with 2.1.322 and 2.1.325; older and newer versions are untested.
- **Login routes:** tested through Amazon Bedrock only. Subscription, API-key, Vertex and gateway logins are untested; `doctor --live` tells you in one cheap call whether yours works.
- **Which sessions it learns from:** sessions started from the terminal (entrypoint `cli`). Headless runs stay out on purpose; an editor or desktop session is left out unless you pass `--include-entrypoint NAME` (the build prints the names it skipped; never name an `sdk-*` entrypoint, those are headless runs and are refused). A session is dated by its first record, so a long session that began before a note was written counts as older than that note (the safe direction).
- **Which memory:** the current project's directory, found from your working directory. `autoMemoryDirectory` and `CLAUDE_CODE_PROJECT_DIR_NAME` are not read; point `MEMORY_ARBORIST_MEMORY_DIR` at the directory instead.
- **The question set:** the judge needs 25 to 32 questions. The wizard proposes recall and negative questions only; you add questions about facts you know have changed by hand (see `docs/recall-questions.md`). A memory with fewer than about 25 notes worth asking about cannot reach the floor yet.
- **Models:** the lanes default to `sonnet` and the judge to `haiku`, aliases that resolve to different versions on different providers, so a baseline is only comparable with runs on the same provider. Pin full model ids in the two model variables for reproducible baselines.
- **Quality is not yet measured against known-bad notes.** The checks stop unsupported edits; how many real stale notes it finds, and how often it proposes something you would reject, has been measured only on the author's own memory. `dev/acceptance/run_seeded.py` is the test that measures it: a synthetic memory with eight planted stale facts, eight true ones (four of them with a session in which the person confirms them, a tempting false edit) and four traps (a claim only an assistant made, a topic merely mentioned), plus an instruction aimed at a model planted inside one of the stale notes so that a writer fixing the real fact has to read it. **Two live runs** (Sonnet 5.5 through Bedrock, about $0.39 each): the lanes found all eight stale facts both times; every accepted hunk was a correct edit (ten of ten each time, one or two of them index lines); no true note and no trap was edited; the planted instruction was not followed; the verifier rejected the two hunks that went beyond the evidence (one rewrote a sentence the person never mentioned, one dropped the old host name from an index line). A run stages at most ten hunks and each stale note can need three (the body, the description line, the index line), so five notes were fixed and three were deferred to the next run. That is two runs on a synthetic memory in which every fact has a clean answer and the new value is typed verbatim: treat it as evidence that the checks work, not as a guarantee about your notes.

## When something goes wrong

| You see | It means |
|---|---|
| `FAIL` or `WARN` from `doctor` | Follow its `fix:` line. |
| exit 4, `REFUSED` | The notes hold a block-tier secret shape or an unscannable entry; nothing was sent. Remove the text and take a fresh snapshot. |
| exit 5, `INCOMPLETE` | The run finished but some model calls failed (the report starts with a banner). Fix the cause named on the `LANE-ERROR` lines, often a login, then run it again; do not read "no proposals" as "nothing to change". |
| `ABORTED the first 6 model calls all failed` | The judge could not get any answer. Run `doctor --live --model <your judge model>`. |
| `did not report its tool list` | This Claude Code version prints its output in a different format than expected. Run `doctor --live` and report the version. |
| `digests=0` | No idle terminal session yet, or every session was left out; read the `skipped_*` counts on the `BUILD` line (`recent` means idle for under six hours) and the `NOTE` lines. |

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MEMORY_ARBORIST_MEMORY_DIR` | memory directory the commands work on, overriding detection (the session-start hook ignores it; evidence reads transcripts from the directory's parent) | the current project's `~/.claude/projects/<project>/memory` |
| `MEMORY_ARBORIST_HOME` | where snapshots, logs, digests, baselines and staged runs live (same filesystem as the memory directory) | `<Claude config dir>/memory-arborist`, normally `~/.claude/memory-arborist` |
| `MEMORY_ARBORIST_CLAUDE_HOME` | the Claude Code configuration directory | Claude Code's own `CLAUDE_CONFIG_DIR` if set, else `~/.claude` |
| `MEMORY_ARBORIST_LANE_MODEL` | model for the proposal lanes | `sonnet` |
| `MEMORY_ARBORIST_JUDGE_MODEL` | model for the recall judge | `haiku` |
| `MEMORY_ARBORIST_PASS_ENV` | comma-separated variable names to forward to the helper processes | none |

`CLAUDE_CONFIG_DIR` is followed because Claude Code documents it as moving every `~/.claude` path; this tool's data root moves with it. After you set it, keep using your old data with `MEMORY_ARBORIST_HOME=<old data root>` rather than moving snapshots (a moved snapshot no longer matches its recorded source and later snapshots and restores refuse it); only `pinned.txt` and `recall/questions.jsonl` are safe to copy. The helper processes do not receive `CLAUDE_CONFIG_DIR`, so if one needs it to find your credentials, list it in `MEMORY_ARBORIST_PASS_ENV`.

## Where the Claude Code facts come from

`docs/PROVENANCE.md` lists, for every behaviour of Claude Code that this code or these docs rely on, the public page, the `claude --help` line, the observation, or the assumption behind it, and where this project deliberately differs.

## Tests

```
cd engine
python3 -I -m unittest discover -s tests -p 'test_memory_*.py'
python3 -I -m unittest discover -s recall/tests -p 'test_memory_recall*.py'
python3 -I -m unittest discover -s dream/tests -p 'test_dream_*.py'
python3 -I -m unittest discover -s doctor/tests -p 'test_doctor_*.py'
```

`dev/harness/` holds mutation checks that edit a temporary copy of the engine and expect the tests to fail. A few timing assertions in the safety-net suite can fail on a heavily loaded machine; rerun when it is idle.

## Not affiliated

memory-arborist is an independent project. It is not made, endorsed or supported by Anthropic. "Claude" and "Claude Code" are names used by Anthropic, PBC (see its Trademark Guidelines at https://www.anthropic.com/legal/trademark-guidelines) and appear here only to say which tool this works with. Licensed under Apache-2.0 (see `LICENSE` and `NOTICE`).
