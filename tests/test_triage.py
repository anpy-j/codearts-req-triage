"""分诊编排端到端测试：FakeClient 全链路（拉取→分诊→写回→幂等）。"""

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import FakeClient
from codearts_triage.config import Config
from codearts_triage.rules import Rules
from codearts_triage.state import State
from codearts_triage.triage import TriagePipeline
from codearts_triage.writeback import triage_hash


def _issue(issue_id, title, severity="一般", updated="2026-08-18 08:00:00", desc="", tracker_id=3):
    return {
        "id": issue_id,
        "name": title,
        "severity": {"id": 2, "name": severity},
        "priority": {"id": 2, "name": "中"},
        "module": {"id": 1, "name": None},
        "status": {"id": 1, "name": "新建"},
        "tracker": {"id": tracker_id, "name": "Bug" if tracker_id == 3 else "Task"},
        "created_time": "2026-08-18 07:00:00",
        "updated_time": updated,
        "assigned_user": {"id": 1, "name": "张三"},
        "description": desc,
    }


def _recent_hw_time(days_ago=1, hour=8):
    """近期（默认昨天指定小时）本地时间字符串：评论水位探测窗口默认只盯 7 天内处理过的项。"""
    base = datetime.now() - timedelta(days=days_ago)
    base = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return base.strftime("%Y-%m-%d %H:%M:%S")


class TriagePipelineTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.tracker_ids = [3]
        self.config.writeback_enabled = True
        self.config.description_append = True
        self.config.triage_field_name = "AI分诊"

    def _pipeline(self, client, state_path):
        state = State(state_path)
        rules = Rules.load(None)  # 内置默认规则
        return TriagePipeline(client, self.config, rules, state), state

    def test_time_interval_uses_unix_milliseconds(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, state = self._pipeline(FakeClient(), f"{tmp}/state.json")
            state.set_cursor("2026-08-18 08:00:00")
            fixed_now = datetime(2026, 8, 18, 9, 0, 0, tzinfo=timezone.utc)

            with patch("codearts_triage.triage._now_utc", return_value=fixed_now):
                interval = pipeline._build_time_interval()

            start, end = interval.split(",")
            expected_start = int(datetime(2026, 8, 18, 7, 59, 0, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
            expected_end = int(fixed_now.timestamp() * 1000)
            self.assertEqual(int(start), expected_start)
            self.assertEqual(int(end), expected_end)

    def test_run_once_full_flow(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录接口超时 token 校验失败", desc="登录时 500")
            client = FakeClient(issues=[issue], details={1: {"description": "登录时 500"}})
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            summary = pipeline.run_once()

            self.assertEqual(summary["fetched"], 1)
            self.assertEqual(summary["triaged"], 1)
            self.assertEqual(summary["written"], 1)
            self.assertEqual(summary["errors"], 0)

            # 写回动作：自定义字段 + 描述追加
            self.assertEqual(len(client.custom_field_writes), 1)
            self.assertEqual(client.custom_field_writes[0][1], "AI分诊")
            self.assertEqual(len(client.description_writes), 1)
            self.assertIn("AI-TRIAGE", client.description_writes[0][1])

            # 分诊结果模块应为 auth；严重级「一般」无升级关键词 → P2
            detail = summary["details"][0]["result"]
            self.assertEqual(detail["module"], "auth")
            self.assertEqual(detail["priority_suggestion"], "P2")

            # 游标推进 + 幂等：再跑一轮不重复写
            summary2 = pipeline.run_once()
            self.assertEqual(summary2["skipped"], 1)
            self.assertEqual(len(client.custom_field_writes), 1)  # 未重复写

    def test_run_once_skips_non_bug_tracker(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(issues=[_issue(2, "任务项", tracker_id=2)])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            summary = pipeline.run_once()
            self.assertEqual(summary["fetched"], 0)  # tracker 过滤生效（FakeClient 内过滤）

    def test_retriage_on_update(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()
            self.assertEqual(len(client.custom_field_writes), 1)

            # 模拟工作项被更新（updated_time 变化）
            client.issues[0]["updated_time"] = "2026-08-18 09:00:00"
            client.details[1] = {"description": "现在变成支付模块问题了"}
            client.issues[0]["name"] = "支付失败金额不对"
            summary = pipeline.run_once()
            self.assertEqual(summary["triaged"], 1)  # 重新分诊
            self.assertEqual(len(client.custom_field_writes), 2)  # 再次写回

    def test_error_isolation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "好问题", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])

            def boom(issue_id):
                raise RuntimeError("api down")

            client.show_issue = boom
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_backoff = (0, 0, 0)  # 测试不等待
            summary = pipeline.run_once()
            self.assertEqual(summary["errors"], 1)
            self.assertEqual(state.error_count(), 1)
            self.assertEqual(summary["written"], 0)

    def test_retry_transient_failure(self):
        """单条瞬时失败：指数退避重试后成功，不记错误。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            calls = {"n": 0}

            def flaky(issue_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient network error")
                return {"id": issue_id, "description": "登录超时"}

            client.show_issue = flaky
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_backoff = (0, 0, 0)  # 测试不等待
            summary = pipeline.run_once()
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["written"], 1)
            self.assertEqual(calls["n"], 2)  # 1 次失败 + 1 次成功
            self.assertEqual(state.error_count(), 0)

    def test_dry_run_no_client_writes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            self.config.writeback_enabled = False
            summary = pipeline.run_once(dry_run=True)
            self.assertEqual(summary["triaged"], 1)
            self.assertEqual(len(client.custom_field_writes), 0)
            self.assertEqual(len(client.description_writes), 0)
            self.assertIn("dry-run", summary["details"][0]["actions"][0])

    def test_writeback_preserves_original_description(self):
        """回归：列表条目不含 description，写回描述时必须保留详情里的原始描述。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            original_desc = "这是原始缺陷描述，很重要。\n第二行。"
            issue = _issue(1, "登录超时", desc=original_desc)
            client = FakeClient(issues=[issue], details={1: {"description": original_desc}})
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            written_desc = client.description_writes[0][1]
            self.assertIn(original_desc, written_desc)  # 原始描述必须保留
            self.assertIn("AI-TRIAGE", written_desc)

    def test_idempotent_when_result_unchanged(self):
        """回归：triaged_at 变化不应破坏幂等，结果未变时第二轮跳过写回。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            summary1 = pipeline.run_once()
            self.assertEqual(summary1["written"], 1)

            # 同一条 issue，updated_time 未变 → 跳过
            summary2 = pipeline.run_once()
            self.assertEqual(summary2["skipped"], 1)
            self.assertEqual(len(client.custom_field_writes), 1)

    def test_auto_field_updates_when_enabled_with_mapping(self):
        """开启 AUTO_CHANGE_* 且规则含 id 映射时，才调用 update_fields。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from codearts_triage.rules import Rules

            issue = _issue(1, "登录超时 token 校验失败")
            client = FakeClient(issues=[issue])
            rules = Rules.load(None)
            rules.assignee_map = {"auth": "张三"}
            rules.assignee_id_map = {"张三": 20001}
            rules.priority_id_map = {"P2": 3}
            rules.severity_id_map = {"一般": 2}
            rules.module_id_map = {"auth": 1001}
            state = State(f"{tmp}/state.json")
            self.config.writeback_enabled = True
            self.config.auto_change_priority = True
            self.config.auto_change_assignee = True
            pipeline = TriagePipeline(client, self.config, rules, state)
            pipeline.run_once()
            self.assertEqual(len(client.field_updates), 1)
            self.assertEqual(client.field_updates[0]["priority_id"], 3)
            self.assertEqual(client.field_updates[0]["assigned_id"], 20001)

    def test_auto_field_off_by_default(self):
        """默认不开启任何 AUTO_CHANGE_*，不调用 update_fields。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from codearts_triage.rules import Rules

            issue = _issue(1, "登录超时 token 校验失败")
            client = FakeClient(issues=[issue])
            rules = Rules.load(None)
            rules.assignee_id_map = {"张三": 20001}
            state = State(f"{tmp}/state.json")
            self.config.writeback_enabled = True
            pipeline = TriagePipeline(client, self.config, rules, state)
            pipeline.run_once()
            self.assertEqual(client.field_updates, [])

    def test_readonly_mode_does_not_touch_state(self):
        """只读模式（WRITEBACK_ENABLED=false + 非 dry-run）也不写状态：
        预览语义，避免污染后续真实运行的进度（与 F3 同一原则）。"""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            self.config.writeback_enabled = False
            state_path = f"{tmp}/state.json"
            pipeline, state = self._pipeline(client, state_path)
            summary = pipeline.run_once(dry_run=False)
            self.assertEqual(summary["triaged"], 1)
            self.assertEqual(len(client.custom_field_writes), 0)
            self.assertFalse(state.is_processed(1))
            self.assertIsNone(state.get_cursor())
            self.assertFalse(os.path.exists(state_path))  # 状态文件未被创建

            # 错误场景也不写状态
            def boom(issue_id):
                raise RuntimeError("api down")

            client.show_issue = boom
            pipeline.retry_attempts = 1  # 不重试，避免等待
            summary2 = pipeline.run_once(dry_run=False)
            self.assertEqual(summary2["errors"], 1)
            self.assertEqual(state.error_count(), 0)
            self.assertFalse(os.path.exists(state_path))

            # 随后真实运行（写回开启）：仍完整分诊并写回
            self.config.writeback_enabled = True
            client.show_issue = FakeClient.show_issue.__get__(client, FakeClient)  # 恢复绑定方法
            summary3 = pipeline.run_once(dry_run=False)
            self.assertEqual(summary3["written"], 1)
            self.assertEqual(len(client.custom_field_writes), 1)

    def test_dry_run_does_not_mutate_state(self):
        """回归(F3)：dry-run 不得 mark_processed/推进游标，否则真实运行会被预览跳过。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")

            summary = pipeline.run_once(dry_run=True)
            self.assertEqual(summary["written"], 1)
            self.assertFalse(state.is_processed(1))  # 未标记
            self.assertIsNone(state.get_cursor())  # 游标未推进
            self.assertEqual(state.error_count(), 0)

            # 随后真实运行：仍然会写回
            self.config.writeback_enabled = True
            summary2 = pipeline.run_once(dry_run=False)
            self.assertEqual(summary2["written"], 1)
            self.assertEqual(len(client.custom_field_writes), 1)

    def test_cursor_not_advanced_when_errors(self):
        """回归(F1)：本轮有失败条目时游标不得推进，失败条目下轮仍在窗口内。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            good = _issue(1, "登录超时", updated="2026-08-18 09:00:00")
            bad = _issue(2, "支付失败", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[good, bad])

            def boom(issue_id):
                if issue_id == 2:
                    raise RuntimeError("api down")
                return client.details.get(issue_id) or {"id": issue_id, "description": "x"}

            client.show_issue = boom
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_attempts = 1  # 不重试，避免等待
            summary = pipeline.run_once()
            self.assertEqual(summary["errors"], 1)
            self.assertIsNone(state.get_cursor())  # 有失败 → 游标停在原地
            self.assertEqual(state.error_count(), 1)

    def test_failed_issue_retried_next_round_then_cursor_advances(self):
        """回归(F1) 恢复路径：失败条目下轮重试成功后才推进游标，不留死记录。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "支付失败", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            calls = {"n": 0}

            def flaky(issue_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("api down")
                return {"id": issue_id, "description": "支付失败金额不对"}

            client.show_issue = flaky
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_attempts = 1  # 不重试，避免等待

            # 第 1 轮：失败 → 游标不动、记错误
            summary1 = pipeline.run_once()
            self.assertEqual(summary1["errors"], 1)
            self.assertIsNone(state.get_cursor())
            self.assertEqual(state.error_count(), 1)

            # 第 2 轮：成功 → 写回、清错误、游标推进
            summary2 = pipeline.run_once()
            self.assertEqual(summary2["written"], 1)
            self.assertEqual(summary2["errors"], 0)
            self.assertEqual(state.error_count(), 0)
            self.assertEqual(state.get_cursor(), "2026-08-18 08:00:00")
            self.assertTrue(state.is_processed(1))

    def test_writeback_failure_retried_after_error(self):
        """写回失败记录错误后，下轮会重新写回并清错误（不被幂等跳过）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "支付失败", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_attempts = 1  # 不重试，避免等待

            # 第 1 轮：写回抛错 → 记错误
            def failing_writeback(issue_id, field_name, value):
                raise RuntimeError("write api down")

            client.update_custom_field = failing_writeback
            summary1 = pipeline.run_once()
            self.assertEqual(summary1["errors"], 1)
            self.assertEqual(state.error_count(), 1)

            # 第 2 轮：写回恢复 → 重新写回、清错误
            client.update_custom_field = FakeClient.update_custom_field.__get__(client, FakeClient)
            summary2 = pipeline.run_once()
            self.assertEqual(summary2["written"], 1)
            self.assertEqual(summary2["errors"], 0)
            self.assertEqual(state.error_count(), 0)
            self.assertEqual(len(client.custom_field_writes), 1)
            self.assertEqual(state.get_cursor(), "2026-08-18 08:00:00")

    def test_pending_error_blocks_cursor_even_if_not_refetched(self):
        """回归：上轮失败的条目本轮未出现在拉取结果中时，游标仍不得推进（防错误悬空）。

        连续多轮未再出现后转 poisoned，游标恢复推进（防永久阻塞）。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "支付失败", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_attempts = 1  # 不重试，避免等待
            self.config.max_error_attempts = 3

            # 第 1 轮：失败 → 记错误
            def boom(issue_id):
                raise RuntimeError("api down")

            client.show_issue = boom
            summary1 = pipeline.run_once()
            self.assertEqual(summary1["errors"], 1)
            self.assertEqual(state.error_count(), 1)

            # 第 2 轮：该条目不再出现在候选里（API 分页不一致/被删除），且无新条目
            client.issues = []
            client.details = {}
            summary2 = pipeline.run_once()
            self.assertEqual(summary2["fetched"], 0)
            self.assertEqual(summary2["errors"], 0)
            # 仍有悬空错误 → 游标保持不动
            self.assertIsNone(state.get_cursor())
            self.assertEqual(state.error_count(), 1)

            # 连续多轮未再出现 → 转 poisoned，游标不再被阻塞（max_rounds=3）
            for _ in range(2):
                pipeline.run_once()
            self.assertTrue(state.is_poisoned(1))
            self.assertEqual(state.error_count(), 0)

    def test_poisoned_issue_does_not_block_cursor(self):
        """连续失败达到上限后转 poisoned：不再阻塞游标，后续轮次跳过该条。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = _issue(1, "支付失败", updated="2026-08-18 08:00:00")
            good = _issue(2, "登录超时", updated="2026-08-18 09:00:00")
            client = FakeClient(issues=[bad, good])

            def boom(issue_id):
                if issue_id == 1:
                    raise RuntimeError("always down")
                return client.details.get(issue_id) or {"id": issue_id, "description": "x"}

            client.show_issue = boom
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.retry_attempts = 1  # 不重试，避免等待
            self.config.max_error_attempts = 2

            # 第 1 轮：bad 失败（attempt 1/2）→ 游标不动
            s1 = pipeline.run_once()
            self.assertEqual(s1["errors"], 1)
            self.assertIsNone(state.get_cursor())
            self.assertTrue(state.has_error(1))

            # 第 2 轮：bad 再失败（attempt 2/2 → poisoned），good 成功 → 游标推进到 good 的时间
            s2 = pipeline.run_once()
            self.assertEqual(s2["poisoned"], 1)
            self.assertTrue(state.is_poisoned(1))
            self.assertFalse(state.has_error(1))
            self.assertEqual(state.get_cursor(), "2026-08-18 09:00:00")

            # 第 3 轮：bad 被跳过（poisoned），不再重试
            s3 = pipeline.run_once()
            self.assertEqual(s3["errors"], 0)
            self.assertEqual(s3["poisoned"], 0)
            self.assertTrue(state.is_poisoned(1))

    def test_hash_match_skip_still_refreshes_updated_time(self):
        """回归(F2)：updated_time 变化但分诊结果 hash 相同 → 跳过写回但刷新 updated_time，避免 livelock。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()
            self.assertEqual(len(client.custom_field_writes), 1)

            # 描述被编辑（updated_time 变化）但分诊结论不变
            client.issues[0]["updated_time"] = "2026-08-18 09:00:00"
            client.details[1] = {"description": "登录超时（追加无关说明）"}
            summary = pipeline.run_once()
            self.assertEqual(summary["skipped"], 1)  # hash 相同 → 跳过
            self.assertEqual(len(client.custom_field_writes), 1)  # 未重复写
            self.assertEqual(state.get_processed_updated_time(1), "2026-08-18 09:00:00")  # updated_time 已刷新

            # 第三轮不再重新分诊（needs_retriage=False）
            summary3 = pipeline.run_once()
            self.assertEqual(summary3["skipped"], 1)

    def test_reopened_status_retriggers_same_multica_issue(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(issues=[issue])
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()
            state.mark_processed(
                1,
                "2026-08-18 08:00:00",
                state.get_triage_hash(1),
                multica_issue_id="multica-1",
                source_status_id=3,
            )

            client.issues[0]["updated_time"] = "2026-08-18 09:00:00"
            client.issues[0]["status"] = {"id": 1, "name": "新建"}
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-1")

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            pipeline.multica_sync.sync_issue.assert_called_once()
            kwargs = pipeline.multica_sync.sync_issue.call_args.kwargs
            self.assertEqual(kwargs["known_multica_issue_id"], "multica-1")
            self.assertTrue(kwargs["retrigger"])

    def test_new_test_comment_retriggers_without_status_change(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated="2026-08-18 08:00:00")
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(
                issues=[issue],
                details={1: {"comments": [{"id": "c1", "created_time": "2026-08-18 08:00:00", "comment": "待验证"}]}}
            )
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()
            state.mark_processed(
                1,
                "2026-08-18 08:00:00",
                state.get_triage_hash(1),
                multica_issue_id="multica-1",
                source_status_id=3,
                source_comment_id="c1",
            )

            client.issues[0]["updated_time"] = "2026-08-18 09:00:00"
            client.details[1]["comments"].append(
                {"id": "c2", "created_time": "2026-08-18 09:00:00", "comment": "测试未通过，请重新修改"}
            )
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-1")

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            kwargs = pipeline.multica_sync.sync_issue.call_args.kwargs
            self.assertTrue(kwargs["retrigger"])
            self.assertEqual(kwargs["latest_comment"]["id"], "c2")

    def test_new_comment_retriggers_even_when_updated_time_unchanged(self):
        """真实华为云行为：新增评论不刷新工作项 updated_time。

        已解决缺陷仅新增测试评论（updated_time 保持不动）也必须触发原任务续跑——
        回归场景：旧实现依赖 updated_time 变化，导致“仅加评论”的测试打回被静默跳过。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(1, "登录超时", updated=_recent_hw_time())
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(
                issues=[issue],
                details={1: {"comments": [{"id": "c1", "created_time": _recent_hw_time(), "comment": "待验证"}]}}
            )
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()  # 第一轮已处理并记录评论基线 c1
            self.assertEqual(state.get_source_comment_id(1), "c1")

            # 关键：只新增评论，updated_time 保持不变（华为云评论不刷新工作项更新时间）
            client.details[1]["comments"].append(
                {"id": "c2", "created_time": _recent_hw_time(hour=9), "comment": "测试未通过，请重新修改"}
            )
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-1")

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            kwargs = pipeline.multica_sync.sync_issue.call_args.kwargs
            self.assertTrue(kwargs["retrigger"])
            self.assertEqual(kwargs["latest_comment"]["id"], "c2")
            # 基线已推进，下一轮不再重复触发
            self.assertEqual(state.get_source_comment_id(1), "c2")

    def test_new_comment_retriggers_legacy_issue_without_comment_baseline(self):
        """历史快照没有评论基线（如 71083121）时，晚于处理时间的评论仍应触发。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(2, "列表页白屏", updated=_recent_hw_time())
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(issues=[issue], details={2: {"comments": []}})
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()  # 第一轮无任何评论 → 快照无 source_comment_id 基线
            self.assertIsNone(state.get_source_comment_id(2))
            state.mark_processed(
                2,
                _recent_hw_time(),
                state.get_triage_hash(2),
                multica_issue_id="multica-2",
                source_status_id=3,
            )

            # 已解决后新增评论（时间晚于快照处理时间），updated_time 依旧不变
            client.details[2]["comments"].append(
                {"id": "c9", "created_time": _recent_hw_time(hour=9), "comment": "仍可复现，请继续处理"}
            )
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-2")

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            kwargs = pipeline.multica_sync.sync_issue.call_args.kwargs
            self.assertTrue(kwargs["retrigger"])
            self.assertEqual(kwargs["latest_comment"]["id"], "c9")
            self.assertEqual(state.get_source_comment_id(2), "c9")

    def test_date_only_comment_time_retriggers_legacy_issue(self):
        """真实 ListIssueCommentsV4 只返回 YYYY-MM-DD 时也不能吞掉测试反馈。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(4, "详情页无数据", updated="2026-09-03 17:46:41")
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(
                issues=[issue],
                details={4: {"comments": [{"id": "89957499", "created_time": "2026-09-04", "comment": "仍未修好"}]}}
            )
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            state.mark_processed(
                4,
                "2026-09-03 17:46:41",
                triage_hash(pipeline.triage_issue(issue)[1]),
                multica_issue_id="multica-4",
                source_status_id=3,
            )
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-4")

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 1)
            self.assertTrue(pipeline.multica_sync.sync_issue.call_args.kwargs["retrigger"])
            self.assertEqual(state.get_source_comment_id(4), "89957499")

    def test_unparseable_comment_time_is_not_consumed_as_baseline(self):
        """无法比较的评论时间保持待探测，不能静默标记为已处理。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(5, "详情页无数据", updated=_recent_hw_time())
            issue["status"] = {"id": 3, "name": "已解决"}
            client = FakeClient(
                issues=[issue],
                details={5: {"comments": [{"id": "c-unknown", "created_time": "unknown", "comment": "仍未修好"}]}}
            )
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            state.mark_processed(
                5,
                _recent_hw_time(),
                triage_hash(pipeline.triage_issue(issue)[1]),
                multica_issue_id="multica-5",
                source_status_id=3,
            )

            summary = pipeline.run_once()

            self.assertEqual(summary["written"], 0)
            self.assertIsNone(state.get_source_comment_id(5))

    def test_legacy_comment_is_baselined_without_retrigger(self):
        """无基线历史项只含存量评论（早于处理时间）时：仅校准基线、不触发，且下轮不再探测。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            issue = _issue(3, "搜索无结果", updated=_recent_hw_time())
            issue["status"] = {"id": 5, "name": "已关闭"}
            client = FakeClient(
                issues=[issue],
                details={3: {"comments": [{"id": "old1", "created_time": _recent_hw_time(hour=7), "comment": "处理前的老评论"}]}},
            )
            pipeline, state = self._pipeline(client, f"{tmp}/state.json")
            pipeline.run_once()
            state.mark_processed(3, _recent_hw_time(), state.get_triage_hash(3), source_status_id=5)

            # 第二轮：存量老评论（早于处理时间）只应回填基线，不应触发
            pipeline.multica_sync.enabled = True
            pipeline.multica_sync.sync_issue = MagicMock(return_value="multica-3")
            summary = pipeline.run_once()
            self.assertEqual(summary["written"], 0)
            self.assertEqual(state.get_source_comment_id(3), "old1")
            pipeline.multica_sync.sync_issue.assert_not_called()

            # 第三轮：评论仍无新增 → 依旧静默
            summary = pipeline.run_once()
            self.assertEqual(summary["written"], 0)
            pipeline.multica_sync.sync_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
