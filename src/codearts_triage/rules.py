"""分诊规则：模块分类、优先级建议、负责人建议、关键词提取。

规则文件支持 YAML（推荐，需 PyYAML）或 JSON。加载失败时回退到内置默认规则。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_RULES: dict[str, Any] = {
    "module_keywords": {
        "auth": ["登录", "登出", "认证", "鉴权", "token", "权限", "403", "session", "密码", "验证码"],
        "payment": ["支付", "订单", "退款", "金额", "余额", "发票", "交易", "扣款"],
        "api": ["接口", "api", "超时", "500", "502", "504", "网关", "报错", "崩溃", "异常"],
        "ui": ["页面", "样式", "前端", "白屏", "布局", "点击", "显示"],
    },
    "severity_priority": {
        "fatal": "P0", "致命": "P0",
        "critical": "P1", "严重": "P1",
        "normal": "P2", "一般": "P2",
        "minor": "P3", "轻微": "P3",
    },
    "priority_upgrade": {
        "P0": ["崩溃", "数据丢失", "安全漏洞", "资金", "无法登录"],
        "P1": ["核心功能不可用", "大面积", "性能严重"],
    },
    "assignee_map": {},
    "default_module": "other",
    "rule_version": "builtin-1.0",
}


class Rules:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.module_keywords: dict[str, list[str]] = data.get("module_keywords") or {}
        self.severity_priority: dict[str, str] = data.get("severity_priority") or {}
        self.priority_upgrade: dict[str, list[str]] = data.get("priority_upgrade") or {}
        self.assignee_map: dict[str, str] = data.get("assignee_map") or {}
        self.default_module: str = data.get("default_module") or "other"
        self.rule_version: str = str(data.get("rule_version") or "builtin-1.0")
        # 自动改字段（默认关）所需的 id 映射；映射缺失时对应字段保持 None（不写）
        self.severity_id_map: dict[str, int] = data.get("severity_id_map") or {}
        self.priority_id_map: dict[str, int] = data.get("priority_id_map") or {}
        self.module_id_map: dict[str, int] = data.get("module_id_map") or {}
        self.assignee_id_map: dict[str, int] = data.get("assignee_id_map") or {}

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Rules":
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    if path.endswith((".yaml", ".yml")):
                        import yaml

                        data = yaml.safe_load(fh) or {}
                    else:
                        data = json.load(fh)
                if isinstance(data, dict) and data:
                    logger.info("loaded rules from %s", path)
                    return cls(data)
                logger.warning("rules file %s empty/invalid, using defaults", path)
            except Exception as exc:  # noqa: BLE001 — 规则文件异常不阻塞运行
                logger.warning("failed to load rules %s (%s), using defaults", path, exc)
        return cls(DEFAULT_RULES)

    # ---- 分类 ----

    def classify_module(self, text: str) -> str:
        lowered = (text or "").lower()
        for module, keywords in self.module_keywords.items():
            for kw in keywords:
                if kw.lower() in lowered:
                    return module
        return self.default_module

    # ---- 优先级 ----

    def suggest_priority(self, severity_name: Optional[str], text: str) -> str:
        lowered = (text or "").lower()
        base = self.severity_priority.get((severity_name or "").strip().lower())
        if not base:
            base = "P2"  # 无法识别严重级时默认中等
        # 关键词升级
        for level in ("P0", "P1"):
            for kw in self.priority_upgrade.get(level, []):
                if kw.lower() in lowered:
                    return level
        return base

    # ---- 负责人 ----

    def suggest_assignee(self, module: str) -> Optional[str]:
        return self.assignee_map.get(module)

    # ---- 关键词（用于代码搜索）----

    def extract_keywords(self, text: str, max_keywords: int = 5) -> list[str]:
        """提取英文标识符/接口名/类名作为代码搜索关键词。"""
        import re

        # 驼峰/下划线标识符、接口路径、数字错误码
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text or "")
        seen: list[str] = []
        for t in tokens:
            low = t.lower()
            if low in ("the", "and", "for", "bug", "issue", "error", "not", "with", "this", "when", "has"):
                continue
            if low not in seen:
                seen.append(low)
            if len(seen) >= max_keywords:
                break
        return seen
