"""保守写回：自定义字段 + 描述追加 + 可选自动改字段。

策略（与 docs/INTEGRATION_PLAN.md §0/§5 一致）：
1. 必做（开启写回后）：UpdateIssueV4 写自定义字段「AI 分诊」；
2. 可选：description 末尾追加带标记的分诊摘要；
3. 默认关：自动改 severity/priority/module/assignee，需成员逐项开启。
幂等：分诊结果 hash 与状态文件中的记录一致则跳过写回。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

import re

logger = logging.getLogger(__name__)

TRIAGE_MARKER_START = "<!-- AI-TRIAGE -->"
TRIAGE_MARKER_END = "<!-- /AI-TRIAGE -->"

EMOJI_PATTERN = re.compile(
    "["
    "\U00010000-\U0010FFFF"  # 4 字节 Unicode 表情符号（华为云 CodeArts Req 抛 PM.02101004 不支持）
    "\uD800-\uDFFF"          # 孤立代理项
    "]+",
    flags=re.UNICODE,
)


def sanitize_req_text(text: str) -> str:
    """清理华为云 CodeArts Req 不支持的字符（如 4 字节 Unicode Emoji 表情 🤖 等）。"""
    if not text:
        return text
    return EMOJI_PATTERN.sub("", text)


def triage_hash(result: dict[str, Any]) -> str:
    """分诊结果 hash，用于幂等判断。

    排除 `triaged_at`（每次运行都变化）等时间戳，只对稳定分诊结论取 hash，
    使「结果未变 → 跳过写回」真正生效。
    """
    stable = {k: v for k, v in result.items() if k not in ("triaged_at",)}
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_triage_json(result: dict[str, Any]) -> str:
    """自定义字段内容：一行 JSON（与 triage_hash 使用同一稳定投影，含 triaged_at 除外）。"""
    stable = {k: v for k, v in result.items() if k not in ("triaged_at",)}
    return sanitize_req_text(json.dumps(stable, ensure_ascii=False, sort_keys=True))


def build_description_block(result: dict[str, Any]) -> str:
    """描述追加的分诊摘要块（带标记，可回滚）。"""
    lines = [
        "",
        TRIAGE_MARKER_START,
        "## AI 分诊摘要",
        f"- 模块：{result.get('module', 'other')}",
        f"- 优先级建议：{result.get('priority_suggestion', 'P2')}",
    ]
    assignee = result.get("assignee_suggestion")
    if assignee:
        lines.append(f"- 负责人建议：{assignee}")
    keywords = result.get("keywords") or []
    if keywords:
        lines.append(f"- 关键词：{'、'.join(keywords)}")
    hints = result.get("code_hints") or []
    if hints:
        lines.append("- 代码线索：")
        for h in hints[:5]:
            if h.get("kind") == "file":
                lines.append(f"  - `{h.get('file')}:{h.get('line')}`")
            else:
                lines.append(f"  - commit `{h.get('commit_short_id') or h.get('commit')}` {h.get('commit_msg', '')[:60]}")
    lines.append(f"- 说明：自动化分诊生成，仅供参考（规则 v{result.get('rule_version', '?')}）")
    lines.append(TRIAGE_MARKER_END)
    return sanitize_req_text("\n".join(lines))


def strip_old_triage_block(description: Optional[str]) -> str:
    """移除旧的分诊摘要块，避免重复追加。"""
    if not description:
        return ""
    start = description.find(TRIAGE_MARKER_START)
    end = description.find(TRIAGE_MARKER_END)
    if start != -1 and end != -1 and end > start:
        return (description[:start] + description[end + len(TRIAGE_MARKER_END) :]).rstrip()
    header_idx = description.find("## AI 分诊摘要")
    if header_idx != -1:
        footer_idx = description.find("- 说明：自动化分诊生成", header_idx)
        if footer_idx != -1:
            next_newline = description.find("\n", footer_idx)
            end_idx = next_newline if next_newline != -1 else len(description)
            return (description[:header_idx] + description[end_idx:]).rstrip()
        return description[:header_idx].rstrip()
    return description.rstrip()


class WriteBack:
    """执行保守写回；dry_run 时只记录不调用客户端。"""

    def __init__(self, client, field_name: str, description_append: bool, auto: dict[str, bool]):
        self.client = client
        self.field_name = field_name
        self.description_append = description_append
        self.auto = auto  # {"severity": bool, "priority": bool, "module": bool, "assignee": bool}

    def apply(self, issue: dict[str, Any], result: dict[str, Any], dry_run: bool = False) -> list[str]:
        actions: list[str] = []
        issue_id = issue["id"]

        # 1. 自定义字段（主通道）
        payload = build_triage_json(result)
        custom_field_slot = None
        for cf in issue.get("new_custom_fields") or []:
            if cf.get("field_name") == self.field_name or cf.get("custom_field") == self.field_name:
                custom_field_slot = cf.get("custom_field")
                break

        if dry_run:
            actions.append(f"[dry-run] update custom field '{self.field_name}' = {payload[:80]}...")
        else:
            try:
                try:
                    self.client.update_custom_field(issue_id, self.field_name, payload, custom_field=custom_field_slot)
                except TypeError:
                    self.client.update_custom_field(issue_id, self.field_name, payload)
                actions.append(f"updated custom field '{self.field_name}'")
            except Exception as e:
                err_msg = str(e)
                if "PM.02303005" in err_msg or "PM.00000001" in err_msg or "无权限" in err_msg or "网络繁忙" in err_msg:
                    logger.warning("CodeArts custom field update skipped for issue %s: %s", issue_id, e)
                    actions.append("custom field write skipped (CodeArts API error)")
                else:
                    raise

        # 2. 描述追加（带标记，先剥离旧块）
        if self.description_append:
            old = (issue.get("description") or "").strip()
            new_desc = strip_old_triage_block(old) + "\n" + build_description_block(result)
            if dry_run:
                actions.append("[dry-run] append triage block to description")
            else:
                try:
                    self.client.update_description(issue_id, new_desc)
                    actions.append("appended triage block to description")
                except Exception as e:
                    err_msg = str(e)
                    if "PM.02303005" in err_msg or "PM.00000001" in err_msg or "无权限" in err_msg or "网络繁忙" in err_msg:
                        logger.warning("CodeArts description update skipped for issue %s: %s", issue_id, e)
                        actions.append("description append skipped (CodeArts API error)")
                    else:
                        raise

        # 3. 自动改字段（全部默认关）
        updates: dict[str, Optional[int]] = {
            "severity": result.get("severity_id"),
            "priority": result.get("priority_id"),
            "module": result.get("module_id"),
            "assignee": result.get("assignee_id"),
        }
        if any(self.auto.values()) and any(updates.values()):
            if dry_run:
                actions.append(f"[dry-run] auto field updates would be: {updates}")
            else:
                try:
                    self.client.update_fields(
                        issue_id,
                        severity_id=updates["severity"] if self.auto.get("severity") else None,
                        priority_id=updates["priority"] if self.auto.get("priority") else None,
                        module_id=updates["module"] if self.auto.get("module") else None,
                        assigned_id=updates["assignee"] if self.auto.get("assignee") else None,
                    )
                    actions.append(f"auto updated fields: {updates}")
                except Exception as e:
                    if "PM.02303005" in str(e) or "无权限" in str(e):
                        logger.warning("CodeArts fields update skipped (no permission for issue %s): %s", issue_id, e)
                        actions.append("auto fields update skipped (no permission: PM.02303005)")
                    else:
                        raise
        return actions
