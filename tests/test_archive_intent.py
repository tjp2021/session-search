import json
import pathlib
import unittest

from archive_intent import IntentKind, PARSER_VERSION, classify_session_intent, has_session_intent_candidate


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "archive-intent-corpus.json"


class ArchiveIntentCorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(CORPUS.read_text(encoding="utf-8"))

    def test_corpus_targets_current_parser(self):
        self.assertEqual(self.payload["parser_target"], PARSER_VERSION)

    def test_regression_corpus(self):
        failures = []
        for case in self.payload["cases"]:
            actual = classify_session_intent(case["text"]).kind.value
            if actual != case["expected"]:
                failures.append((case["id"], case["expected"], actual))
        self.assertEqual(failures, [])

    def test_sealed_holdout(self):
        failures = []
        for case in self.payload["holdout"]:
            actual = classify_session_intent(case["text"]).kind.value
            if actual != case["expected"]:
                failures.append((case["id"], case["expected"], actual))
        self.assertEqual(failures, [])

    def test_every_positive_case_passes_candidate_prefilter(self):
        positives = [
            case
            for partition in (self.payload["cases"], self.payload["holdout"])
            for case in partition
            if case["expected"] != "none"
        ]
        self.assertEqual(
            [case["id"] for case in positives if not has_session_intent_candidate(case["text"])],
            [],
        )

    def test_input_mutations_cancel_close_intent(self):
        mutations = (
            "Do not close this session.",
            "Should we close this session?",
            'Use "close this session" as an example.',
            "If testing passes, close this session.",
            "Close this session later.",
            "Close this session, but keep going.",
        )
        for text in mutations:
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, IntentKind.NONE)

    def test_production_candidate_never_discards_parser_positive_typos(self):
        examples = (
            "Cloze this session.",
            "Archve this session.",
            "Finsih this session.",
            "Resme this session.",
            "Contineu this session.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNot(classify_session_intent(text).kind, IntentKind.NONE)
                self.assertTrue(has_session_intent_candidate(text))

    def test_pasted_and_attributed_commands_are_not_terminal_intent(self):
        examples = (
            "Here is the command:\nclose this session",
            "The user requested:\nclose this session",
            "Example:\n```\nclose this session\n```",
            "> close this session",
            "Transcript:\nAlex: close this session",
            '{"instruction": "close this session"}',
            "He wrote:\nclose this session",
            "Copied command:\nclose this session",
            "EMAIL FROM ALEX:\nclose this session",
            'close this session"',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, IntentKind.NONE)

    def test_actions_on_objects_inside_a_session_are_not_terminal_intent(self):
        examples = (
            "Finish analyzing this session.",
            "Archive the notes for this session.",
            "Close the issue in this session.",
            "End the task in this session.",
            "Close this session summary document.",
            "Archive the file from this session.",
            "Finish debugging this session.",
            "Archive output from this session.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, IntentKind.NONE)

    def test_consumed_independent_holdout_failures_are_regressions(self):
        supported = {
            "Please archive our present chat.": IntentKind.CLOSE,
            "Terminate this conversation; we're done.": IntentKind.CLOSE,
            "Shut down the curent session now.": IntentKind.CLOSE,
            "Mark this thread closed and do not continue it.": IntentKind.CLOSE,
            "Seal up this conversation for good.": IntentKind.CLOSE,
            "Close this chat context immediatly.": IntentKind.CLOSE,
            "Archive the session we're in—no more replies.": IntentKind.CLOSE,
            "Please pick this session back up now.": IntentKind.RESUME,
            "Unarchive and resume our current chat.": IntentKind.RESUME,
            "Carry on with the curent conversation.": IntentKind.RESUME,
            "Restart this same thread and continnue.": IntentKind.RESUME,
            "Re-open this session; let's proceed.": IntentKind.RESUME,
        }
        for text, expected in supported.items():
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, expected)
                self.assertTrue(has_session_intent_candidate(text))

    def test_expanded_vocabulary_keeps_conservative_boundaries(self):
        negatives = (
            "Should we terminate this conversation?",
            "Do not terminate this conversation.",
            "Terminate this conversation tomorrow.",
            'Use "seal up this conversation" in the documentation.',
            "The user requested:\nmark this thread closed",
            "If support agrees, pick this session back up.",
            "Do not pick this session back up.",
            "Can we carry on with this conversation?",
            "Restart this thread next week.",
            "Archive the chat transcript.",
            "Mark the issue closed in this session.",
            "Seal the output from this conversation.",
        )
        for text in negatives:
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, IntentKind.NONE)

    def test_consumed_second_independent_holdout_failures_are_regressions(self):
        supported = {
            "Put an end to this session now.": IntentKind.CLOSE,
            "Archive this active thread; I am finished.": IntentKind.CLOSE,
            "Shut this current chat down for good.": IntentKind.CLOSE,
            "Finish and close our ongoing thread.": IntentKind.CLOSE,
            "Consider this current conversation closed.": IntentKind.CLOSE,
            "Stop here and archive this chat.": IntentKind.CLOSE,
            "Bring the current session back and proceed.": IntentKind.RESUME,
            "Let's pick up this conversation again.": IntentKind.RESUME,
            "Restore and resume the thread we're in.": IntentKind.RESUME,
            "Please un-archive this current chat.": IntentKind.RESUME,
            "Reusme our ongoing session now.": IntentKind.RESUME,
            "Open this session back up; keep going.": IntentKind.RESUME,
        }
        for text, expected in supported.items():
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, expected)
                self.assertTrue(has_session_intent_candidate(text))
        self.assertIs(
            classify_session_intent(
                "Close this session, but keep this same session open."
            ).kind,
            IntentKind.NONE,
        )

    def test_consumed_third_independent_holdout_failures_are_regressions(self):
        supported = {
            "Conclude the session I’m in now.": IntentKind.CLOSE,
            "Archive our live exchange at this point.": IntentKind.CLOSE,
            "Bring this active dialogue to a final close.": IntentKind.CLOSE,
            "Please retire the thread open between us.": IntentKind.CLOSE,
            "End this very session and leave it ended.": IntentKind.CLOSE,
            "File away the chat we’re using now.": IntentKind.CLOSE,
            "Declare our current discussion finished.": IntentKind.CLOSE,
            "Permanently close the conversation on screen.": IntentKind.CLOSE,
            "Put our ongoing dialogue into the archive.": IntentKind.CLOSE,
            "Cease this thread and mark it complete.": IntentKind.CLOSE,
            "We are finished here—end the current session.": IntentKind.CLOSE,
            "Close down this exact conversation.": IntentKind.CLOSE,
            "Finalize and terminate our present exchange.": IntentKind.CLOSE,
            "Dismiss this session as completed.": IntentKind.CLOSE,
            "Please end the conversation I’m speaking in.": IntentKind.CLOSE,
            "Retire this curent thread right away.": IntentKind.CLOSE,
            "Reactivate the session I’m in.": IntentKind.RESUME,
            "Take this archived conversation live again.": IntentKind.RESUME,
            "Return to our active thread and proceed.": IntentKind.RESUME,
            "Restore this same dialogue so we can go on.": IntentKind.RESUME,
            "Bring our current chat out of the archive.": IntentKind.RESUME,
            "Start this paused session moving again.": IntentKind.RESUME,
            "Reinstate the conversation currently before us.": IntentKind.RESUME,
            "Carry forward this exact session.": IntentKind.RESUME,
            "Wake this thread back up and proceed.": IntentKind.RESUME,
            "Unseal our present conversation and go on.": IntentKind.RESUME,
            "Revive this sesson and keep talking.": IntentKind.RESUME,
            "Return this current exchange to an open state.": IntentKind.RESUME,
        }
        for text, expected in supported.items():
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, expected)
                self.assertTrue(has_session_intent_candidate(text))
        self.assertIs(
            classify_session_intent(
                "Close the browser tab containing this chat."
            ).kind,
            IntentKind.NONE,
        )

    def test_consumed_distribution_holdout_failures_are_regressions(self):
        supported = {
            "End this chat and don't reopen it.": IntentKind.CLOSE,
            "Please permanently archive this thread.": IntentKind.CLOSE,
            "I'm all set—archive this chat for good.": IntentKind.CLOSE,
            "Kill this current session permanently.": IntentKind.CLOSE,
            "Please archive this whole session and be done with it.": IntentKind.CLOSE,
            "We're finished, so close this chat forever.": IntentKind.CLOSE,
            "Archive our current convo for good.": IntentKind.CLOSE,
            "Bring this thread back from the archive.": IntentKind.RESUME,
            "Unarchive this convo, I need to add something.": IntentKind.RESUME,
            "Get this current thread going again.": IntentKind.RESUME,
            "Take this chat out of archive and continue.": IntentKind.RESUME,
        }
        for text, expected in supported.items():
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, expected)
                self.assertTrue(has_session_intent_candidate(text))
        # Questions remain non-mutating even when followed by an imperative
        # pronoun. The approved parser policy requires a direct terminal form.
        self.assertIs(
            classify_session_intent("Can you reopen this chat now? Do it.").kind,
            IntentKind.NONE,
        )

    def test_conflicting_close_and_resume_language_is_not_terminal_intent(self):
        examples = (
            "Close this session and continue this session.",
            "Resume this session, but archive this session.",
            "Close this session. Resume this session.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_session_intent(text).kind, IntentKind.NONE)

    def test_deterministic_mutation_matrix(self):
        supported = {
            "Close this session.": IntentKind.CLOSE,
            "Archive this session.": IntentKind.CLOSE,
            "End this session.": IntentKind.CLOSE,
            "Finish this session.": IntentKind.CLOSE,
            "Wrap this session up.": IntentKind.CLOSE,
            "Close this out.": IntentKind.CLOSE,
            "Name and summarize this session and close it down.": IntentKind.CLOSE,
            "This session is finished.": IntentKind.CLOSE,
            "Resume this session.": IntentKind.RESUME,
            "Reopen this conversation.": IntentKind.RESUME,
            "Continue this thread.": IntentKind.RESUME,
        }
        negatives = []
        for action in ("Archive", "Close", "End", "Finish"):
            for target in (
                "the issue",
                "the task",
                "the ticket",
                "the file",
                "the document",
                "the notes",
                "the summary",
                "the analysis",
                "the report",
                "the plan",
            ):
                negatives.append(f"{action} {target} for this session.")
        negatives.extend(
            f"{prefix} close this session."
            for prefix in (
                "Do not",
                "Never",
                "Later,",
                "If the tests pass,",
                "When we are done,",
                "The example says",
                "The detector should recognize",
                "I almost said",
                "Should we",
                "Can we",
            )
        )
        variants = (
            lambda text: text,
            lambda text: text.upper(),
            lambda text: text.lower(),
            lambda text: text.rstrip(".") + "!",
        )
        checked = 0
        for text, expected in supported.items():
            for mutate in variants:
                candidate = mutate(text)
                with self.subTest(candidate=candidate):
                    self.assertIs(classify_session_intent(candidate).kind, expected)
                checked += 1
        for text in negatives:
            for mutate in variants:
                candidate = mutate(text)
                with self.subTest(candidate=candidate):
                    self.assertIs(classify_session_intent(candidate).kind, IntentKind.NONE)
                checked += 1
        self.assertGreaterEqual(checked, 200)


if __name__ == "__main__":
    unittest.main()
