"""分诊编排：拉取 → 去重 → 详情 → 分诊 → 代码定位 → 保守写回。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .client import F_DESCRIPTION, F_ID, F_MODULE, F_NAME, F_SEVERITY, F_STATUS, F_UPDATED_TIME
from .code_search import merge_code_hints, search_commits_in_repo, search_in_repo
from .config import Config
from .rules import Rules
from .state import State
from .writeback import WriteBack, strip_old_triage_block, triage_hash

logger = logging.getLogger(__name__)


def _api_timestamp_ms(dt: datetime) -> str:
    """ProjectMan 查询时间区间格式：Unix 时间戳（毫秒）。"""
    return str(int(dt.timestamp() * 1000))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_hw_datetime(value) -> Optional[datetime]:
    """解析华为云返回的本地时间字符串（兼容 'YYYY-MM-DD HH:MM:SS'、带 T 及毫秒变体）。

    华为云 updated_time / 评论 created_time 为项目时区（默认东八区）的本地时间、无时区标记，
    两者同口径直接比较即可。无法解析返回 None，调用方保守处理（不触发）。
    """
    if not value:
        return None
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


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
        from codearts_triage.multica_sync import MulticaSync

        self.multica_sync = MulticaSync(
            enabled=getattr(config, "multica_sync_enabled", False),
            assignee_id=getattr(config, "multica_assignee_id", None),
            min_priority=getattr(config, "multica_sync_min_priority", "P4"),
            sync_handlers=getattr(config, "multica_sync_handlers", None),
            handler_mapping=getattr(config, "multica_handler_mapping", None),
            multica_project=getattr(config, "multica_project", None),
            project_mapping=getattr(config, "multica_project_mapping", None),
            auto_assign_agent=getattr(config, "multica_auto_assign_agent", False),
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
                # 华为云 API 返回的 updated_time 为项目时区（默认东八区北京时间 UTC+8）
                # 必须按对应时区解析，转换为 Unix 毫秒时间戳传给 API
                offset = getattr(self.config, "timezone_offset_hours", 8)
                c_tz = timezone(timedelta(hours=offset))
                start_dt = datetime.strptime(raw_cursor, "%Y-%m-%d %H:%M:%S").replace(tzinfo=c_tz)
            except ValueError:
                logger.warning("unparseable cursor %r, defaulting to 24h lookback", raw_cursor)
        start_dt = start_dt - timedelta(seconds=self.config.lookback_seconds)
        if start_dt > end:
            logger.warning("start_dt %s > end %s, adjusting to lookback window", start_dt, end)
            start_dt = end - timedelta(seconds=self.config.lookback_seconds)
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
        raw_description = detail.get(F_DESCRIPTION) or ""
        # 自己追加的分诊块不能再次进入规则引擎，否则会造成 hash 漂移和重复触发。
        description = strip_old_triage_block(raw_description)
        detail["_source_description"] = description
        try:
            detail["_comments"] = self.client.list_comments(issue_id)
        except Exception as exc:  # 评论是重触发增强信息，读取失败不应阻断主分诊。
            logger.warning("failed to list comments for issue %s: %s", issue_id, exc)
            detail["_comments"] = []
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
    def _latest_comment(detail: dict[str, Any]) -> Optional[dict[str, Any]]:
        comments = detail.get("_comments") or []
        if not comments:
            return None
        return max(
            comments,
            key=lambda item: (
                str(item.get("created_time") or ""),
                str(item.get("id") or ""),
            ),
        )

    def _should_retrigger(
        self,
        issue_id: int,
        current_hash: str,
        source_status_id: Optional[int],
        source_comment_id: Optional[str],
    ) -> bool:
        """已同步 Bug 的正文/测试反馈变化，或回到开放态时，重新运行原任务。"""
        if not self.state.is_processed(issue_id):
            return False
        if self.state.get_triage_hash(issue_id) != current_hash:
            return True
        if source_comment_id and self.state.get_source_comment_id(issue_id) != source_comment_id:
            return True
        if (
            source_status_id in (1, 2)
            and self.state.get_source_status_id(issue_id) != source_status_id
        ):
            return True
        return False

    # 华为云新增评论不会刷新工作项 updated_time（ListIssuesV4 增量窗口与 needs_retriage
    # 都只认它），因此“已解决/已关闭后测试打回评论”必须单独按评论水位探测：对近期处理过的
    # 已解决/已关闭或已同步 Multica 的已知缺陷，每轮直接 ListIssueCommentsV4 对比最新评论
    # 与快照基线 source_comment_id，发现新评论时强制重处理（即使 updated_time 未变）。
    COMMENT_PROBE_MAX_AGE_DAYS = 7  # 只盯近期处理过的项，控制每轮评论查询成本

    def _probe_comment_retriggers(self, persist: bool = True) -> set[int]:
        """探测“仅新增评论”的测试打回，返回需要强制重处理的 CodeArts issue id 集合。

        - 快照已有评论基线：最新评论 id 与基线不一致 → 触发。
        - 快照无基线（历史记录未记录过评论）：以“最新评论时间晚于快照处理时间”判定；
          无法比较或评论更早时只校准基线、不触发（避免把存量历史评论误判为新反馈）。
        校准写回仅在 persist=True（真实写回模式）执行，只读/预览轮次不污染状态。
        """
        pending: set[int] = set()
        # 华为云字段为项目时区（默认东八）本地时间，与快照解析口径保持一致
        offset = getattr(self.config, "timezone_offset_hours", 8)
        now = datetime.now(timezone(timedelta(hours=offset))).replace(tzinfo=None)
        for key, rec in self.state.iter_processed():
            issue_id = int(key)
            source_status_id = rec.get("source_status_id")
            if source_status_id not in (3, 5) and not rec.get("multica_issue_id"):
                # 开放态(1/2) 已由 updated_time 增量覆盖；其余未同步项无需盯评论
                continue
            rec_time = _parse_hw_datetime(rec.get("updated_time"))
            if rec_time is None or (now - rec_time).days > self.COMMENT_PROBE_MAX_AGE_DAYS:
                continue
            try:
                comments = self.client.list_comments(issue_id)
            except Exception as exc:  # noqa: BLE001 — 单条探测失败不阻断整轮
                logger.warning("failed to list comments for comment-probe issue %s: %s", issue_id, exc)
                continue
            latest = (
                max(
                    comments,
                    key=lambda item: (
                        str(item.get("created_time") or ""),
                        str(item.get("id") or ""),
                    ),
                )
                if comments
                else None
            )
            latest_id = str(latest.get("id")) if latest and latest.get("id") is not None else None
            baseline = self.state.get_source_comment_id(issue_id)
            if baseline:
                if latest_id is not None and latest_id != baseline:
                    pending.add(issue_id)
            elif latest_id is not None:
                comment_time = _parse_hw_datetime(latest.get("created_time"))
                if comment_time is not None and comment_time > rec_time:
                    pending.add(issue_id)
                elif persist:
                    # 存量历史评论：回填基线，避免下一轮重复探测误判
                    self.state.update_source_snapshot(issue_id, source_comment_id=latest_id)
        return pending

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

        # 评论水位增量：华为云新增评论不刷新工作项 updated_time，只靠 updated_time 窗口会漏掉
        # “已解决/已关闭后仅新增评论”的测试打回。此处探测到新评论的已处理缺陷即使 updated_time
        # 未变也强制重处理（并把它补进本轮候选，若其 updated_time 已滑出增量窗口）。
        forced_ids = self._probe_comment_retriggers(persist=persist)
        all_issues = list(candidates)
        for forced_id in sorted(forced_ids):
            if not any(item[F_ID] == forced_id for item in all_issues):
                all_issues.append({
                    F_ID: forced_id,
                    F_UPDATED_TIME: self.state.get_processed_updated_time(forced_id),
                })

        for issue in all_issues:
            issue_id = issue[F_ID]
            updated_time = issue.get(F_UPDATED_TIME)
            forced = issue_id in forced_ids

            if self.state.is_poisoned(issue_id):
                # 连续失败已放弃（不再阻塞游标），跳过但保持记录供人工排查
                summary["skipped"] += 1
                continue

            if self.state.is_processed(issue_id):
                # 有记录错误 → 不跳过，重新分诊/写回；评论水位命中的强制项同样不跳过
                if (
                    not forced
                    and not self.state.needs_retriage(issue_id, updated_time)
                    and not self.state.has_error(issue_id)
                ):
                    summary["skipped"] += 1
                    continue
                logger.info("issue %s changed or errored, re-triaging", issue_id)

            try:
                was_processed = self.state.is_processed(issue_id)
                detail, result = self.triage_with_retry(issue)
                summary["triaged"] += 1
                h = triage_hash(result)
                source_status = detail.get(F_STATUS) or {}
                source_status_id = source_status.get("id") if isinstance(source_status, dict) else None
                latest_comment = self._latest_comment(detail)
                source_comment_id = (
                    str(latest_comment.get("id"))
                    if latest_comment and latest_comment.get("id") is not None
                    else None
                )
                known_multica_id = self.state.get_multica_issue_id(issue_id)
                should_retrigger = self._should_retrigger(
                    issue_id,
                    h,
                    source_status_id,
                    source_comment_id,
                )

                # 幂等：结果未变且无未清错误 → 跳过写回，但仍需刷新 stored updated_time，
                # 否则 needs_retriage 永远为 True → 每轮重复分诊（livelock）
                if self.state.get_triage_hash(issue_id) == h and not self.state.has_error(issue_id):
                    actions: list[str] = []
                    multica_id = known_multica_id
                    if should_retrigger and self.multica_sync and self.multica_sync.enabled:
                        if persist:
                            multica_id = self.multica_sync.sync_issue(
                                issue_id=issue_id,
                                title=detail.get("name", ""),
                                description=detail.get("_source_description", ""),
                                result=result,
                                assigned_user=detail.get("assigned_user"),
                                raw_module=detail.get("module"),
                                raw_issue=detail,
                                multica_project=getattr(self.config, "multica_project", None),
                                project_id=self.config.project_id,
                                region=self.config.region,
                                test_branch=getattr(self.config, "test_branch", "test-cloud"),
                                known_multica_issue_id=known_multica_id,
                                retrigger=True,
                                source_status=source_status,
                                latest_comment=latest_comment,
                                source_updated_time=updated_time,
                            )
                            if multica_id:
                                actions.append(f"retriggered existing Multica issue {multica_id}")
                        else:
                            actions.append("[dry-run] would append feedback and rerun the existing Multica issue")

                    if actions:
                        summary["written"] += 1
                        summary["details"].append({
                            "issue_id": issue_id,
                            "actions": actions,
                            "result": result,
                            "title": detail.get("name"),
                            "description": detail.get("_source_description"),
                        })
                    else:
                        summary["skipped"] += 1
                    if persist and updated_time:
                        self.state.mark_processed(
                            issue_id,
                            updated_time,
                            h,
                            multica_issue_id=multica_id,
                            source_status_id=source_status_id,
                            source_comment_id=source_comment_id,
                        )
                        self.state.clear_error(issue_id)
                        if latest_updated is None or updated_time > latest_updated:
                            latest_updated = updated_time
                    elif persist and forced:
                        # 评论触发的历史项若快照缺少 updated_time，至少刷新评论基线/状态，防下轮重复触发
                        self.state.update_source_snapshot(
                            issue_id,
                            source_status_id=source_status_id,
                            source_comment_id=source_comment_id,
                        )
                        self.state.clear_error(issue_id)
                    continue

                if persist:
                    actions = self.writeback.apply(detail, result, dry_run=False)
                    multica_id = known_multica_id
                    if self.multica_sync and self.multica_sync.enabled:
                        multica_id = self.multica_sync.sync_issue(
                            issue_id=issue_id,
                            title=detail.get("name", ""),
                            description=detail.get("_source_description", ""),
                            result=result,
                            assigned_user=detail.get("assigned_user"),
                            raw_module=detail.get("module"),
                            raw_issue=detail,
                            multica_project=getattr(self.config, "multica_project", None),
                            project_id=self.config.project_id,
                            region=self.config.region,
                            test_branch=getattr(self.config, "test_branch", "test-cloud"),
                            known_multica_issue_id=known_multica_id,
                            retrigger=was_processed,
                            source_status=source_status,
                            latest_comment=latest_comment,
                            source_updated_time=updated_time,
                        )
                        if multica_id:
                            verb = "reused/retriggered" if was_processed else "synced/reused"
                            actions.append(f"{verb} Multica issue {multica_id}")
                    self.state.mark_processed(
                        issue_id,
                        updated_time,
                        h,
                        multica_issue_id=multica_id,
                        source_status_id=source_status_id,
                        source_comment_id=source_comment_id,
                    )
                    self.state.clear_error(issue_id)
                    if updated_time and (latest_updated is None or updated_time > latest_updated):
                        latest_updated = updated_time
                else:
                    actions = self.writeback.apply(detail, result, dry_run=True)
                    if getattr(self.config, "multica_sync_enabled", False):
                        p, a, detected = self.multica_sync.resolve_target(
                            raw_issue=detail,
                            module_name=detail.get("module") or result.get("module"),
                            title=detail.get("name"),
                            description=detail.get("_source_description"),
                            assigned_user=detail.get("assigned_user"),
                            override_project=getattr(self.config, "multica_project", None),
                        )
                        actions.append(f"[dry-run] would sync to Multica (platform={detected}, project={p}, assignee={a})")
                summary["written"] += 1
                summary["details"].append({
                    "issue_id": issue_id,
                    "actions": actions,
                    "result": result,
                    "title": detail.get("name"),
                    "description": detail.get("_source_description"),
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
