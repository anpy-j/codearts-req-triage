"""分诊字段提取与无规则引擎回归测试。

验证：
1. 运行时不依赖 rules.yaml 或内置关键词规则；
2. 模块、严重程度和优先级优先读取并保留 CodeArts 原始字段，缺失时使用稳定的中性默认值；
3. 普通接口 curl 中的 token/Authorization 不会被误判为 auth；
4. 关键词提取仅用于代码定位。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import FakeClient
from codearts_triage.code_search import extract_keywords
from codearts_triage.config import Config
from codearts_triage.state import State
from codearts_triage.triage import (
    DEFAULT_MODULE,
    DEFAULT_PRIORITY,
    DEFAULT_SEVERITY,
    TriagePipeline,
    resolve_priority_suggestion,
)


def _make_issue(
    issue_id: int = 1,
    title: str = "接口报错",
    desc: str = "",
    module_name: str | None = None,
    module_id: int | None = 1,
    severity_name: str | None = "一般",
    severity_id: int | None = 2,
    priority_name: str | None = "中",
    priority_id: int | None = 2,
    assigned_name: str | None = "张三",
    assigned_id: int | None = 101,
) -> dict:
    return {
        "id": issue_id,
        "name": title,
        "description": desc,
        "severity": {"id": severity_id, "name": severity_name} if severity_name is not None else None,
        "priority": {"id": priority_id, "name": priority_name} if priority_name is not None else None,
        "module": {"id": module_id, "name": module_name} if module_name is not None else {"id": module_id, "name": None},
        "status": {"id": 1, "name": "新建"},
        "tracker": {"id": 3, "name": "Bug"},
        "created_time": "2026-08-18 07:00:00",
        "updated_time": "2026-08-18 08:00:00",
        "assigned_user": {"id": assigned_id, "name": assigned_name} if assigned_name is not None else None,
    }


class RulesRemovalAndFieldResolutionTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.tracker_ids = [3]

    def _pipeline(self, client: FakeClient, state_file: str) -> TriagePipeline:
        state = State(state_file)
        return TriagePipeline(client, self.config, state)

    def test_no_rules_yaml_dependency_at_runtime(self):
        """项目在不存在 rules.yaml 时正常初始化和运行，不加载任何关键词分类规则。"""
        with tempfile.TemporaryDirectory() as tmp:
            issue = _make_issue(issue_id=1, title="普通缺陷测试", desc="无规则运行")
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            # 确认无 rules_file 属性或依赖
            self.assertFalse(hasattr(self.config, "rules_file"))
            summary = pipeline.run_once(dry_run=True)
            self.assertEqual(summary["triaged"], 1)
            self.assertEqual(summary["errors"], 0)

    def test_preserves_codearts_module(self):
        """CodeArts 原始 module 存在时优先保留，不进行关键词推测。"""
        with tempfile.TemporaryDirectory() as tmp:
            issue = _make_issue(
                issue_id=2,
                title="登录页面样式异常",
                desc="页面白屏",
                module_name="user-center",
                module_id=88,
            )
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            self.assertEqual(result["module"], "user-center")
            self.assertEqual(result["module_name"], "user-center")
            self.assertEqual(result["module_id"], 88)

    def test_default_module_when_missing(self):
        """CodeArts module 缺失时回退到稳定的中性默认值 other。"""
        with tempfile.TemporaryDirectory() as tmp:
            issue = _make_issue(
                issue_id=3,
                title="完全没有模块信息",
                desc="正文描述",
                module_name=None,
                module_id=None,
            )
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            self.assertEqual(result["module"], DEFAULT_MODULE)
            self.assertEqual(result["module"], "other")
            self.assertIsNone(result["module_name"])
            self.assertIsNone(result["module_id"])

    def test_curl_with_token_not_misclassified_as_auth(self):
        """描述中包含 curl/token/Authorization 的普通接口不会因通用词被误判为 auth。"""
        with tempfile.TemporaryDirectory() as tmp:
            curl_desc = (
                "调用商品查询接口失败：\n"
                "curl -X GET 'https://api.example.com/v1/goods/list?category=1' \\\n"
                "-H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...' \\\n"
                "-H 'Content-Type: application/json' \\\n"
                "-H 'token: s3cr3t_t0k3n_abc123'\n"
                "返回 500 internal server error"
            )
            issue_without_module = _make_issue(
                issue_id=4,
                title="商品查询接口偶发 500",
                desc=curl_desc,
                module_name=None,
            )
            issue_with_goods = _make_issue(
                issue_id=5,
                title="商品查询接口偶发 500",
                desc=curl_desc,
                module_name="goods",
                module_id=50,
            )
            client = FakeClient(issues=[issue_without_module, issue_with_goods])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue_without_module)
            # 不应误判为 auth，应为中性默认值 other
            self.assertNotEqual(result["module"], "auth")
            self.assertEqual(result["module"], "other")

            # 若 CodeArts 设置了真实模块（如 goods），则保留原模块 goods，而非被 token 覆盖为 auth
            _, result_goods = pipeline.triage_issue(issue_with_goods)
            self.assertEqual(result_goods["module"], "goods")

    def test_preserves_codearts_severity_and_priority(self):
        """保留 CodeArts 原始严重程度与优先级，不按关键词升级。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 即使正文包含崩溃、数据丢失等高危词，也不私自把一般级别升级为 P0
            issue = _make_issue(
                issue_id=6,
                title="测试页面偶发崩溃数据丢失",
                desc="页面崩溃，可能数据丢失",
                severity_name="一般",
                severity_id=3,
                priority_name="中",
                priority_id=3,
            )
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            self.assertEqual(result["severity"], "一般")
            self.assertEqual(result["severity_id"], 3)
            self.assertEqual(result["priority_suggestion"], "P2")
            self.assertEqual(result["priority_id"], 3)

    def test_priority_and_severity_fallbacks(self):
        """优先级与严重程度缺失时的中性兜底。"""
        with tempfile.TemporaryDirectory() as tmp:
            issue = _make_issue(
                issue_id=7,
                title="字段全缺失",
                desc="",
                severity_name=None,
                severity_id=None,
                priority_name=None,
                priority_id=None,
            )
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            self.assertEqual(result["severity"], DEFAULT_SEVERITY)
            self.assertEqual(result["priority_suggestion"], DEFAULT_PRIORITY)
            self.assertIsNone(result["severity_id"])
            self.assertIsNone(result["priority_id"])

    def test_resolve_priority_suggestion_helper(self):
        """测试 priority 映射函数的标准口径。"""
        self.assertEqual(resolve_priority_suggestion("致命", None), "P0")
        self.assertEqual(resolve_priority_suggestion("加急", None), "P0")
        self.assertEqual(resolve_priority_suggestion("P0", None), "P0")
        self.assertEqual(resolve_priority_suggestion("严重", None), "P1")
        self.assertEqual(resolve_priority_suggestion("高", None), "P1")
        self.assertEqual(resolve_priority_suggestion("P1", None), "P1")
        self.assertEqual(resolve_priority_suggestion("一般", None), "P2")
        self.assertEqual(resolve_priority_suggestion("中", None), "P2")
        self.assertEqual(resolve_priority_suggestion("P2", None), "P2")
        self.assertEqual(resolve_priority_suggestion("轻微", None), "P3")
        self.assertEqual(resolve_priority_suggestion("低", None), "P3")
        self.assertEqual(resolve_priority_suggestion("P3", None), "P3")
        # priority 缺失时回退到 severity
        self.assertEqual(resolve_priority_suggestion(None, "致命"), "P0")
        self.assertEqual(resolve_priority_suggestion(None, "严重"), "P1")
        self.assertEqual(resolve_priority_suggestion(None, "一般"), "P2")
        self.assertEqual(resolve_priority_suggestion(None, "轻微"), "P3")
        # 两者均缺失或无法识别时回退到中性值 P2
        self.assertEqual(resolve_priority_suggestion(None, None), "P2")
        self.assertEqual(resolve_priority_suggestion("unknown", "unknown"), "P2")

    def test_preserves_codearts_assignee(self):
        """负责人直接读取 assigned_user，不再按模块关键词推测。"""
        with tempfile.TemporaryDirectory() as tmp:
            issue = _make_issue(
                issue_id=8,
                title="支付接口故障",
                desc="支付失败",
                assigned_name="李四",
                assigned_id=10002,
            )
            client = FakeClient(issues=[issue])
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            self.assertEqual(result["assignee_suggestion"], "李四")
            self.assertEqual(result["assignee_id"], 10002)

            # 未分配时为 None
            unassigned_issue = _make_issue(issue_id=9, assigned_name=None, assigned_id=None)
            _, unassigned_result = pipeline.triage_issue(unassigned_issue)
            self.assertIsNone(unassigned_result["assignee_suggestion"])
            self.assertIsNone(unassigned_result["assignee_id"])

    def test_extract_keywords_for_code_search(self):
        """代码搜索关键词提取正常工作，且不参与模块推断。"""
        kws = extract_keywords("登录接口 loginApi 超时 token 校验失败 userService.queryById", max_keywords=5)
        self.assertIn("loginapi", kws)
        self.assertIn("token", kws)
        self.assertIn("userservice", kws)
        self.assertIn("querybyid", kws)
        self.assertLessEqual(len(kws), 5)

    def test_fixture_issue_is_triaged_with_native_fields(self):
        """加载 tests/fixtures/issues.json 样例，验证原生字段保留。"""
        fixture = Path(__file__).resolve().parent / "fixtures" / "issues.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        issue = data["issues"][0]
        detail = data["details"][str(issue["id"])]
        issue["description"] = detail["description"]

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(issues=[issue], details={issue["id"]: detail})
            pipeline = self._pipeline(client, f"{tmp}/state.json")
            _, result = pipeline.triage_issue(issue)
            # fixtures 中的 issue 原生 module.name 即为 auth
            self.assertEqual(result["module"], "auth")
            # 原始 severity 为 "严重"，priority 为 "中"
            self.assertEqual(result["severity"], "严重")
            self.assertEqual(result["priority_suggestion"], "P2")
            self.assertEqual(result["assignee_suggestion"], "张三")


if __name__ == "__main__":
    unittest.main()
