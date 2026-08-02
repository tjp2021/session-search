# SS task-based usability pilot

This pilot supplies observed human evidence. Automated acceptance cannot prove that unfamiliar people understand SS.

## Participants

Recruit five developers who use at least one AI coding tool. They must not have seen the SS implementation or demo.

Use a fresh macOS or Linux account with Python 3.11 or newer, `pipx`, Git, and network access. Give each participant only the public repository URL and the tasks below. Do not coach them.

## Setup

The facilitator runs these commands once per participant. They build a synthetic archive. They never read the participant's real session history.

```bash
git clone https://github.com/tjp2021/session-search.git
cd session-search
python tests/run_task_usability_acceptance.py --prepare-pilot ~/ss-pilot
```

The last command prints three `export` lines. Run those lines in the participant's shell, then build the index:

```bash
ss index --reset
```

Task 7 needs a separate account with no SS installed. The participant installs SS there with `pipx install session-search`.

## Tasks

1. Find the session where the resume changed.
2. Browse an older project without using search terms.
3. Recover work after its original agent closed.
4. Explain what happened and what should happen next.
5. Correct one mistaken project or session choice.
6. Repeat recovery in a 40-column terminal.
7. Install SS, diagnose a malformed store, and recover from a stale selector.

Installation, remembered-work search, older-project browsing, exact recovery, and card interpretation are mandatory.

## Recording sheet

Record only synthetic content and these fields for each participant and task:

| Field | Value |
|---|---|
| Participant code | P01 through P05 |
| Platform | macOS or Linux |
| Task | 1 through 7 |
| Completed without help | yes or no |
| Elapsed seconds | integer |
| Commands attempted | synthetic commands only |
| Error category | discovery, wording, selector, rendering, installation, or recovery |
| Help requested | none or sanitized description |
| Expected command effect | participant's short explanation |

Ask what `open`, `look at`, `continue`, and `archive` mean before the participant runs those commands.

Do not record names, real home paths, private sessions, terminal history, tokens, screen recordings, or raw transcripts.

## Acceptance

- At least four participants must complete every mandatory task without coaching.
- Every task must reach 80 percent unassisted success.
- Median completion must stay below two minutes per task.
- No participant may cause an unintended archive change or access outside the synthetic home.
- Any repeated failure must become an automated regression case before release.

A blocking installation, privacy, selector, or recovery failure reopens the release plan. Record smaller problems as ranked follow-up issues.
