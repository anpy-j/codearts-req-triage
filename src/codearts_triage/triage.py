"""分诊编排：拉取 → 去重 → 详情 → 分诊 → 代码定位 → 保守写回。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .client import F_DESCRIPTION, F_ID, F_MODULE, F_NAME, F_SEVERITY, F_UPDATED_TIME
from .code_search import merge_code_hints, search_commits_in_repo, search_in_repo
from .config import Config
from .rules import Rules
from .state import State
from .writeback import WriteBack, triage_hash

logger = logging.getLogger(__name__)


def _api_timestamp_ms(dt: datetime) -> str:
    """ProjectMan 查询时间区间格式：Unix 时间戳（毫秒）。"""
    return str(int(dt.timestamp() * 1000))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class TriagePipeline:
    def __init__(self, client, config: Config, rules: Rules, state: State):
        self.client = client
        self.config = config
        self.rules = rules
        self.state = state
        self.writeback = WriteBack(
            client,
            field_name=config.triage_field_name,
            description_append=config.description_append,
            auto={
                "severity": config.auto_change_severity,
                "priority": config.auto_change_priority,
                "module": config.auto_change_module,
                "assignee": config.auto_change_assignee,
            },
        )

    # ---- 拉取 ----

    def _build_time_interval(self) -> str:
        """增量游标时间窗口：['last_cursor - lookback', now]，格式 '开始,结束'。

        游标存的是 API 返回的 updated_time 原值（年-月-日 时:分:秒），
        发给查询接口时转换为 Unix 毫秒时间戳。首次运行回看 24 小时。
        """
        end = _now_utc()
        start_dt = end - timedelta(hours=24)
        raw_cursor = self.state.get_cursor()
        if raw_cursor:
            try:
                # API 原值无时区，按 UTC 解析（华为云 API 时间一般为 UTC）
                start_dt = datetime.strptime(raw_cursor, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("unparseable cursor %r, defaulting to 24h lookback", raw_cursor)
        start_dt = start_dt - timedelta(seconds=self.config.lookback_seconds)
        return f"{_api_timestamp_ms(start_dt)},{_api_timestamp_ms(end)}"

    def fetch_candidates(self) -> list[dict[str, Any]]:
        """按增量游标拉取缺陷列表（ListIssuesV4）。"""
        interval = self._build_time_interval()
        all_items: list[dict[str, Any]] = []
        offset = 0
        while True:
            items = self.client.list_issues(
                updated_time_interval=interval,
                tracker_ids=self.config.tracker_ids,
                limit=self.config.poll_limit,
                offset=offset,
            )
            all_items.extend(items)
            if len(items) < self.config.poll_limit:
                break
            offset += self.config.poll_limit
        return all_items

    # ---- 分诊 ----

    RETRY_ATTEMPTS = 3
    RETRY_BACKOFF = (1, 2, 4)  # 秒

    def triage_with_retry(self, issue: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """分诊单条：默认最多尝试 3 次（即最多重试 2 次），指数退避；仍失败则抛出。

        可用实例属性 retry_attempts / retry_backoff 覆盖（测试中置 0 避免等待）。
        """
        attempts = max(1, getattr(self, "retry_attempts", self.RETRY_ATTEMPTS))
        backoff = getattr(self, "retry_backoff", self.RETRY_BACKOFF)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return self.triage_issue(issue)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < attempts - 1:
                    delay = backoff[min(attempt, len(backoff) - 1)]
                    logger.warning("triage attempt %d failed for issue %s (%s), retry in %ss", attempt + 1, issue.get(F_ID), exc, delay)
                    if delay > 0:
                        time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def triage_issue(self, issue: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """单条分诊：读详情 → 规则分类 → 代码定位。返回 (detail, result)。"""
        issue_id = issue[F_ID]
        detail = self.client.show_issue(issue_id)
        title = detail.get(F_NAME) or ""
        description = detail.get(F_DESCRIPTION) or ""
        severity = (detail.get(F_SEVERITY) or {}).get("name")
        module = detail.get(F_MODULE) or {}
        module_name = (module or {}).get("name")

        text = f"{title}\n{description}"
        module_hit = self.rules.classify_module(text)
        priority = self.rules.suggest_priority(severity, text)
        assignee = self.rules.suggest_assignee(module_hit)
        keywords = self.rules.extract_keywords(text)

        # 代码定位：本地 clone 关键词搜索 + API 关联提交
        local_hits = search_in_repo(self.config.repo_local_path, keywords)
        api_commits = self.client.list_associated_commits(issue_id)
        code_hints = merge_code_hints(local_hits, api_commits)

        result = {
            "issue_id": issue_id,
            "module": module_hit,
            "module_name": module_name,
            "priority_suggestion": priority,
            "severity": severity,
            "assignee_suggestion": assignee,
            "keywords": keywords,
            "code_hints": code_hints,
            "summary": self._summarize(title, module_hit, priority),
            "triaged_at": _now_utc().isoformat(),
            "rule_version": self.rules.rule_version,
        }
        # 自动改字段（默认关）：按规则 id 映射解析，映射缺失则保持 None
        result.update(
            {
                # 与 suggest_priority 一致：严重级名先小写归一，避免大小写不匹配导致静默不生效
                "severity_id": self.rules.severity_id_map.get((severity or "").strip().lower()),
                "priority_id": self.rules.priority_id_map.get(priority),
                "module_id": self.rules.module_id_map.get(module_hit),
                "assignee_id": self.rules.assignee_id_map.get(assignee) if assignee else None,
            }
        )
        return detail, result

    @staticmethod
    def _summarize(title: str, module: str, priority: str) -> str:
        return f"「{title[:50]}」→ 模块 {module}，建议优先级 {priority}"

    # ---- 一轮 ----

    def run_once(self, dry_run: bool = False) -> dict[str, Any]:
        """执行一轮：拉取 → 过滤 → 分诊 → 写回。返回本轮统计。

        dry_run / 只读模式下**不修改任何状态**（不 mark_processed、不动游标、不记错误），
        保证先预览再真实运行的流程不会因预览而跳过真实写回。
        """
        candidates = self.fetch_candidates()
        summary = {
            "fetched": len(candidates),
            "triaged": 0,
            "skipped": 0,
            "written": 0,
            "errors": 0,
            "poisoned": 0,
            "details": [],
        }
        latest_updated: Optional[str] = self.state.get_cursor()
        # 只有真实写回模式才持久化状态（mark_processed/record_error/游标/save）。
        # dry_run 与只读模式（writeback_enabled=false）都不写状态，
        # 保证预览/只读运行不会污染后续真实运行的进度。
        persist = self.config.writeback_enabled and not dry_run

        for issue in candidates:
            issue_id = issue[F_ID]
            updated_time = issue.get(F_UPDATED_TIME)

            if self.state.is_poisoned(issue_id):
                # 连续失败已放弃（不再阻塞游标），跳过但保持记录供人工排查
                summary["skipped"] += 1
                continue

            if self.state.is_processed(issue_id):
                # 有记录错误 → 不跳过，重新分诊/写回
                if not self.state.needs_retriage(issue_id, updated_time) and not self.state.has_error(issue_id):
                    summary["skipped"] += 1
                    continue
                logger.info("issue %s changed or errored, re-triaging", issue_id)

            try:
                detail, result = self.triage_with_retry(issue)
                summary["triaged"] += 1
                h = triage_hash(result)

                # 幂等：结果未变且无未清错误 → 跳过写回，但仍需刷新 stored updated_time，
                # 否则 needs_retriage 永远为 True → 每轮重复分诊（livelock）
                if self.state.get_triage_hash(issue_id) == h and not self.state.has_error(issue_id):
                    summary["skipped"] += 1
                    if persist and updated_time:
                        self.state.mark_processed(issue_id, updated_time, h)
                        self.state.clear_error(issue_id)
                        if latest_updated is None or updated_time > latest_updated:
                            latest_updated = updated_time
                    continue

                # 写回使用详情 dict（含 description，避免覆盖原始描述）
                if persist:
                    actions = self.writeback.apply(detail, result, dry_run=False)
                    self.state.mark_processed(issue_id, updated_time, h)
                    self.state.clear_error(issue_id)
                    if updated_time and (latest_updated is None or updated_time > latest_updated):
                        latest_updated = updated_time
                else:
                    actions = self.writeback.apply(detail, result, dry_run=True)
                summary["written"] += 1
                summary["details"].append({
                    "issue_id": issue_id,
                    "actions": actions,
                    "result": result,
                    "title": detail.get("name"),
                    "description": detail.get("description"),
                })
            except Exception as exc:  # noqa: BLE001 — 单条失败不中断整轮
                logger.exception("triage failed for issue %s: %s", issue_id, exc)
                if persist:
                    poisoned = self.state.record_error(issue_id, str(exc), max_attempts=self.config.max_error_attempts)
                    if poisoned:
                        summary["poisoned"] += 1
                        logger.error("issue %s 连续失败超过 %d 次，已放弃自动重试（需人工排查）", issue_id, self.config.max_error_attempts)
                    else:
                        summary["errors"] += 1
                else:
                    summary["errors"] += 1

        # 游标：仅当本轮无待重试错误且为真实写回模式才推进。
        # 任一待重试失败都保持游标不动 → 失败条目留在窗口内，下轮重试。
        # 已处理（skipped）与已放弃（poisoned）条目也计入推进：它们已得到结论，
        # 游标越过不会丢失；这也保证错误恢复后游标能追上已成功处理的条目。
        # 只读/预览模式（writeback_enabled=false 或 dry_run）完全不写状态，
        # 避免污染后续真实运行的进度（dry-run 后真实运行仍需完整写回）。
        for issue in candidates:
            updated_time = issue.get(F_UPDATED_TIME)
            if updated_time and (latest_updated is None or updated_time > latest_updated):
                latest_updated = updated_time

        if persist:
            # 推进轮次并清理连续多轮未再出现的悬空错误（防永久阻塞游标）
            stale_poisoned = self.state.poison_stale_errors(max_rounds=max(1, self.config.max_error_attempts))
            if stale_poisoned:
                summary["poisoned"] += stale_poisoned
                logger.error("%d 条悬空错误连续多轮未再出现，已转入 poisoned（需人工排查）", stale_poisoned)

        if persist and latest_updated and summary["errors"] == 0 and self.state.error_count() == 0:
            self.state.set_cursor(latest_updated)
        if persist:
            self.state.save()
        return summary
