# Writing the recall question set

The outside recall judge asks a fixed set of questions of fresh `claude` children that are given Read, Grep and Glob and pointed at your memory directory (your Claude Code permission rules still apply to them), and grades each answer with regular expressions. It tells you whether a staged edit made the memory worse (a regression) or brought back a fact you had replaced (a resurrection). The question wizard (below) can propose most of the file for you to read and accept; you add the questions only you can know (stale facts, replaced values) by hand.

**File:** `<data root>/<project>/recall/questions.jsonl` (the data root is `~/.claude/memory-shepherd`, or under `CLAUDE_CONFIG_DIR`, unless `MEMORY_SHEPHERD_HOME` is set; `--questions` points the judge at another file). One JSON object per line, 25 to 32 questions, unique `id`s.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | unique string |
| `kind` | yes | `recall`, `drift`, `supersede` or `negative` (below) |
| `question` | yes | what the child is asked; it answers briefly from memory only |
| `must` | yes | list of regexes; every one must match the answer |
| `forbid` | no | list of regexes; none may match |
| `truth` | for `drift` | where the true value can be checked (a note for you; the judge does not read it) |

Regexes are Python `re` patterns searched with the `DOTALL` flag. Write them case-insensitive with `(?i)` where wording may vary.

## The question wizard

`recall/memory_recall_wizard.py propose` reads the newest verified snapshot, sends batches of notes (10 notes or 36,000 characters per batch, the first 6,000 characters of each note; 8 batches unless you pass `--max-chunks`, up to 40; each batch costs about 15 cents) to tool-less `claude` children, and keeps only the candidates that pass these checks:

- the quoted evidence is a literal span of the note, and it and the question hold no secret shape, personal data or control, invisible or line-separator characters;
- every `must` key is written in a small safe grammar (literal characters, `\d`, `\s`, simple classes, `|`, `?` after an item, bounded repeats, at most one open-ended repeat and only on a digit or a space, no lookarounds, anchors or nested groups, and at least three literal letters or digits in every top-level alternative), matches the quote and does not match the question; a `forbid` key does not match the question or the text the `must` keys matched, and does not match the quote either, since a correct answer drawn from the note would fail it;
- a `negative` question carries probe words that appear in no note, and its key is written by the script;
- the question starts with a letter or digit and holds none of the characters ~ ` $ < > | \ { } (slashes and ordinary punctuation are allowed); a negative question may not contain its own answer phrase ("I do not know" and similar).

The judge grades at most the first 8,000 characters of any answer. It then picks about 28 questions (`recall` and up to 3 `negative`; at most 2 per note, spread over the notes) and writes a proposal and a report under `<data root>/<project>/recall/proposed/`, and prints a `DIGEST`. The `COVERAGE` line says how many notes were asked about: with hundreds of notes the default covers a fraction, and `--max-chunks` widens it.

Read the report. The judge and `accept` need 25 to 32 questions in total, so with the default of 28 you can delete up to 3 lines (or add up to 4 hand-written ones); pass `--target 32` to `propose` for more room to add. Delete the question lines you do not want from the proposal, then install it with `recall/memory_recall_wizard.py accept --file <proposal> --expect <digest>` (the first 16 characters of the file's SHA-256, so an edit after you read it needs the new digest; `--replace` copies an existing set to a backup first). The wizard proposes only `recall` and `negative` questions: only you know which facts are stale today (`drift`), and a fact whose old value you replaced (`supersede`) is by definition no longer in the notes, so add those by hand (a live trial of wizard-written `supersede` questions failed both of two at baseline, because a correct answer mentions the old value the note records); until you add `supersede` questions, `RESURRECTED` in a staged check has nothing to measure. The checks cannot tell whether a question measures your notes rather than what the model already knows, or whether a key will accept every correct paraphrase: the first baseline run's empty-memory and noise arms decide that, and a key that rejects a correct answer is yours to loosen.

## Kinds

- **recall**: a stable fact that a note holds. After a good edit it must still be answerable.
- **supersede**: a fact whose old value you have replaced. `must` matches the new value, `forbid` matches the old one; an edit that brings the old value back is counted as a resurrection.
- **drift**: a fact where you know the memory is wrong today. The run reports how many drift probes a staged edit fixed.
- **negative**: something the memory does not hold. `must` is a pattern such as `(?i)do not know`. These guard against the memory (or the model) inventing answers.

## Making keys that measure memory, not the model

A baseline run refuses a set that cannot tell your memory from nothing:

- The judge also asks every question once against an **empty** memory. If more than 3 graded questions of the kinds other than `negative` still pass, the set measures what the model already knows and must be rewritten. Ask for facts only your notes can contain (a private project name, a number you chose, a decision and its reason).
- Each question is asked twice against the live memory. If more than 2 questions flip between those runs, the set is too noisy. Loosen nothing silently: make the question more specific, or make `must` accept every correct phrasing.
- A key that rejects a correct answer is a bug in the key, not in the memory. Read the failing answers of a baseline run before you trust the set. (`recall/memory_recall.py --selftest` spends nothing but only checks the grader itself against built-in canned answers, not your keys.)

Aim for a mix: most `recall`, several `supersede`, a few `negative`, and a `drift` probe for each fact you suspect is stale.

## Example

```json
{"id": "r01", "kind": "recall", "question": "Which day does the team deploy, and who owns deploys?", "must": ["(?i)thursday", "(?i)platform team"], "forbid": ["(?i)tuesday"]}
{"id": "s01", "kind": "supersede", "question": "What is the staging database called now?", "must": ["(?i)stg-orders-2"], "forbid": ["(?i)stg-orders-1"]}
{"id": "n01", "kind": "negative", "question": "What is the user's favourite colour?", "must": ["(?i)do not know|not in (my )?memory"], "forbid": []}
```
