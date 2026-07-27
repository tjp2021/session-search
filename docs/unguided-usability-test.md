# SS unguided usability test

## Participant

Use one developer who has used at least one AI coding harness and hasn't seen
the SS implementation or demo.

## Setup

Give the participant a fresh macOS account or machine with Python 3.11 or
newer, `pipx`, Git, and network access. Provide only the public repository URL.
Don't provide verbal coaching.

## Tasks

1. Install SS and verify that the command works.
2. Run the isolated demo and explain what SS does.
3. Inspect the capability matrix and identify which sources support native
   reopening.
4. Point SS at the provided synthetic home and find the x402 payment session
   from a fuzzy memory prompt.
5. Inspect the result, identify its state, and explain the next step.
6. Produce the native reopen instruction.
7. Create a Claude Code context packet from the Codex session.
8. Archive the session, confirm it leaves the active dashboard, then unarchive
   it.
9. Diagnose one deliberately missing semantic dependency using `ss status`.

## Evidence to record

For each task, record completion, elapsed time, commands attempted, observed
errors, and whether help was required. Ask the participant what they believed
each of `open`, `look at`, `continue`, and `archive` would do before running it.

## Acceptance

The participant must complete at least eight of nine tasks without coaching.
Installation, search, result inspection, and native reopen instructions are
mandatory. No command may read or write outside the synthetic home and data
directory.

Any blocking installation, privacy, selector, or command-understanding failure
reopens the release plan. Record smaller usability problems as ranked follow-up
issues.
