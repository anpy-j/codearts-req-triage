"""Multica 平台联动：新缺陷自动同步创建 Multica Issue 进收件箱。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


DEFAULT_PROJECT_MAPPING = {
    "商城业务平台": {
        "project": "trade-system-backend",
        "assignee": "trade-dev-agent",
        "keywords": ["trade", "pay", "order", "mall", "商城", "交易"],
    },
    "买家端App": {
        "project": "buyer-app-service",
        "assignee": "app-dev-agent",
        "keywords": ["app", "buyer", "mobile", "client", "移动端"],
    },
    "运营管理后台": {
        "project": "admin-portal-service",
        "assignee": "admin-dev-agent",
        "keywords": ["admin", "manage", "portal", "dashboard", "管理后台"],
    },
}


def _load_local_mapping() -> Optional[dict[str, Any]]:
    for filename in ("project_mapping.local.json", "project_mapping.json"):
        p = Path(filename)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def parse_project_mapping(raw: Any) -> dict[str, dict[str, Any]]:
    """解析 project_mapping 配置，支持字典、JSON 字符串或 key:project:assignee 简写。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        res = {}
        for k, v in raw.items():
            k_clean = str(k).strip()
            if isinstance(v, dict):
                agent_val = str(v.get("agent") or v.get("assignee") or "").strip() or None
                res[k_clean] = {
                    "project": str(v.get("project", "")).strip(),
                    "assignee": agent_val,
                    "agent": agent_val,
                    "keywords": v.get("keywords", []),
                }
            elif isinstance(v, str):
                res[k_clean] = {"project": str(v).strip(), "assignee": None, "agent": None, "keywords": []}
        return res
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parse_project_mapping(parsed)
        except Exception:
            pass
        res = {}
        items = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
        for item in items:
            parts = [p.strip() for p in item.split(":") if p.strip()]
            if len(parts) >= 3:
                res[parts[0]] = {"project": parts[1], "assignee": parts[2], "agent": parts[2], "keywords": []}
            elif len(parts) == 2:
                res[parts[0]] = {"project": parts[1], "assignee": None, "agent": None, "keywords": []}
        return res
    return {}


class MulticaSync:
    """将分诊结果同步创建为 Multica Issue，推送到成员收件箱。"""

    def __init__(
        self,
        enabled: bool = False,
        assignee_id: Optional[str] = None,
        min_priority: str = "P4",
        sync_handlers: Optional[list[str]] = None,
        handler_mapping: Optional[dict[str, str]] = None,
        multica_project: Optional[str] = None,
        project_mapping: Optional[Any] = None,
        auto_assign_agent: bool = False,
    ):
        self.enabled = enabled
        self.assignee_id = assignee_id
        self.min_priority = min_priority
        self.sync_handlers = [h.strip().lower() for h in (sync_handlers or []) if h.strip()]
        self.handler_mapping = {k.strip().lower(): str(v).strip() for k, v in (handler_mapping or {}).items()}
        self.multica_project = multica_project
        self.auto_assign_agent = auto_assign_agent
        parsed_mapping = parse_project_mapping(project_mapping)
        if not parsed_mapping:
            parsed_mapping = parse_project_mapping(_load_local_mapping())
        self.project_mapping = parsed_mapping or DEFAULT_PROJECT_MAPPING

    def should_sync(
        self,
        result: dict[str, Any],
        assigned_user: Optional[dict[str, Any]] = None,
    ) -> bool:
        if not self.enabled:
            return False
        priority = result.get("priority_suggestion", "P2")
        levels = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
        p_val = levels.get(priority, 2)
        min_val = levels.get(self.min_priority, 4)
        if p_val > min_val:
            return False

        # 处理人白名单过滤
        if self.sync_handlers:
            if not assigned_user:
                logger.info("Skipping Multica sync: issue has no assigned_user while sync_handlers=%s", self.sync_handlers)
                return False
            name = str(assigned_user.get("name") or "").lower()
            uid = str(assigned_user.get("id") or "")
            matched = any(h in name or h == uid for h in self.sync_handlers)
            if not matched:
                logger.info(
                    "Skipping Multica sync: assigned_user '%s' (id=%s) does not match sync_handlers %s",
                    name,
                    uid,
                    self.sync_handlers,
                )
                return False

        return True

    def resolve_assignee(self, assigned_user: Optional[dict[str, Any]] = None) -> Optional[str]:
        """根据华为云处理人查找对应的 Multica 成员 ID；未匹配则回退到默认 assignee_id。"""
        if assigned_user and self.handler_mapping:
            name = str(assigned_user.get("name") or "").lower()
            uid = str(assigned_user.get("id") or "")
            for pattern, mapped_id in self.handler_mapping.items():
                if pattern in name or pattern == uid:
                    return mapped_id
        return self.assignee_id

    def resolve_project(self, project_name_or_id: Optional[str] = None) -> Optional[str]:
        """将 Multica 项目名称（如 service-backend、app-cloud）或 ID 解析为项目 UUID。"""
        target = (project_name_or_id or self.multica_project or "").strip()
        if not target:
            return None
        if len(target) == 36 and target.count("-") == 4:
            return target
        multica_bin = shutil.which("multica") or "multica"
        if not multica_bin:
            return None
        try:
            proc = subprocess.run(
                [multica_bin, "project", "list", "--output", "json"],
                capture_output=True,
                text=True,
                check=True,
            )
            projs = json.loads(proc.stdout)
            for p in projs:
                if str(p.get("title", "")).strip().lower() == target.lower():
                    return p.get("id")
        except Exception as e:
            logger.warning("Failed to resolve Multica project '%s': %s", target, e)
        return None

    def resolve_assignee_id(self, target_assignee: Optional[str]) -> Optional[str]:
        """将负责人名称动态解析为 UUID，避免同名歧义。"""
        if not target_assignee:
            return None
        target = target_assignee.strip()
        if len(target) == 36 and target.count("-") == 4:
            return target

        multica_bin = shutil.which("multica") or "multica"
        if not multica_bin:
            return None

        try:
            proc = subprocess.run([multica_bin, "agent", "list", "--output", "json"], capture_output=True, text=True)
            if proc.returncode == 0:
                agents = json.loads(proc.stdout)
                for a in agents:
                    if str(a.get("name", "")).strip().lower() == target.lower():
                        return a.get("id")
        except Exception:
            pass

        try:
            proc = subprocess.run([multica_bin, "member", "list", "--output", "json"], capture_output=True, text=True)
            if proc.returncode == 0:
                members = json.loads(proc.stdout)
                for m in members:
                    if str(m.get("name", "")).strip().lower() == target.lower():
                        return m.get("id")
        except Exception:
            pass

        return None

    def find_existing_issue(
        self, issue_id: int, known_multica_issue_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """查找同一 CodeArts Bug 已创建的 Multica Issue（含已关闭任务）。"""
        multica_bin = shutil.which("multica") or "multica"
        if known_multica_issue_id:
            try:
                proc = subprocess.run(
                    [multica_bin, "issue", "get", known_multica_issue_id, "--output", "json"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(proc.stdout)
            except Exception:
                logger.warning(
                    "Stored Multica issue %s for CodeArts bug %s is unavailable; searching by title",
                    known_multica_issue_id,
                    issue_id,
                )

        title_prefix = f"【CodeArts Bug #{issue_id}】"
        try:
            proc = subprocess.run(
                [
                    multica_bin,
                    "issue",
                    "search",
                    title_prefix,
                    "--include-closed",
                    "--limit",
                    "20",
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
            matches = [
                item
                for item in payload.get("issues", [])
                if str(item.get("title") or "").startswith(title_prefix)
            ]
            # 历史版本若已有重复卡片，固定复用最早创建的原任务。
            return min(matches, key=lambda item: item.get("created_at") or "") if matches else None
        except Exception as exc:
            # 不确定是否已有任务时禁止继续 create，以免 CLI/网络抖动制造重复卡片。
            raise RuntimeError(
                f"failed to check existing Multica issue for CodeArts bug {issue_id}"
            ) from exc

    def _set_codearts_metadata(self, multica_issue_id: str, codearts_issue_id: int) -> None:
        multica_bin = shutil.which("multica") or "multica"
        try:
            subprocess.run(
                [
                    multica_bin,
                    "issue",
                    "metadata",
                    "set",
                    multica_issue_id,
                    "--key",
                    "codearts_bug_id",
                    "--value",
                    str(codearts_issue_id),
                    "--type",
                    "number",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception as exc:
            logger.warning("Failed to set CodeArts metadata on Multica issue %s: %s", multica_issue_id, exc)

    def _retrigger_existing_issue(
        self,
        existing: dict[str, Any],
        codearts_issue_id: int,
        source_status: Optional[dict[str, Any]] = None,
        latest_comment: Optional[dict[str, Any]] = None,
        source_updated_time: Optional[str] = None,
    ) -> None:
        """把测试打回追加到原任务，并重新运行原任务的当前智能体。"""
        multica_issue_id = str(existing.get("id") or "")
        if not multica_issue_id:
            return

        status_name = str((source_status or {}).get("name") or "未提供")
        lines = [
            "## CodeArts 重新处理通知",
            "",
            f"- **华为云 Bug**：`#{codearts_issue_id}`",
            f"- **当前状态**：{status_name}",
        ]
        if source_updated_time:
            lines.append(f"- **华为云更新时间**：{source_updated_time}")
        if latest_comment and latest_comment.get("comment"):
            feedback = str(latest_comment.get("comment") or "").strip()[:4000]
            lines.extend(["", "### 最新测试反馈", "", feedback])
        lines.extend(["", "请继续在本任务中修复和验证，不要新建重复任务。"])

        multica_bin = shutil.which("multica") or "multica"
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as fh:
                fh.write("\n".join(lines))
                temp_file = fh.name
            subprocess.run(
                [
                    multica_bin,
                    "issue",
                    "comment",
                    "add",
                    multica_issue_id,
                    "--content-file",
                    temp_file,
                    "--allow-external-file",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            status_category = str(existing.get("status_category") or existing.get("status") or "")
            if str(existing.get("assignee_type") or "") == "agent":
                # 运行中只追加反馈，避免产生并发重复 run；其他状态均续跑原任务。
                if status_category != "in_progress":
                    subprocess.run(
                        [multica_bin, "issue", "rerun", multica_issue_id, "--output", "json"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
            elif status_category != "todo":
                # 成员负责的任务回到其收件箱，仍然使用原卡片。
                subprocess.run(
                    [multica_bin, "issue", "status", multica_issue_id, "todo"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    def detect_platform(
        self,
        raw_issue: Optional[dict[str, Any]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[str]:
        """按三层优先级判定缺陷归属平台：
        1. 优先读取华为云自定义字段「平台」；
        2. 其次读取标题【前缀】或关键词；
        3. 最后根据正文中的 curl 请求/URL/接口路径特征识别。
        """
        mapping = self.project_mapping

        # --- 1. 优先根据「平台」字段 ---
        if raw_issue:
            cfs = raw_issue.get("new_custom_fields") or raw_issue.get("custom_fields") or []
            for cf in cfs:
                fn = str(cf.get("field_name") or cf.get("custom_field") or "").strip()
                if fn == "平台":
                    val = str(cf.get("value") or "").strip()
                    if val and val not in ("None", "null"):
                        for key in mapping:
                            if key.lower() == val.lower():
                                return key
                        for key in mapping:
                            if key.lower() in val.lower() or val.lower() in key.lower():
                                return key
                        return val

        # --- 2. 其次根据标题 ---
        if title:
            t_clean = title.strip()
            t_lower = t_clean.lower()
            for key, conf in mapping.items():
                k_lower = key.lower()
                if f"【{k_lower}】" in t_lower or k_lower in t_lower:
                    return key
                keywords = conf.get("keywords", []) if isinstance(conf, dict) else []
                for kw in keywords:
                    if kw.lower() in t_lower:
                        return key

        # --- 3. 最后根据正文中的 curl / 接口特征 ---
        if description:
            d_clean = description.strip()
            d_lower = d_clean.lower()
            for key, conf in mapping.items():
                keywords = conf.get("keywords", []) if isinstance(conf, dict) else []
                for kw in keywords:
                    if kw.lower() in d_lower:
                        return key

        return None

    def resolve_recommended_agent(self, detected_platform: Optional[str]) -> Optional[str]:
        if not detected_platform or not self.project_mapping:
            return None
        conf = self.project_mapping.get(detected_platform)
        if isinstance(conf, dict):
            return conf.get("agent") or conf.get("assignee")
        d_lower = detected_platform.lower()
        for key, c in self.project_mapping.items():
            if key.lower() == d_lower or key.lower() in d_lower or d_lower in key.lower():
                if isinstance(c, dict):
                    return c.get("agent") or c.get("assignee")
        return None

    def resolve_target(
        self,
        raw_issue: Optional[dict[str, Any]] = None,
        module_name: Optional[Any] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        assigned_user: Optional[dict[str, Any]] = None,
        override_project: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """根据平台三层判定/模块/标题/处理人解析目标 Multica 项目与指派人。"""
        target_project = override_project or self.multica_project
        target_assignee = self.resolve_assignee(assigned_user)

        mapping = self.project_mapping

        def _apply_conf(conf: dict[str, Any]):
            nonlocal target_project, target_assignee
            if conf.get("project"):
                target_project = conf["project"]
            agent_or_assignee = conf.get("agent") or conf.get("assignee")
            if agent_or_assignee:
                if self.auto_assign_agent:
                    target_assignee = agent_or_assignee
                elif not self.assignee_id and conf.get("assignee"):
                    target_assignee = conf["assignee"]

        # 1. 优先根据三层规则探测平台
        detected = self.detect_platform(raw_issue=raw_issue, title=title, description=description)

        # 2. 如果匹配到平台且在 mapping 中有精确配置
        if detected and detected in mapping:
            conf = mapping[detected]
            if isinstance(conf, dict):
                _apply_conf(conf)
            return target_project, target_assignee, detected

        # 3. 兜底模糊匹配平台名称
        if detected:
            d_lower = detected.lower()
            for key, conf in mapping.items():
                if key.lower() in d_lower or d_lower in key.lower():
                    if isinstance(conf, dict):
                        _apply_conf(conf)
                    return target_project, target_assignee, detected

        # 4. 兜底匹配 module_name 和 title 关键字
        if mapping:
            check_targets = []
            if isinstance(module_name, dict):
                m_str = str(module_name.get("name") or "").strip().lower()
                if m_str:
                    check_targets.append(m_str)
            elif module_name:
                check_targets.append(str(module_name).strip().lower())
            if title:
                check_targets.append(title.strip().lower())

            for target_str in check_targets:
                for key, conf in mapping.items():
                    if isinstance(conf, dict):
                        k_lower = key.lower()
                        keywords = conf.get("keywords", [])
                        if k_lower in target_str or any(kw.lower() in target_str for kw in keywords):
                            _apply_conf(conf)
                            return target_project, target_assignee, key

        return target_project, target_assignee, detected

    def sync_issue(
        self,
        issue_id: int,
        title: str,
        description: str,
        result: dict[str, Any],
        assigned_user: Optional[dict[str, Any]] = None,
        raw_module: Optional[Any] = None,
        raw_issue: Optional[dict[str, Any]] = None,
        multica_project: Optional[str] = None,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        test_branch: Optional[str] = None,
        known_multica_issue_id: Optional[str] = None,
        retrigger: bool = False,
        source_status: Optional[dict[str, Any]] = None,
        latest_comment: Optional[dict[str, Any]] = None,
        source_updated_time: Optional[str] = None,
    ) -> Optional[str]:
        if not self.should_sync(result, assigned_user):
            return None

        multica_bin = shutil.which("multica") or "multica"
        if not multica_bin:
            logger.warning("multica CLI not found")
            return None

        try:
            existing = self.find_existing_issue(issue_id, known_multica_issue_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to sync CodeArts bug {issue_id} to Multica") from exc
        if existing:
            multica_issue_id = str(existing.get("id") or "")
            if multica_issue_id and retrigger:
                self._retrigger_existing_issue(
                    existing,
                    issue_id,
                    source_status=source_status,
                    latest_comment=latest_comment,
                    source_updated_time=source_updated_time,
                )
                logger.info("Retriggered existing Multica issue %s for CodeArts bug %s", multica_issue_id, issue_id)
            return multica_issue_id or None

        issue_title = f"【CodeArts Bug #{issue_id}】{title}"
        p_map = {"P0": "urgent", "P1": "high", "P2": "medium", "P3": "low", "P4": "low"}
        p_val = p_map.get(result.get("priority_suggestion", "P2"), "medium")

        handler_str = assigned_user.get("name") if assigned_user else "未分配"
        mod_val = raw_module.get("name") if isinstance(raw_module, dict) else (raw_module or result.get("module", "other"))
        target_test_branch = (test_branch or "test-cloud").strip()
        desc_lines = [
            f"## 华为云 CodeArts Req 缺陷 #{issue_id}",
            f"- **标题**：{title}",
            f"- **所属模块**：`{mod_val}`",
            f"- **建议优先级**：`{result.get("priority_suggestion", "P2")}`",
            f"- **严重级别**：{result.get("severity", "一般")}",
            f"- **华为云处理人**：`{handler_str}`",
        ]
        target_project_name, target_assignee, detected_platform = self.resolve_target(
            raw_issue=raw_issue,
            module_name=raw_module or result.get("module"),
            title=title,
            description=description,
            assigned_user=assigned_user,
            override_project=multica_project,
        )
        recommended_agent = self.resolve_recommended_agent(detected_platform)
        if detected_platform:
            desc_lines.append(f"- **识别所属平台**：`{detected_platform}`")
        if target_project_name:
            desc_lines.append(f"- **归属项目**：`{target_project_name}`")
        if recommended_agent:
            desc_lines.append(f"- **推荐排查智能体**：🤖 `{recommended_agent}`")
        if target_test_branch:
            desc_lines.append(f"- **目标测试分支**：`{target_test_branch}`")
        if result.get("assignee_suggestion"):
            desc_lines.append(f"- **AI 建议负责人**：{result.get('assignee_suggestion')}")
        if result.get("keywords"):
            desc_lines.append(f"- **命中关键词**：{', '.join(result.get('keywords'))}")
        if project_id and region:
            url = f"https://devcloud.{region}.myhuaweicloud.com/projectman/workitems/issues/{project_id}/detail/{issue_id}"
            desc_lines.append(f"- **华为云直达链接**：<{url}>")
        desc_lines.extend(["", "### 原始问题描述", description or "（无描述）"])
        proj_arg = f"--hw-project {project_id}" if project_id else ""
        resolve_cmd = f"python main.py {proj_arg} --resolve {issue_id}".strip()
        git_flow_cmd = f"python git_flow.py -b {target_test_branch}"

        desc_lines.extend([
            "",
            "### 🛠 缺陷处理与交付规范（处理完毕后必须执行）",
            f"1. **代码提交与分支合流**：排查并修复代码后，将修改提交并合并推送到目标测试分支 **`{target_test_branch}`**（可使用命令 `{git_flow_cmd}` 或执行标准 git merge 推送）；",
            f"2. **华为云缺陷状态回调**：分支合流完成后，必须执行以下指令将华为云 Bug #{issue_id} 状态更新为「已解决」：",
            "   ```bash",
            f"   {resolve_cmd}",
            "   ```",
            "3. **看板任务交付**：确认华为云状态更新完成后，将本 Multica 任务卡片状态变更为 `in_review`。",
        ])

        target_project_id = self.resolve_project(target_project_name)

        temp_file = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as f:
                f.write("\n".join(desc_lines))
                temp_file = f.name

            cmd = [
                multica_bin,
                "issue",
                "create",
                "--title",
                issue_title,
                "--description-file",
                temp_file,
                "--allow-external-file",
                "--priority",
                p_val,
                "--output",
                "json",
            ]
            if target_project_id:
                cmd.extend(["--project", target_project_id])
            if target_assignee:
                resolved_aid = self.resolve_assignee_id(target_assignee)
                if resolved_aid:
                    cmd.extend(["--assignee-id", resolved_aid])
                elif len(target_assignee) == 36 and target_assignee.count("-") == 4:
                    cmd.extend(["--assignee-id", target_assignee])
                else:
                    cmd.extend(["--assignee", target_assignee])

            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(proc.stdout)
            multica_issue_id = data.get("id")
            if multica_issue_id:
                self._set_codearts_metadata(multica_issue_id, issue_id)
            logger.info("Created Multica issue %s for CodeArts bug %s (assignee: %s)", multica_issue_id, issue_id, target_assignee)
            return multica_issue_id
        except Exception as e:
            raise RuntimeError(f"Failed to create Multica issue for CodeArts bug {issue_id}") from e
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
