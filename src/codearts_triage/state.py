"""增量状态：游标 + 去重 + 幂等 + 错误队列。

持久化为 JSON 文件（STATE_FILE），默认 ./state.json。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class State:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {"cursor": None, "processed": {}, "errors": {}, "poisoned": {}, "round": 0}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {
                    "cursor": loaded.get("cursor"),
                    "processed": loaded.get("processed") or {},
                    "errors": loaded.get("errors") or {},
                    "poisoned": loaded.get("poisoned") or {},
                    "round": int(loaded.get("round") or 0),
                }
        except (OSError, ValueError) as exc:
            logger.warning("state file %s unreadable (%s), starting fresh", self.path, exc)

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---- 游标 ----

    def get_cursor(self) -> Optional[str]:
        return self._data.get("cursor")

    def set_cursor(self, value: str) -> None:
        self._data["cursor"] = value

    # ---- 去重 / 幂等 ----

    def is_processed(self, issue_id: int) -> bool:
        return str(issue_id) in self._data["processed"]

    def get_processed_updated_time(self, issue_id: int) -> Optional[str]:
        rec = self._data["processed"].get(str(issue_id))
        if not rec:
            return None
        return rec.get("updated_time")

    def mark_processed(
        self,
        issue_id: int,
        updated_time: Optional[str],
        triage_hash: Optional[str] = None,
        multica_issue_id: Optional[str] = None,
        source_status_id: Optional[int] = None,
        source_comment_id: Optional[str] = None,
    ) -> None:
        """记录处理快照，并保留同一 CodeArts Bug 对应的 Multica Issue 映射。"""
        key = str(issue_id)
        rec = dict(self._data["processed"].get(key) or {})
        rec.update({"updated_time": updated_time, "triage_hash": triage_hash})
        if multica_issue_id:
            rec["multica_issue_id"] = multica_issue_id
        if source_status_id is not None:
            rec["source_status_id"] = source_status_id
        if source_comment_id is not None:
            rec["source_comment_id"] = str(source_comment_id)
        self._data["processed"][key] = rec

    def get_multica_issue_id(self, issue_id: int) -> Optional[str]:
        rec = self._data["processed"].get(str(issue_id)) or {}
        return rec.get("multica_issue_id")

    def get_source_status_id(self, issue_id: int) -> Optional[int]:
        rec = self._data["processed"].get(str(issue_id)) or {}
        return rec.get("source_status_id")

    def get_source_comment_id(self, issue_id: int) -> Optional[str]:
        rec = self._data["processed"].get(str(issue_id)) or {}
        value = rec.get("source_comment_id")
        return str(value) if value is not None else None

    def iter_processed(self) -> list:
        """遍历已处理记录 (issue_id_str, rec) 对，供评论水位等增量探测使用。"""
        return list(self._data["processed"].items())

    def update_source_snapshot(
        self,
        issue_id: int,
        updated_time: Optional[str] = None,
        source_status_id: Optional[int] = None,
        source_comment_id: Optional[str] = None,
    ) -> None:
        """记录由本程序主动产生的 CodeArts 变更，防止下一轮误判为测试打回。"""
        key = str(issue_id)
        rec = dict(self._data["processed"].get(key) or {})
        if updated_time is not None:
            rec["updated_time"] = updated_time
        if source_status_id is not None:
            rec["source_status_id"] = source_status_id
        if source_comment_id is not None:
            rec["source_comment_id"] = str(source_comment_id)
        self._data["processed"][key] = rec

    def needs_retriage(self, issue_id: int, updated_time: Optional[str]) -> bool:
        """已处理但 updated_time 变化 → 需要重新分诊。"""
        rec = self._data["processed"].get(str(issue_id))
        if not rec:
            return True
        return bool(updated_time) and rec.get("updated_time") != updated_time

    def get_triage_hash(self, issue_id: int) -> Optional[str]:
        """已写入的分诊结果 hash（幂等判断用）。"""
        rec = self._data["processed"].get(str(issue_id))
        if not rec:
            return None
        return rec.get("triage_hash")

    # ---- 错误队列 ----

    def record_error(self, issue_id: int, message: str, max_attempts: int = 5) -> bool:
        """记录错误并累计重试次数；超过 max_attempts 后转入 poisoned（不再阻塞游标），返回 True 表示已放弃。"""
        key = str(issue_id)
        rec = self._data["errors"].get(key)
        attempts = (rec.get("attempts", 0) if isinstance(rec, dict) else 0) + 1
        if attempts >= max_attempts:
            self._data["poisoned"][key] = message
            self._data["errors"].pop(key, None)
            return True
        self._data["errors"][key] = {"message": message, "attempts": attempts, "round": self._data.get("round", 0)}
        return False

    def poison_stale_errors(self, max_rounds: int = 5) -> int:
        """推进轮次计数；连续 max_rounds 轮未被重新拉取/记录的错误转入 poisoned，避免永久阻塞游标。

        返回本次新转 poisoned 的数量。
        """
        self._data["round"] = self._data.get("round", 0) + 1
        current = self._data["round"]
        poisoned_count = 0
        for key in list(self._data["errors"]):
            rec = self._data["errors"][key]
            last_round = rec.get("round", 0) if isinstance(rec, dict) else 0
            if current - last_round >= max_rounds:
                self._data["poisoned"][key] = rec.get("message", "stale error") if isinstance(rec, dict) else str(rec)
                self._data["errors"].pop(key, None)
                poisoned_count += 1
        return poisoned_count

    def has_error(self, issue_id: int) -> bool:
        return str(issue_id) in self._data["errors"]

    def is_poisoned(self, issue_id: int) -> bool:
        return str(issue_id) in self._data["poisoned"]

    def clear_error(self, issue_id: int) -> None:
        self._data["errors"].pop(str(issue_id), None)
        self._data["poisoned"].pop(str(issue_id), None)

    def error_count(self) -> int:
        return len(self._data["errors"])
