# Recovering work across AI coding harnesses

## Problem

Switching among Claude Code, Codex, Copilot, and Cursor creates fragmented
session history. A restart can leave the developer remembering the project but
not the harness, folder, session identifier, or exact wording needed to find
it.

The first version of SS searched local transcripts. That was useful, but it
didn't solve recovery. Results could be technically relevant while failing to
explain what happened, what remained, or how to reopen the work.

## System

SS now indexes local harness records into SQLite and combines exact full-text
search, character-level fuzzy matching, whole-session embeddings, and per-turn
embeddings. Results are grouped by native session and ranked with bounded
recency, source intent, query coverage, and filters for worker sessions,
expanded instructions, and transcript dumps.

Each result includes the original folder, session purpose, visible progress,
continuation evidence, and the correct next action. Claude Code and Codex can
reopen their native sessions. Cross-harness continuation creates a local
context packet. Archive state removes completed work from the active dashboard
without deleting native history.

## Engineering decisions

SS keeps all retrieval local. Deterministic cards were chosen over generated
summaries because they remain fast, traceable, private, and reproducible.
Unsupported editor behavior appears in an executable capability matrix instead
of being implied by documentation.

Reliability work covered nested transactions, atomic migrations, backups,
schema validation, lock contention, process termination, read-only storage,
SQLite full conditions, selector isolation, malformed input, and archive-state
recovery.

## Evaluation

The public corpus contains 120 synthetic sessions and 80 frozen queries split
across exact, fuzzy, semantic, and time-oriented recovery.

| Mode | Top-one accuracy | Recall at five | MRR at ten |
| --- | ---: | ---: | ---: |
| Full text | 60.00% | 61.25% | 0.606 |
| Local fuzzy | 68.75% | 70.00% | 0.694 |
| Hybrid | 90.00% | 95.00% | 0.919 |

Hybrid search preserved perfect exact-identifier retrieval, raised fuzzy
top-one accuracy from 15% to 95%, and raised semantic top-one accuracy from
35% to 65%.

At 1,000 synthetic sessions, hybrid query latency measured 191 ms p50 and 220
ms p95 after embeddings were available. The one-time embedding build took 6.8
seconds on the recorded Apple Silicon Mac.

## What failed

Adversarial testing repeatedly found defects that the original suite missed:
nested rollbacks erased outer work, partial migrations were trusted, pasted
commands could archive sessions, typo prefilters disagreed with the parser,
and weak evidence produced malformed cards.

Those failures were retained as regressions. Independent archive-intent batches
were recorded even when they failed rather than being relabeled after the
fact.

## Current boundary

This release supports macOS. Claude Code and Codex have proven native reopening.
VS Code/Copilot and Cursor remain searchable and support packet continuation,
but exact native reopening isn't claimed. Codex assistant output is partially
available because its low-level telemetry doesn't yet provide a stable,
complete thread mapping.
