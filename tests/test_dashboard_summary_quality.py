import pathlib
import tempfile
import unittest

import session_search as ss


def seed(conn, *, source, session_id, cwd, users, assistants=(), title="", ts=1000):
    docs = []
    t = ts
    for text in users:
        docs.append(ss.Document(
            doc_id=f"{source}:{session_id}:u:{t}", source=source, session_id=session_id,
            title=title or text[:80], path=f"/tmp/{source}/{session_id}.jsonl", cwd=cwd,
            role="user", ts=t, text=text, meta={},
        )); t += 1
    for text in assistants:
        docs.append(ss.Document(
            doc_id=f"{source}:{session_id}:a:{t}", source=source, session_id=session_id,
            title=title or "a", path=f"/tmp/{source}/{session_id}.jsonl", cwd=cwd,
            role="assistant", ts=t, text=text, meta={},
        )); t += 1
    ss.upsert_documents(conn, docs)
    return conn.execute(
        "SELECT * FROM documents WHERE session_id=? ORDER BY ts DESC LIMIT 1", (session_id,)
    ).fetchone()


class SummaryQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ss.connect_db(pathlib.Path(self.tmp.name) / "t.sqlite")
        ss.init_db(self.conn)

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_shell_paste_not_about(self):
        row = seed(self.conn, source="claude", session_id="s1", cwd="/tmp/os",
            users=["alex@host ~ % ss qvac", "Please fix Hermes gateway and bookmark scraper watchdog alerts."],
            assistants=["Implemented gateway recovery and restored the scraper schedule.",
                        "Next need to verify the watchdog no longer red-alerts."], title="Fix Hermes gateway")
        card = ss.session_card_for_result(self.conn, row, "")
        about, state, resume, clue = ss.dashboard_summary(card, ss.session_rows(self.conn, row), row)
        self.assertNotIn("ss qvac", about.lower())
        self.assertNotIn("alex@", about.lower())
        self.assertTrue("hermes" in about.lower() or "gateway" in about.lower())
        self.assertFalse(state.startswith("|"))
        self.assertNotIn("what happened:", state.lower())
        self.assertNotEqual(resume.lower().strip("."), "stop")

    def test_stop_not_about(self):
        row = seed(self.conn, source="codex", session_id="s2", cwd="/tmp/career",
            users=["Stop.", "I need to find the sessions where I was applying to jobs."],
            assistants=["Located two application sessions in the career folder."])
        card = ss.session_card_for_result(self.conn, row, "")
        about, state, resume, clue = ss.dashboard_summary(card, ss.session_rows(self.conn, row), row)
        self.assertNotEqual(about.lower().rstrip("."), "stop")
        self.assertTrue("job" in about.lower() or "career" in about.lower() or "application" in about.lower())

    def test_path_dedupe(self):
        self.assertEqual(ss._dedupe_card_path("/tmp/a.txt/tmp/a.txt"), "/tmp/a.txt")

    def test_typos(self):
        self.assertIn("application", ss.normalize_card_typos("job applciation"))
        self.assertIn("state", ss.normalize_card_typos("stqate"))


if __name__ == "__main__":
    unittest.main()
