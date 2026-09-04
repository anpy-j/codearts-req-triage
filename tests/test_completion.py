"""CodeArts 评论 + 状态闭环测试。"""

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import FakeClient
from codearts_triage.completion import AI_COMMENT_MARKER, complete_issue, prepare_ai_comment
from codearts_triage.state import State


class CompletionTest(unittest.TestCase):
    def test_sanitize_and_mark_ai_comment(self):
        text = prepare_ai_comment(
            "Authorization: Bearer top-secret\nHW_SK_READ=secret-value\nCookie: token=abc\nSQL: SELECT 1;"
        )

        self.assertTrue(text.startswith(AI_COMMENT_MARKER))
        self.assertNotIn("top-secret", text)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("token=abc", text)
        self.assertIn("SELECT 1", text)

    def test_comment_precedes_resolve_and_duplicate_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(str(Path(tmp) / "state.json"))
            client = FakeClient(
                issues=[{"id": 7, "updated_time": "2026-09-04 10:00:00"}],
                details={7: {"comments": []}},
            )

            first = complete_issue(client, state, 7, "修复完成\n```sql\nSELECT 1;\n```")
            second = complete_issue(client, state, 7, "修复完成\n```sql\nSELECT 1;\n```")

            self.assertTrue(first["comment_written"])
            self.assertTrue(second["comment_skipped"])
            self.assertEqual(len(client.comment_writes), 1)
            self.assertLess(client.calls.index("add_comment:7"), client.calls.index("update_status:7:3"))
            self.assertEqual(state.get_source_status_id(7), 3)
            self.assertIsNotNone(state.get_source_comment_id(7))

    def test_comment_failure_does_not_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(str(Path(tmp) / "state.json"))
            client = FakeClient(issues=[{"id": 8, "updated_time": "2026-09-04 10:00:00"}])
            client.add_comment = lambda issue_id, notes: (_ for _ in ()).throw(RuntimeError("comment down"))

            with self.assertRaisesRegex(RuntimeError, "comment down"):
                complete_issue(client, state, 8, "修复完成")

            self.assertEqual(client.status_updates, [])


if __name__ == "__main__":
    unittest.main()
