"""环境变量配置。所有敏感信息只从环境变量读取，绝不写入代码或文档。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_int_list(name: str, default: str) -> list[int]:
    raw = os.getenv(name, default)
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out or [3]


def _env_str_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    if not raw or not raw.strip():
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _env_json_dict(name: str) -> dict[str, str]:
    raw = os.getenv(name, "")
    if not raw or not raw.strip():
        return {}
    try:
        import json
        val = json.loads(raw)
        return {str(k): str(v) for k, v in val.items()} if isinstance(val, dict) else {}
    except Exception:
        return {}


@dataclass
class Config:
    """从环境变量构建的运行配置。"""

    project_id: str = field(default_factory=lambda: os.getenv("HW_PROJECT_ID", ""))
    region: str = field(default_factory=lambda: os.getenv("HW_REGION", "cn-north-4"))

    ak_read: str = field(default_factory=lambda: os.getenv("HW_AK_READ", ""))
    sk_read: str = field(default_factory=lambda: os.getenv("HW_SK_READ", ""))
    ak_write: str = field(default_factory=lambda: os.getenv("HW_AK_WRITE", ""))
    sk_write: str = field(default_factory=lambda: os.getenv("HW_SK_WRITE", ""))

    tracker_ids: list[int] = field(default_factory=lambda: _env_int_list("TRACKER_IDS", "3"))
    poll_limit: int = field(default_factory=lambda: _env_int("POLL_LIMIT", 100))
    poll_interval_seconds: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 300))
    lookback_seconds: int = field(default_factory=lambda: _env_int("LOOKBACK_SECONDS", 60))
    # 华为云项目时区偏移（默认东八区北京时间 UTC+8）
    timezone_offset_hours: int = field(default_factory=lambda: _env_int("TIMEZONE_OFFSET_HOURS", 8))

    state_file: str = field(default_factory=lambda: os.getenv("STATE_FILE", "./state.json"))
    rules_file: str = field(default_factory=lambda: os.getenv("RULES_FILE", "./rules.yaml"))
    repo_local_path: str = field(default_factory=lambda: os.getenv("CODEARTS_REPO_LOCAL_PATH", ""))
    # 单条连续失败达到该次数后放弃自动重试（转 poisoned），避免永久阻塞游标
    max_error_attempts: int = field(
        default_factory=lambda: max(1, _env_int("MAX_ERROR_ATTEMPTS", 5))
    )

    writeback_enabled: bool = field(default_factory=lambda: _env_bool("WRITEBACK_ENABLED", False))
    triage_field_name: str = field(default_factory=lambda: os.getenv("TRIAGE_FIELD_NAME", "AI分诊"))
    description_append: bool = field(default_factory=lambda: _env_bool("DESCRIPTION_APPEND", True))

    auto_change_severity: bool = field(default_factory=lambda: _env_bool("AUTO_CHANGE_SEVERITY", False))
    auto_change_priority: bool = field(default_factory=lambda: _env_bool("AUTO_CHANGE_PRIORITY", False))
    auto_change_module: bool = field(default_factory=lambda: _env_bool("AUTO_CHANGE_MODULE", False))
    auto_change_assignee: bool = field(default_factory=lambda: _env_bool("AUTO_CHANGE_ASSIGNEE", False))

    # Multica 平台联动配置
    multica_sync_enabled: bool = field(default_factory=lambda: _env_bool("MULTICA_SYNC_ENABLED", False))
    multica_assignee_id: str = field(
        default_factory=lambda: os.getenv("MULTICA_ASSIGNEE_ID", "")
    )
    multica_sync_min_priority: str = field(default_factory=lambda: os.getenv("MULTICA_SYNC_MIN_PRIORITY", "P4"))
    # 处理人白名单过滤：逗号分隔的用户名/姓名（如 dev_user1,dev_user2），为空则同步所有处理人
    multica_sync_handlers: list[str] = field(default_factory=lambda: _env_str_list("MULTICA_SYNC_HANDLERS", ""))
    # 华为云处理人 -> Multica 成员 ID 映射表（JSON 字符串）
    multica_handler_mapping: dict[str, str] = field(default_factory=lambda: _env_json_dict("MULTICA_HANDLER_MAPPING"))
    # Multica 目标归属项目（名称或 UUID）
    multica_project: str = field(default_factory=lambda: os.getenv("MULTICA_PROJECT", ""))
    # 模块/系统 -> Multica 项目及负责人路由映射
    multica_project_mapping: dict[str, dict[str, str]] = field(default_factory=lambda: _env_json_dict("MULTICA_PROJECT_MAPPING"))
    # 是否直接指派给对应智能体并触发任务（默认 False：存入成员 Multica 收件箱，静默待命，不直接启动智能体任务）
    multica_auto_assign_agent: bool = field(default_factory=lambda: _env_bool("MULTICA_AUTO_ASSIGN_AGENT", False))
    # 修复完成后的目标测试合流分支（如 test-cloud，可在命令行或定时任务动态指定）
    test_branch: str = field(default_factory=lambda: os.getenv("TEST_BRANCH", "test-cloud"))

    @property
    def has_read_credentials(self) -> bool:
        return bool(self.project_id and self.ak_read and self.sk_read)

    @property
    def has_write_credentials(self) -> bool:
        return bool(self.ak_write and self.sk_write)
