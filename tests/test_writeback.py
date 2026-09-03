"""写回模块测试：自定义字段、描述追加、标记剥离、自动改字段开关。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import FakeClient
from codearts_triage.writeback import (
    TRIAGE_MARKER_END,
    TRIAGE_MARKER_START,
    WriteBack,
    build_description_block,
    build_triage_json,
    strip_old_triage_block,
    triage_hash,
)

RESULT = {
    "issue_id": 1,
    "module": "auth",
    "priority_suggestion": "P1",
    "severity": "严重",
    "assignee_suggestion": "张三",
    "keywords": ["token", "login"],
    "code_hints": [{"kind": "file", "file": "src/auth.py", "line": "42"}],
    "summary": "登录 token 校验失败",
    "triaged_at": "2026-08-18T08:00:00Z",
    "rule_version": "1.0",
}


class WriteBackTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.wb = WriteBack(
            self.client,
            field_name="AI分诊",
            description_append=True,
            auto={"severity": False, "priority": False, "module": False, "assignee": False},
        )

    def test_triage_hash_stable(self):
        self.assertEqual(triage_hash(RESULT), triage_hash(RESULT))
        changed = {**RESULT, "module": "payment"}
        self.assertNotEqual(triage_hash(RESULT), triage_hash(changed))

    def test_build_triage_json(self):
        payload = build_triage_json(RESULT)
        import json

        parsed = json.loads(payload)
        self.assertEqual(parsed["module"], "auth")

    def test_description_block_has_markers(self):
        block = build_description_block(RESULT)
        self.assertIn(TRIAGE_MARKER_START, block)
        self.assertIn(TRIAGE_MARKER_END, block)
        self.assertIn("模块：auth", block)

    def test_strip_old_triage_block(self):
        old = "原始描述内容\n" + build_description_block(RESULT)
        stripped = strip_old_triage_block(old)
        self.assertNotIn(TRIAGE_MARKER_START, stripped)
        self.assertIn("原始描述内容", stripped)
        # 无标记时不改动
        self.assertEqual(strip_old_triage_block("普通描述"), "普通描述")

    def test_apply_writes_custom_field_and_description(self):
        actions = self.wb.apply({"id": 1, "description": "原始"}, RESULT, dry_run=False)
        self.assertEqual(len(self.client.custom_field_writes), 1)
        self.assertEqual(self.client.custom_field_writes[0][1], "AI分诊")
        self.assertEqual(len(self.client.description_writes), 1)
        self.assertIn("原始", self.client.description_writes[0][1])
        self.assertIn(TRIAGE_MARKER_START, self.client.description_writes[0][1])
        self.assertGreaterEqual(len(actions), 2)

    def test_apply_dry_run_no_writes(self):
        actions = self.wb.apply({"id": 1, "description": "原始"}, RESULT, dry_run=True)
        self.assertEqual(len(self.client.custom_field_writes), 0)
        self.assertEqual(len(self.client.description_writes), 0)
        self.assertTrue(all(a.startswith("[dry-run]") for a in actions))

    def test_auto_field_updates_default_off(self):
        self.wb.apply({"id": 1, "description": ""}, RESULT, dry_run=False)
        self.assertEqual(self.client.field_updates, [])  # auto 全 false → 不调用

    def test_auto_field_updates_when_enabled(self):
        wb = WriteBack(
            self.client,
            field_name="AI分诊",
            description_append=False,
            auto={"severity": True, "priority": True, "module": False, "assignee": False},
        )
        result = {**RESULT, "severity_id": 1, "priority_id": 2}
        wb.apply({"id": 1, "description": ""}, result, dry_run=False)
        self.assertEqual(len(self.client.field_updates), 1)
        self.assertEqual(self.client.field_updates[0]["severity_id"], 1)
        self.assertEqual(self.client.field_updates[0]["priority_id"], 2)
        self.assertIsNone(self.client.field_updates[0]["module_id"])

    def test_apply_with_custom_field_slot(self):
        issue = {
            "id": 1,
            "description": "原始",
            "new_custom_fields": [
                {"custom_field": "custom_field16", "field_name": "AI分诊", "value": ""}
            ],
        }
        actions = self.wb.apply(issue, RESULT, dry_run=False)
        self.assertEqual(len(self.client.custom_field_writes), 1)
        self.assertEqual(self.client.custom_field_writes[0][1], "AI分诊")
    def test_sanitize_req_text(self):
        from codearts_triage.writeback import sanitize_req_text

        text_with_emoji = "## 🤖 AI 分诊摘要 👍🎉"
        self.assertEqual(sanitize_req_text(text_with_emoji), "##  AI 分诊摘要 ")
        self.assertEqual(sanitize_req_text("正常文本"), "正常文本")
        self.assertEqual(sanitize_req_text(""), "")

    def test_strip_old_triage_block_without_html_comments(self):
        old = "原始描述内容\n\n## AI 分诊摘要\n- 模块：auth\n- 优先级建议：P2\n- 关键词：multica\n- 说明：自动化分诊生成，仅供参考（规则 vbuiltin-1.0）"
        stripped = strip_old_triage_block(old)
        self.assertEqual(stripped, "原始描述内容")


if __name__ == "__main__":
    unittest.main()

