"""状态文件测试：游标、去重、幂等、错误队列、持久化。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.state import State


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "state.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_fresh_state(self):
        s = State(self.path)
        self.assertIsNone(s.get_cursor())
        self.assertFalse(s.is_processed(1))
        self.assertEqual(s.error_count(), 0)

    def test_cursor_roundtrip_and_persist(self):
        s = State(self.path)
        s.set_cursor("2026-08-18 07:00:00")
        s.mark_processed(42, "2026-08-18 07:00:00", "hash123")
        s.record_error(7, "boom")
        s.save()

        s2 = State(self.path)
        self.assertEqual(s2.get_cursor(), "2026-08-18 07:00:00")
        self.assertTrue(s2.is_processed(42))
        self.assertEqual(s2.get_triage_hash(42), "hash123")
        self.assertEqual(s2.error_count(), 1)

    def test_needs_retriage(self):
        s = State(self.path)
        s.mark_processed(1, "t1", "h")
        self.assertFalse(s.needs_retriage(1, "t1"))
        self.assertTrue(s.needs_retriage(1, "t2"))  # updated_time 变化 → 重新分诊
        self.assertTrue(s.needs_retriage(99, "t1"))  # 未处理

    def test_multica_mapping_and_source_snapshot_survive_updates(self):
        s = State(self.path)
        s.mark_processed(
            42,
            "t1",
            "hash1",
            multica_issue_id="multica-issue-42",
            source_status_id=3,
            source_comment_id="comment-1",
        )
        s.mark_processed(42, "t2", "hash2")
        self.assertEqual(s.get_multica_issue_id(42), "multica-issue-42")
        self.assertEqual(s.get_source_status_id(42), 3)
        self.assertEqual(s.get_source_comment_id(42), "comment-1")

        s.update_source_snapshot(42, updated_time="t3", source_status_id=1, source_comment_id="comment-2")
        self.assertEqual(s.get_processed_updated_time(42), "t3")
        self.assertEqual(s.get_source_status_id(42), 1)
        self.assertEqual(s.get_source_comment_id(42), "comment-2")

    def test_clear_error(self):
        s = State(self.path)
        s.record_error(5, "err")
        self.assertEqual(s.error_count(), 1)
        s.clear_error(5)
        self.assertEqual(s.error_count(), 0)

    def test_record_error_poison_after_max_attempts(self):
        s = State(self.path)
        # 前 4 次：仍在 errors 队列
        for i in range(4):
            poisoned = s.record_error(1, f"err{i}", max_attempts=5)
            self.assertFalse(poisoned)
            self.assertTrue(s.has_error(1))
            self.assertFalse(s.is_poisoned(1))
        # 第 5 次：转 poisoned，从 errors 移除
        poisoned = s.record_error(1, "err4", max_attempts=5)
        self.assertTrue(poisoned)
        self.assertFalse(s.has_error(1))
        self.assertTrue(s.is_poisoned(1))
        self.assertEqual(s.error_count(), 0)  # poisoned 不计入待重试

    def test_poisoned_persisted(self):
        s = State(self.path)
        s.record_error(9, "boom", max_attempts=1)  # 直接转 poisoned
        s.save()
        s2 = State(self.path)
        self.assertTrue(s2.is_poisoned(9))
        self.assertEqual(s2.error_count(), 0)

    def test_poison_stale_errors(self):
        s = State(self.path)
        s.record_error(1, "err")  # round=0
        # 连续 max_rounds 轮未再出现 → 转 poisoned
        self.assertEqual(s.poison_stale_errors(max_rounds=3), 0)  # round=1: 1-0=1 < 3
        self.assertEqual(s.poison_stale_errors(max_rounds=3), 0)  # round=2: 2-0=2 < 3
        self.assertEqual(s.poison_stale_errors(max_rounds=3), 1)  # round=3: 3-0=3 >= 3
        self.assertTrue(s.is_poisoned(1))
        self.assertEqual(s.error_count(), 0)
        # 重新出现的错误轮次刷新，不会立即被判 stale
        s.record_error(2, "err2")
        s.poison_stale_errors(max_rounds=3)
        self.assertTrue(s.has_error(2))


if __name__ == "__main__":
    unittest.main()
