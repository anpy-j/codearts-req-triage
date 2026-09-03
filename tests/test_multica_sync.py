"""Multica 联动模块单元测试（脱敏测试用例）。"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.multica_sync import MulticaSync


class MulticaSyncTest(unittest.TestCase):
    def test_disabled_by_default(self):
        sync = MulticaSync(enabled=False)
        self.assertFalse(sync.should_sync({"priority_suggestion": "P1"}))
        self.assertIsNone(sync.sync_issue(1, "title", "desc", {}))

    def test_priority_filtering(self):
        sync = MulticaSync(enabled=True, min_priority="P1")
        self.assertTrue(sync.should_sync({"priority_suggestion": "P0"}))
        self.assertTrue(sync.should_sync({"priority_suggestion": "P1"}))
        self.assertFalse(sync.should_sync({"priority_suggestion": "P2"}))
        self.assertFalse(sync.should_sync({"priority_suggestion": "P3"}))

    def test_handler_whitelist_filtering(self):
        sync = MulticaSync(enabled=True, min_priority="P4", sync_handlers=["dev_user1"])
        # 匹配租户前缀账号
        self.assertTrue(sync.should_sync(
            {"priority_suggestion": "P2"},
            assigned_user={"name": "tenant_prefix_dev_user1", "id": 10001}
        ))
        # 其他处理人被过滤
        self.assertFalse(sync.should_sync(
            {"priority_suggestion": "P2"},
            assigned_user={"name": "dev_user2", "id": 20002}
        ))
        # 未分配被过滤
        self.assertFalse(sync.should_sync(
            {"priority_suggestion": "P2"},
            assigned_user=None
        ))

    def test_handler_mapping(self):
        sync = MulticaSync(
            enabled=True,
            assignee_id="default-uuid",
            handler_mapping={"dev_user1": "uuid-dev1", "dev_user2": "uuid-dev2"},
        )
        self.assertEqual(sync.resolve_assignee({"name": "tenant_prefix_dev_user1"}), "uuid-dev1")
        self.assertEqual(sync.resolve_assignee({"name": "dev_user2"}), "uuid-dev2")
        self.assertEqual(sync.resolve_assignee({"name": "other_person"}), "default-uuid")

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_sync_issue_calls_multica_cli(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/multica"
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps({"id": "01a0-test-id", "identifier": "MUL-100"})
        mock_run.return_value = mock_proc

        sync = MulticaSync(enabled=True, assignee_id="00000000-0000-0000-0000-000000000001", min_priority="P4")
        issue_id = sync.sync_issue(
            issue_id=71081752,
            title="测试Multica联通",
            description="<p>500错误</p>",
            result={"module": "auth", "priority_suggestion": "P2", "severity": "一般"},
            project_id="test-proj",
            region="cn-north-4",
        )
        self.assertEqual(issue_id, "01a0-test-id")
        self.assertTrue(mock_run.called)
        cmd = mock_run.call_args[0][0]
        self.assertIn("multica", cmd[0])
        self.assertIn("create", cmd)
        self.assertIn("--assignee-id", cmd)
        self.assertIn("00000000-0000-0000-0000-000000000001", cmd)

        # 测试人名模式使用 --assignee
        sync_name = MulticaSync(enabled=True, assignee_id="dev_user1", min_priority="P4")
        sync_name.sync_issue(
            issue_id=71081752,
            title="测试Multica联通",
            description="<p>500错误</p>",
            result={"module": "auth", "priority_suggestion": "P2", "severity": "一般"},
        )
        cmd_name = mock_run.call_args[0][0]
        self.assertIn("--assignee", cmd_name)
        self.assertIn("dev_user1", cmd_name)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_sync_issue_with_project(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/multica"
        proc_list = MagicMock()
        proc_list.stdout = json.dumps([{"id": "proj-uuid-1234-5678-9012-345678901234", "title": "trade-system-backend"}])
        proc_create = MagicMock()
        proc_create.stdout = json.dumps({"id": "new-issue-id"})
        mock_run.side_effect = [proc_list, proc_create]

        sync = MulticaSync(enabled=True, assignee_id="dev_user1", min_priority="P4")
        sync.sync_issue(
            issue_id=101,
            title="Bug in trade service",
            description="err",
            result={"module": "auth", "priority_suggestion": "P2"},
            multica_project="trade-system-backend",
        )
        cmd = mock_run.call_args[0][0]
        self.assertIn("--project", cmd)
        self.assertIn("proj-uuid-1234-5678-9012-345678901234", cmd)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_project_mapping_routing(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/multica"
        def fake_run(cmd, *args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "agent" in cmd:
                m.stdout = "[]"
            elif "project" in cmd:
                m.stdout = json.dumps([
                    {"id": "uuid-trade-proj-1234-5678-9012-345678901234", "title": "trade-system-backend"},
                    {"id": "uuid-app-proj-1234-5678-9012-345678901234", "title": "buyer-app-service"},
                ])
            else:
                m.stdout = json.dumps({"id": "created-issue-id"})
            return m

        mock_run.side_effect = fake_run

        mapping_str = "trade:trade-system-backend:trade-dev-agent,app:buyer-app-service:app-dev-agent"
        sync = MulticaSync(
            enabled=True,
            assignee_id="default-user",
            min_priority="P4",
            project_mapping=mapping_str,
            auto_assign_agent=True,
        )

        # 1. 命中 trade 模块
        sync.sync_issue(
            issue_id=201,
            title="数据报表加载慢",
            description="500 error",
            result={"module": "other", "priority_suggestion": "P2"},
            raw_module={"name": "trade-service"},
        )
        cmd_trade = mock_run.call_args[0][0]
        self.assertIn("--project", cmd_trade)
        self.assertIn("uuid-trade-proj-1234-5678-9012-345678901234", cmd_trade)
        self.assertIn("--assignee", cmd_trade)
        self.assertIn("trade-dev-agent", cmd_trade)

        # 2. 标题命中 app
        sync.sync_issue(
            issue_id=202,
            title="【app】客户端打卡失败",
            description="fail",
            result={"module": "other", "priority_suggestion": "P2"},
        )
        cmd_app = mock_run.call_args[0][0]
        self.assertIn("--project", cmd_app)
        self.assertIn("uuid-app-proj-1234-5678-9012-345678901234", cmd_app)
        self.assertIn("--assignee", cmd_app)
        self.assertIn("app-dev-agent", cmd_app)

        # 3. 默认收件箱模式（auto_assign_agent=False）：指派给 default-user
        sync_inbox = MulticaSync(
            enabled=True,
            assignee_id="default-user",
            min_priority="P4",
            project_mapping=mapping_str,
            auto_assign_agent=False,
        )
        sync_inbox.sync_issue(
            issue_id=203,
            title="【app】测试收件箱模式",
            description="inbox test",
            result={"module": "other", "priority_suggestion": "P2"},
        )
        cmd_inbox = mock_run.call_args[0][0]
        self.assertIn("--assignee", cmd_inbox)
        self.assertIn("default-user", cmd_inbox)

    def test_detect_platform_3_tiers(self):
        test_mapping = {
            "业务系统A": {
                "project": "proj-a",
                "assignee": "agent-a",
                "keywords": ["system_a", "biz_a"],
            },
            "客户端App": {
                "project": "proj-app",
                "assignee": "agent-app",
                "keywords": ["app_api", "mobile"],
            },
            "管理后台": {
                "project": "proj-admin",
                "assignee": "agent-admin",
                "keywords": ["admin_api", "manage"],
            },
        }
        sync = MulticaSync(enabled=True, project_mapping=test_mapping)

        # 1. 第一优先级：平台自定义字段
        issue_with_field = {
            "new_custom_fields": [
                {"field_name": "平台", "value": "业务系统A"}
            ]
        }
        res1 = sync.detect_platform(
            raw_issue=issue_with_field,
            title="【客户端App】测试标题",
            description="curl http://example.com/admin_api",
        )
        self.assertEqual(res1, "业务系统A")

        # 2. 第二优先级：标题
        issue_no_field = {"new_custom_fields": [{"field_name": "平台", "value": None}]}
        res2 = sync.detect_platform(
            raw_issue=issue_no_field,
            title="【客户端App】用户打卡白屏",
            description="curl http://example.com/admin_api",
        )
        self.assertEqual(res2, "客户端App")

        res2_backend = sync.detect_platform(
            raw_issue=issue_no_field,
            title="【管理后台】角色权限配置失败",
        )
        self.assertEqual(res2_backend, "管理后台")

        # 3. 第三优先级：正文 curl / 关键词
        res3_curl_a = sync.detect_platform(
            raw_issue=issue_no_field,
            title="接口偶发 500 异常",
            description="复现方式：curl -X POST https://api.domain.com/system_a/report",
        )
        self.assertEqual(res3_curl_a, "业务系统A")

        res3_curl_admin = sync.detect_platform(
            raw_issue=issue_no_field,
            title="接口偶发 500 异常",
            description="复现方式：curl -X GET http://10.0.0.1:8080/admin_api/list",
        )
        self.assertEqual(res3_curl_admin, "管理后台")

        # 验证解析目标映射
        p1, a1, d1 = sync.resolve_target(raw_issue=issue_with_field)
        self.assertEqual(d1, "业务系统A")
        self.assertEqual(p1, "proj-a")
        self.assertEqual(a1, "agent-a")

        p2, a2, d2 = sync.resolve_target(raw_issue=issue_no_field, title="【客户端App】打卡异常")
        self.assertEqual(d2, "客户端App")
        self.assertEqual(p2, "proj-app")
        self.assertEqual(a2, "agent-app")

        p3, a3, d3 = sync.resolve_target(raw_issue=issue_no_field, title="【管理后台】角色配置")
        self.assertEqual(d3, "管理后台")
        self.assertEqual(p3, "proj-admin")
        self.assertEqual(a3, "agent-admin")

    @patch("os.remove")
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_delivery_guidelines_and_test_branch(self, mock_which, mock_run, mock_remove):
        mock_which.return_value = "/usr/local/bin/multica"
        mock_run.return_value.return_value = 0
        mock_run.return_value.stdout = json.dumps({"id": "issue-delivery-test"})

        sync = MulticaSync(enabled=True)
        sync.sync_issue(
            issue_id=888999,
            title="测试动态目标测试分支交付指南",
            description="出现 NPE 崩溃",
            result={"module": "auth", "priority_suggestion": "P1"},
            project_id="a1b2c3d4e5f678901234567890abcdef",
            region="cn-north-4",
            test_branch="test-cloud",
        )

        cmd = mock_run.call_args[0][0]
        desc_file_idx = cmd.index("--description-file") + 1
        desc_file_path = cmd[desc_file_idx]

        with open(desc_file_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("- **目标测试分支**：`test-cloud`", content)
        self.assertIn("git_flow.py -b test-cloud", content)
        self.assertIn("--hw-project a1b2c3d4e5f678901234567890abcdef --resolve 888999", content)
        self.assertIn("in_review", content)


if __name__ == "__main__":
    unittest.main()
