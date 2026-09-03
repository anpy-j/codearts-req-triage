"""规则引擎测试：模块分类、优先级、负责人、关键词提取。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.rules import DEFAULT_RULES, Rules


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = Rules(DEFAULT_RULES)

    def test_classify_module(self):
        self.assertEqual(self.rules.classify_module("用户登录时 token 过期"), "auth")
        self.assertEqual(self.rules.classify_module("支付失败，金额不对"), "payment")
        self.assertEqual(self.rules.classify_module("页面白屏，样式错乱"), "ui")
        self.assertEqual(self.rules.classify_module("完全无关的内容 xyz"), "other")

    def test_classify_case_insensitive(self):
        self.assertEqual(self.rules.classify_module("API 500 超时"), "api")
        self.assertEqual(self.rules.classify_module("Login token invalid"), "auth")

    def test_suggest_priority_from_severity(self):
        self.assertEqual(self.rules.suggest_priority("致命", "登录接口报错"), "P0")
        self.assertEqual(self.rules.suggest_priority("严重", "订单列表查不出来"), "P1")
        self.assertEqual(self.rules.suggest_priority("轻微", "文案错别字"), "P3")

    def test_suggest_priority_keyword_upgrade(self):
        # 一般严重级 + 崩溃关键词 → 升级 P0
        self.assertEqual(self.rules.suggest_priority("一般", "页面崩溃，数据丢失"), "P0")
        # 未知严重级 + 核心功能不可用 → P1
        self.assertEqual(self.rules.suggest_priority(None, "核心功能不可用"), "P1")

    def test_suggest_assignee(self):
        rules = Rules({"assignee_map": {"auth": "张三"}, "module_keywords": {"auth": ["登录"]}, "default_module": "other"})
        self.assertEqual(rules.suggest_assignee("auth"), "张三")
        self.assertIsNone(rules.suggest_assignee("other"))

    def test_extract_keywords(self):
        kws = self.rules.extract_keywords("登录接口 loginApi 超时 token 校验失败 userService", max_keywords=5)
        self.assertIn("loginapi", kws)
        self.assertIn("token", kws)
        self.assertIn("userservice", kws)
        self.assertLessEqual(len(kws), 5)

    def test_load_missing_file_falls_back(self):
        rules = Rules.load("/nonexistent/rules.yaml")
        self.assertEqual(rules.default_module, "other")

    def test_load_json_file(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"module_keywords": {"db": ["慢查询"]}, "default_module": "misc"}, fh)
            path = fh.name
        rules = Rules.load(path)
        self.assertEqual(rules.classify_module("SQL 慢查询"), "db")
        self.assertEqual(rules.classify_module("其他"), "misc")

    def test_fixture_issue_is_classified_auth(self):
        """加载 tests/fixtures/issues.json 样例，验证端到端样例可分诊。"""
        import json

        fixture = Path(__file__).resolve().parent / "fixtures" / "issues.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        issue = data["issues"][0]
        detail = data["details"][str(issue["id"])]
        text = f"{issue['name']}\n{detail['description']}"
        self.assertEqual(self.rules.classify_module(text), "auth")
        self.assertEqual(self.rules.suggest_priority(issue["severity"]["name"], text), "P1")


if __name__ == "__main__":
    unittest.main()
