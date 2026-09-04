"""修复交付闭环：脱敏评论写入 CodeArts 后再更新缺陷状态。"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional


AI_COMMENT_MARKER = "[AI处理结果]"
MAX_COMMENT_CHARS = 16000


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s'\"<]+"),
    re.compile(r"(?i)(\b(?:token|access[_-]?token|refresh[_-]?token|password|passwd|pwd)\b\s*[:=]\s*)[^\s,;'\"<]+"),
    re.compile(r"(?i)(\bHW_(?:AK|SK)(?:_READ|_WRITE)?\b\s*=\s*)[^\s]+"),
    re.compile(r"(?im)^(\s*(?:cookie|-b)\s*[:=]?\s*).+$"),
)


def sanitize_comment_text(text: str) -> str:
    """清理常见凭据并限制长度，保留 SQL/Markdown 的可读性。"""
    cleaned = str(text or "").replace("\x00", "").strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
    if len(cleaned) > MAX_COMMENT_CHARS:
        cleaned = cleaned[: MAX_COMMENT_CHARS - 32].rstrip() + "\n\n[内容过长，已截断]"
    return cleaned


def prepare_ai_comment(text: str) -> str:
    cleaned = sanitize_comment_text(text)
    if not cleaned:
        raise ValueError("CodeArts comment is empty after sanitization")
    if AI_COMMENT_MARKER not in cleaned:
        cleaned = f"{AI_COMMENT_MARKER}\n\n{cleaned}"
    return cleaned


def comment_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_ai_comment(comment: Optional[dict[str, Any]]) -> bool:
    return bool(comment and AI_COMMENT_MARKER in str(comment.get("comment") or ""))


def latest_comment(comments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return max(
        comments,
        key=lambda item: (
            str(item.get("created_time") or ""),
            str(item.get("id") or ""),
        ),
        default=None,
    )


def complete_issue(client, state, issue_id: int, comment_text: Optional[str] = None) -> dict[str, Any]:
    """先幂等写 AI 评论，再设为已解决，最后刷新来源快照。

    评论成功后立即持久化 hash：若随后状态更新失败，重试只补状态，不重复发评论。
    评论失败会直接抛错，因而不会把缺陷提前标记为已解决。
    """
    comment_written = False
    prepared: Optional[str] = None
    if comment_text is not None:
        prepared = prepare_ai_comment(comment_text)
        digest = comment_digest(prepared)
        if state.get_delivered_comment_hash(issue_id) != digest:
            client.add_comment(issue_id, prepared)
            remote_latest = None
            try:
                remote_latest = latest_comment(client.list_comments(issue_id))
            except Exception:
                # 评论请求已经成功；即使回读失败，也先记录 hash 防止状态重试时重复发送。
                remote_latest = None
            remote_id = (
                str(remote_latest.get("id"))
                if remote_latest and remote_latest.get("id") is not None
                else None
            )
            state.mark_delivered_comment(issue_id, digest, source_comment_id=remote_id)
            state.save()
            comment_written = True

    client.update_status(issue_id, status_id=3)

    detail = client.show_issue(issue_id)
    remote_latest = latest_comment(client.list_comments(issue_id))
    remote_id = (
        str(remote_latest.get("id"))
        if remote_latest and remote_latest.get("id") is not None
        else None
    )
    state.update_source_snapshot(
        issue_id,
        updated_time=detail.get("updated_time"),
        source_status_id=3,
        source_comment_id=remote_id,
    )
    state.save()
    return {
        "comment_written": comment_written,
        "comment_skipped": prepared is not None and not comment_written,
        "source_comment_id": remote_id,
    }
