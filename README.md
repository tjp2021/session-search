# SS

SS finds, understands, reopens, and organizes your local Claude Code, Codex,
VS Code/Copilot, and Cursor sessions.

Use it when a terminal restarted, a session disappeared, or you remember the
work but not which AI tool or folder contained it.

SS is local and private. It doesn't send session text to OpenAI, Anthropic,
GitHub, or another external service.

## Install

SS supports macOS and Python 3.11 or newer.

Install the current release from GitHub:

```bash
pipx install "session-search[semantic] @ git+https://github.com/tjp2021/session-search.git"
```

The semantic extra installs the local embedding backend. SS still supports
full-text and fuzzy search when that extra isn't installed.

Confirm the installation:

```bash
ss --help
ss capabilities
ss demo
```

`ss demo` creates a temporary database containing 120 synthetic records. It
doesn't read your real session history, and it removes the temporary data when
finished.

## Start here

Run this from any folder:

```bash
ss
```

After a restart, that is the whole recovery loop. SS prints a short grouped
list, not a giant spreadsheet:

- Project headers first (`Organic Growth`, `Personal / Career`, ...)
- Threads under each project, with open numbers
- One short `About` / `State` / `Resume` block per thread
- A small footer of older projects still saved, if any

Every thread still keeps the recovery fields:

- `About`: what the session concerned.
- `State`: what happened or where the work stands.
- `Resume`: the best visible continuation point.
- `Clue`: only when a real path or distinctive fact helps
- `Open`: reopen the exact native session when supported

If the right session is number 4:

```bash
ss open 4
```

That's the normal workflow: run `ss`, identify the session, then open its
number. Closed terminals do not erase sessions. Open numbers belong only to
the screen you just saw, so run `ss` again in a new terminal before opening.

Control how many threads appear:

```bash
ss --limit 5
```

`--limit` is exact. If you ask for 5 sessions, SS shows 5 open numbers. Older
projects can still show in a short footer so they do not vanish only because
they sit outside the newest few threads.

Inside the interactive dashboard, you can also:

- Enter a number to get that session's open command.
- Type search words to replace the dashboard with matching sessions.
- Enter `q` to leave.

## What to type

| What you want | Command |
| --- | --- |
| See the project work map | `ss` |
| Show exactly N newest threads | `ss --limit N` |
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

Run this after a large refresh when you want to backfill missing semantic
vectors:

```bash
ss embed
```

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
- Session-card summaries use local evidence and can still be vague.
- One preserved legacy archive record lacks its original evidence identifier
  and requires manual review.

## Published evidence

The public retrieval corpus contains 120 synthetic sessions and 80 frozen
queries. On the recorded macOS benchmark:

| Mode | Top-one accuracy | Recall at five | MRR at ten |
| --- | ---: | ---: | ---: |
| FTS | 60.00% | 61.25% | 0.606 |
| Local fuzzy | 68.75% | 70.00% | 0.694 |
| Hybrid | 90.00% | 95.00% | 0.919 |

At 1,000 synthetic sessions, hybrid query latency measured 191 ms p50 and 220
ms p95 after the local embeddings were built. Cold indexing took 105 ms,
incremental refresh took 196 ms, and the one-time embedding build took 6.8
seconds.

The versioned source data is in `evidence/`. Run it again with:

```bash
.venv/bin/python tests/run_public_retrieval_eval.py --mode all
.venv/bin/python tests/benchmark_public.py --sessions 100 1000 --semantic
```

See `docs/case-study.md` for the engineering narrative and
`docs/architecture.md` for the system boundaries.

## Developer reference

The installed `ss` command launches `session_search.py` through
`ss_launcher.sh`. Normal use should go through `ss`. The lower-level commands
below support development and diagnostics.

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
.venv/bin/python session_search.py eval --mode fts
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
