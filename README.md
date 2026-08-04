# Session Search

## Recover the state, not only the transcript

You remember the work, but not the session. It may be in Claude Code, Codex,
another terminal, or a repository you have not opened all week.

Session Search lets you type what you remember. It searches your local AI
coding history. From local evidence, it reconstructs the best visible stopping
point and gives you the command to reopen or continue the work.

```bash
ss what was I doing with the robots battery pricing
```

```text
┌─ 5 · Codex · 3d ago ─────────────────────────────────────────┐
│ About: Update the robot battery pricing page.                │
│ State: The pricing table is complete.                        │
│ Resume: Verify the mobile layout.                            │
│ Open: ss open 5                                              │
└──────────────────────────────────────────────────────────────┘
```

Session Search is built for Mac developers who use Claude Code and Codex
across many repositories. It also reads Pi sessions. VS Code/Copilot and
Cursor support is partial, and exact native reopening is not available for
those two sources.

The proof is public: the [case study](docs/case-study.md),
[retrieval benchmark](#published-evidence),
[privacy tests](tests/test_secret_redaction.py), and
[current CI results](../../actions) ship with the repository.

Indexing, search, ranking, and evidence-based session cards run on macOS and Linux.
Optional model summaries send selected session text to OpenRouter only after
you turn on two settings. See
[What leaves your machine](#what-leaves-your-machine).

## Install

Session Search supports macOS, Linux, and Python 3.11 or newer. This design-partner
command installs the latest public `main` branch from GitHub:

```bash
pipx install "session-search[semantic] @ git+https://github.com/tjp2021/session-search.git"
```

This command is not version-pinned. The first semantic search can download the
local model and take longer. Use the smaller base install when you want to
start without that model:

```bash
pipx install "session-search @ git+https://github.com/tjp2021/session-search.git"
```

Without the `semantic` option, Session Search still supports exact-word and
typo-tolerant search.

Confirm the installation:

```bash
ss capabilities
ss demo
ss doctor
```

`ss demo` proves the installed command without reading your history. It creates
120 fictional records in a temporary database, exercises search and archive
behavior, and removes the data when finished.

`ss doctor` reports whether each local adapter found a store and parsed
documents. It does not print store paths or session text. Use
`ss doctor --strict` when a candidate store that yields no documents should
fail an automated check.

## Recover your first session

Describe something distinctive that you remember:

```bash
ss fresh robots battery pricing
```

`fresh` scans the local archives before searching, so its first run can take
longer on a large history. Each result explains:

- `About`: what the session concerned.
- `State`: what happened or where the work stands.
- `Resume`: the best visible continuation point.
- `Clue`: a useful path or distinctive search terms.
- `Open`: reopen the exact native session when supported.
- `Details`: inspect more indexed context without changing anything.

If the right session is number 4:

```bash
ss open 4
```

That's the recovery loop: describe the work, inspect the evidence, and open the
result number.

Run `ss` without search words when you want the most recent active sessions
instead.

Inside the interactive dashboard, you can also:

- Enter a number to get that session's open command.
- Enter `pN` to open an older project bucket.
- Enter `n` or `b` to move through a project with more than 200 sessions.
- Type search words to replace the dashboard with matching sessions.
- Enter `q` to leave.

## What to type

| What you want | Command |
| --- | --- |
| See recent active sessions | `ss` |
| Search from memory | `ss <what you remember>` |
| Force a fresh scan, then search | `ss fresh <what you remember>` |
| Reopen the selected native session | `ss open N` |
| Read more without reopening | `ss look at N` |
| Continue through another AI tool | `ss continue N in codex` |
| Show finished or archived sessions | `ss archived` |
| Archive a listed session manually | `ss archive N` |
| Return a listed session to active results | `ss unarchive N` |
| Inspect archive-state problems safely | `ss archive-audit` |
| Check index health | `ss status` |
| Check adapter parsing health | `ss doctor` |
| Show exact source capabilities | `ss capabilities` |
| Run the isolated synthetic demo | `ss demo` |

Replace `N` with the displayed result number.

`sessions` is an alias for `ss` when a more explicit command name is easier to
remember.

## Find a session from memory

Write what you remember in normal language:

```bash
ss Alex C# class structure
ss what was I doing with the ohmni robot batteries
ss claude system design beginner books
ss copilot github sign in
ss what did I work on yesterday
```

You don't need special query syntax or quotation marks. Include unusual names,
technologies, errors, files, or decisions when possible.

Relevance and recency work together. A recent weak match shouldn't outrank an
older session that clearly matches the query.

Use a source name when you remember the tool:

```bash
ss claude <search words>
ss codex <search words>
ss pi <search words>
ss copilot <search words>
ss cursor <search words>
```

## Use `fresh` when the latest work is missing

Normal searches use the existing local index immediately. Use `fresh` after
finishing a session, recovering from a restart, or noticing that a recent
message is absent:

```bash
ss fresh x402 lesson card grading
```

`fresh` rescans the local archives before searching. It takes longer, so it
isn't necessary for every query.

## Understand the actions

### `ss open N`

Use this when you want the original session.

Codex sessions open in Codex. Claude Code sessions open in Claude Code. SS
resolves the session's original working folder before producing the native
resume command.

### `ss look at N`

Use this when the card doesn't provide enough context.

It shows more indexed session material and doesn't reopen, archive, or modify
the session.

### `ss continue N in <tool>`

Use this when you want to move the work into another supported AI tool:

```bash
ss continue 2 in codex
ss continue 2 in claude
```

Claude Code and Codex can't natively reopen each other's sessions. SS instead
creates a local context packet and prints the command needed to start the
target tool in the correct folder.

Context packets are stored under:

```text
~/Library/Application Support/session-search/context-packets/
```

VS Code/Copilot and Cursor sessions remain searchable. Exact native reopening
is unavailable until a reliable native command exists, but packet-based
continuation can still work.

## Archive finished work

Archiving removes a finished session from the normal active dashboard. It
doesn't delete the original Claude Code or Codex session.

SS recognizes explicit instructions that clearly end the current session, for
example:

```text
Name and summarize this session and close it down.
Archive this current session.
We're finished, so close this chat.
```

The next SS refresh records the session as archived.

SS deliberately doesn't archive when the wording is unsafe or ambiguous. These
remain non-mutating:

- Questions such as "Should we close this session?"
- Negations such as "Don't close this session."
- Future plans such as "Close this after the tests pass."
- Quoted examples, pasted transcripts, code blocks, or attributed commands.
- Instructions about a file, issue, summary, note, browser tab, or other
  object inside the session.
- Conflicting instructions to close and continue.
- A plain request to name and summarize without a closing instruction.

You can always set the state explicitly:

```bash
ss archive 4
ss unarchive 4
ss archived
```

Opening or continuing an archived session returns it to active status.
`ss look at N` stays read-only.

Use this to inspect parser migrations and missing evidence without changing
status:

```bash
ss archive-audit
```

## Read a result card

A direct search groups matching turns by session and presents a useful
card:

```text
1. 27/07/26 10:42:18 — Repair SS archive reliability
   Found in: Codex
   Work folder: /Users/alex/projects/os/_shared/session-search
   Last touched: 2026-07-27 10:42

   Session card:
     Last message from you: <latest indexed user message>
     What this was: <session purpose>
     What happened: <visible progress>
     Next clue: <next action or blocker>

   What you can do:
     open exact session: ss open 1
     continue in Claude Code: ss continue 1 in claude
     read more: ss look at 1
```

The displayed timestamp comes from your latest indexed message, not the
session start or the assistant's final response.

Result numbers belong to the terminal or agent context that ran the search.
Another terminal can't silently retarget `ss open 1`. If a context has no
result list, SS fails closed and asks you to search there first.

## How SS works

SS reads local session records from:

- Codex thread metadata and prompt history.
- Claude Code project-session JSONL.
- Pi agent session JSONL.
- VS Code/Copilot empty-window chat JSONL and selected state entries.
- Selected Cursor chat state entries.

It writes a private SQLite index to:

```text
~/Library/Application Support/session-search/session-search.sqlite
```

The default hybrid search combines:

- SQLite full-text search for exact words.
- Local token and character similarity for typos and fuzzy wording.
- Local whole-session embeddings for broad meaning.
- Local per-turn embeddings for relevant details buried inside long sessions.

The semantic model is `BAAI/bge-small-en-v1.5`, loaded locally through
FastEmbed and cached under:

```text
~/Library/Application Support/session-search/models/
```

Run this after a large refresh when you want to build any missing
meaning-search records:

```bash
ss embed
```

## What leaves your machine

No session content leaves your Mac by default. Session cards use local
evidence. FastEmbed can download its model during the first semantic search,
but it doesn't upload your session text.

Model summaries are the opt-in exception. They make "About" lines noticeably
better, and they work by sending session text to OpenRouter. Both switches
must be set; a stray API key in your environment is not treated as consent:

```bash
export SS_SUMMARIES=openrouter
export OPENROUTER_API_KEY=...
```

With summaries on:

- **What is sent:** up to 4000 characters of a session's text, twice per card,
  once for "About" and once for the next action.
- **Credential shapes are stripped first:** API keys, bearer and CLI tokens,
  hex secrets, plaintext passwords, private keys, and passwords inside
  database URLs are replaced before the request is built. See
  `secret_patterns.py`.
- **What redaction can't do:** names, clients, file paths, and anything else
  without a machine-recognizable shape still leave the machine. Redaction
  narrows the credential risk; it isn't a privacy guarantee.
- **What is never sent:** tool calls and tool results, so command output and
  file contents that SS never indexed are also never transmitted.
- **Where it goes:** `openai/gpt-4.1-nano` through
  `https://openrouter.ai/api/v1/chat/completions`. Your OpenRouter account
  settings govern whether it's retained or trained on. SS has no say in that.
- **When it happens:** on `ss cards`, and for at most 10 visible sessions when
  you open the dashboard. Reading a cached card sends nothing.
- **How much:** routine backfill covers only the 60 newest sessions. An older
  session is summarized the first time a search surfaces it, and that summary
  is cached, so its text is sent once rather than never being read.

Turn summaries off again by unsetting either variable. Cards fall back to
local evidence lines, and a card built while summaries were off is rebuilt
automatically the next time they're on.

## Privacy and safety boundaries

- SS reads native session archives but doesn't edit or delete them.
- Archive state, cards, embeddings, selectors, and context packets stay in
  SS-owned private storage.
- Schema migrations create a SQLite backup first and retain the five newest
  backups.
- Status and audit-event writes commit together or roll back together.
- Storage, migration, unsupported-platform, and lock-timeout failures return
  distinct errors with recovery guidance.
- Suspicious, malformed, pasted, conditional, or ambiguous closing language
  leaves the session active.

## Troubleshooting

### A recent session is missing

```bash
ss fresh <what you remember>
```

### A result card is too vague

```bash
ss look at N
```

Then search again with a distinctive filename, person, technology, decision,
or error message.

### A result opens the wrong session

Run the search again in the same terminal, then use the new result number.
Selector mappings are isolated by terminal and agent context.

### Archive status looks wrong

```bash
ss archived
ss archive-audit
ss archive N
ss unarchive N
```

### Check index and semantic-search health

```bash
ss status
```

## Current limitations

- Codex user prompts and thread metadata are reliable, but complete assistant
  output reconstruction remains incomplete.
- VS Code/Copilot and Cursor exact native reopening isn't proven.
- Related-session suggestions don't yet form complete cross-tool work threads.
- Without model summaries turned on, cards use local evidence and can still
  be vague.
- One preserved legacy archive record lacks its original evidence identifier
  and requires manual review.

## Published evidence

The public retrieval corpus contains 120 synthetic sessions and 80 frozen
queries. On the recorded macOS benchmark:

| Mode | Top-one accuracy | Recall at five | MRR at ten |
| --- | ---: | ---: | ---: |
| Exact words (FTS) | 60.00% | 61.25% | 0.606 |
| Local fuzzy | 68.75% | 70.00% | 0.694 |
| Combined local search | 90.00% | 95.00% | 0.918 |

Top-one accuracy means the correct session appeared first. The combined search
did that for 72 of 80 queries. Recall at five means the correct session
appeared within the first five results. It did that for 76 of 80 queries. MRR
at ten rewards putting the right answer nearer the top of the first ten.

At 1,000 synthetic sessions, half of combined searches finished within 191
milliseconds. Ninety-five percent finished within 220 milliseconds after the
local meaning index existed. The first text index took 105 milliseconds. An
incremental refresh took 196 milliseconds. Building the meaning index once took
6.8 seconds.

These synthetic measurements provide a repeatable regression baseline. They
don't predict the exact speed or accuracy of every real archive or Mac.

The versioned source data is in `evidence/`. Run it again with:

```bash
.venv/bin/python tests/run_public_retrieval_eval.py --mode all
.venv/bin/python tests/benchmark_public.py --sessions 100 1000 --semantic
```

See `docs/case-study.md` for the engineering narrative and
`docs/architecture.md` for the system boundaries.

## Developer reference

The installed `ss` command runs the `session_search:main` entry point
declared in `pyproject.toml`. Normal use should go through `ss`. The lower-level commands
below support development and diagnostics.

Create the development environment first. A fresh clone has no `.venv`:

```bash
git clone https://github.com/tjp2021/session-search.git
cd session-search
python3 -m venv .venv
.venv/bin/pip install -e ".[semantic,dev]"
```

Run the test suite:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Run branch coverage:

```bash
.venv/bin/coverage run -m unittest discover -s tests -q
.venv/bin/coverage report
```

Run ranking evaluations:

```bash
.venv/bin/python tests/run_public_retrieval_eval.py --mode all \
  --expected evidence/public-retrieval-v0.1.0.json
.venv/bin/python tests/run_dashboard_latency_gate.py
```

`session_search.py eval` also exists, but its default case file does not ship
with the repository. It scores your own local sessions, so it reports every
case as failed on a fresh clone. Point it at your own file to use it:

```bash
.venv/bin/python session_search.py eval --mode fts --file <your-cases.json>
```

Refresh the index directly:

```bash
.venv/bin/python session_search.py index
```

Inspect internal counts:

```bash
.venv/bin/python session_search.py status
```

The archive-intent regression corpus is
`evals/archive-intent-corpus.json`. Independent holdouts stay outside Git and
are evaluated by recorded SHA-256 digest and aggregate results.

## Open product work

- Add more real ranking cases when the correct session isn't first.
- Improve local extraction of decisions, blockers, files, and next actions.
- Prove or reject exact VS Code/Copilot and Cursor reopening.
- Reconstruct Codex assistant output only after stable thread mapping passes
  visible-output evaluations.
- Cluster related sessions into a trustworthy cross-tool work thread.
- Continue suppressing sessions about SS itself unless the query concerns SS.
