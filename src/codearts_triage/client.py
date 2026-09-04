"""ProjectMan 客户端封装。

对官方 SDK 做薄封装，返回普通 dict，便于 mock 测试（测试不导入 SDK、不联网、无凭据）。
真实运行才导入 huaweicloudsdkprojectman。

REST 路径（已在方案 §3 核验，SDK 3.1.210）：
- ListIssuesV4:            POST /v4/projects/{project_id}/issues
- ShowIssueV4:             GET  /v4/projects/{project_id}/issues/{issue_id}
- UpdateIssueV4:           PUT  /v4/projects/{project_id}/issues/{issue_id}
- ListIssueCommentsV4:     GET  /v4/projects/{project_id}/issues/{issue_id}/comments
- ListIssueAssociatedCommits: GET /v4/projects/{project_id}/issues/{issue_id}/associated-commits
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 关键字段常量（与 SDK 模型字段一致）
F_ID = "id"
F_NAME = "name"
F_DESCRIPTION = "description"
F_SEVERITY = "severity"
F_PRIORITY = "priority"
F_MODULE = "module"
F_STATUS = "status"
F_TRACKER = "tracker"
F_CREATED_TIME = "created_time"
F_UPDATED_TIME = "updated_time"
F_ASSIGNED_USER = "assigned_user"


def _associated_commits_request_kwargs(
    project_id: str, issue_id: int, limit: int, offset: int
) -> dict[str, Any]:
    """构造关联提交查询参数；当前 API 要求显式传 type=commit。"""
    return {
        "project_id": project_id,
        "issue_id": issue_id,
        "type": "commit",
        "limit": limit,
        "offset": offset,
    }


def _ref_dict(obj: Any) -> Optional[dict]:
    """SDK 的 {id, name} 小对象 → dict。"""
    if obj is None:
        return None
    return {"id": getattr(obj, "id", None), "name": getattr(obj, "name", None)}


def _issue_item_to_dict(item: Any) -> dict:
    """ListIssuesV4 响应项 → dict（列表接口不含 description）。"""
    return {
        F_ID: getattr(item, "id", None),
        F_NAME: getattr(item, "name", None),
        F_SEVERITY: _ref_dict(getattr(item, "severity", None)),
        F_PRIORITY: _ref_dict(getattr(item, "priority", None)),
        F_MODULE: _ref_dict(getattr(item, "module", None)),
        F_STATUS: _ref_dict(getattr(item, "status", None)),
        F_TRACKER: _ref_dict(getattr(item, "tracker", None)),
        F_CREATED_TIME: getattr(item, "created_time", None),
        F_UPDATED_TIME: getattr(item, "updated_time", None),
        F_ASSIGNED_USER: _ref_dict(getattr(item, "assigned_user", None)),
    }


def _issue_detail_to_dict(item: Any) -> dict:
    """ShowIssueV4 详情 → dict（含 description 和 new_custom_fields）。"""
    d = _issue_item_to_dict(item)
    d[F_DESCRIPTION] = getattr(item, "description", None)
    new_custom_fields = getattr(item, "new_custom_fields", None) or []
    d["new_custom_fields"] = [
        {
            "custom_field": getattr(f, "custom_field", None) if not isinstance(f, dict) else f.get("custom_field"),
            "field_name": getattr(f, "field_name", None) if not isinstance(f, dict) else f.get("field_name"),
            "value": getattr(f, "value", None) if not isinstance(f, dict) else f.get("value"),
            "field_type": getattr(f, "field_type", None) if not isinstance(f, dict) else f.get("field_type"),
        }
        for f in new_custom_fields
    ]
    return d


class ProjectManClient:
    """官方 SDK 薄封装。构造时传入凭据，只读/写回共用同一封装。"""

    def __init__(self, region: str, ak: str, sk: str, project_id: str, auth_token: str = ""):
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkprojectman.v4.projectman_client import ProjectManClient as SDKClient
        from huaweicloudsdkprojectman.v4.region.projectman_region import ProjectManRegion

        self.project_id = project_id
        self.auth_token = auth_token.strip()
        self._client = (
            SDKClient.new_builder()
            .with_credentials(BasicCredentials(ak, sk))
            .with_region(ProjectManRegion.value_of(region))
            .build()
        )

    # ---- 读 ----

    def list_issues(
        self,
        updated_time_interval: Optional[str] = None,
        tracker_ids: Optional[list[int]] = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> list[dict]:
        """增量查询工作项。updated_time_interval 为逗号分隔的 Unix 毫秒时间戳。"""
        from huaweicloudsdkprojectman.v4.model import (
            ListIssueRequestV4,
            ListIssuesV4Request,
        )

        body = ListIssueRequestV4(
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
        )
        if tracker_ids:
            body.tracker_ids = tracker_ids
        if updated_time_interval:
            body.updated_time_interval = updated_time_interval
        request = ListIssuesV4Request(project_id=self.project_id, body=body)
        response = self._client.list_issues_v4(request)
        return [_issue_item_to_dict(i) for i in (response.issues or [])]

    def show_issue(self, issue_id: int) -> dict:
        """查询单条工作项详情（含 description）。"""
        from huaweicloudsdkprojectman.v4.model import ShowIssueV4Request

        request = ShowIssueV4Request(project_id=self.project_id, issue_id=issue_id)
        response = self._client.show_issue_v4(request)
        return _issue_detail_to_dict(response)

    def list_comments(self, issue_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
        """获取工作项评论列表。"""
        from huaweicloudsdkprojectman.v4.model import ListIssueCommentsV4Request

        request = ListIssueCommentsV4Request(
            project_id=self.project_id, issue_id=issue_id, limit=limit, offset=offset
        )
        response = self._client.list_issue_comments_v4(request)
        return [{"id": c.id, "comment": c.comment, "created_time": c.created_time} for c in (response.comments or [])]

    def add_comment(self, issue_id: int, notes: str) -> dict:
        """给 Scrum 工作项添加评论。

        AddIssueNotes（POST /v2/issues/update-issue-notes）已进入官方 API，但当前
        huaweicloudsdkprojectman 尚未生成对应方法，因此复用 SDK Core 的 AK/SK 签名与
        HTTP/异常处理能力发起原始请求。
        """
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["X-Auth-Token"] = self.auth_token
        try:
            response = self._client.call_api(
                "/v2/issues/update-issue-notes",
                "POST",
                header_params=headers,
                body={
                    "id": int(issue_id),
                    "notes": notes,
                    "project_uuid": self.project_id,
                    "type": "scrum",
                },
            )
        except Exception as exc:
            if not self.auth_token:
                raise RuntimeError(
                    "CodeArts AddIssueNotes rejected AK/SK signing; set HW_AUTH_TOKEN "
                    "from an IAM project token in the local .env (never post it to an issue)"
                ) from exc
            raise
        raw = getattr(response, "raw_content", b"") or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"raw": str(raw)}
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            raise RuntimeError(f"CodeArts add comment failed: {data}")
        return data if isinstance(data, dict) else {"result": data}

    def list_associated_commits(self, issue_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        """查询工作项已关联的代码提交记录。"""
        from huaweicloudsdkprojectman.v4.model import ListIssueAssociatedCommitsRequest

        request = ListIssueAssociatedCommitsRequest(
            **_associated_commits_request_kwargs(self.project_id, issue_id, limit, offset)
        )
        response = self._client.list_issue_associated_commits(request)
        commits = getattr(response, "commits", None) or []
        return [
            {
                "repository_id": c.repository_id,
                "commit_id": c.commit_id,
                "commit_short_id": c.commit_short_id,
                "commit_msg": c.commit_msg,
                "commit_url": c.commit_url,
                "branch_name": c.branch_name,
            }
            for c in commits
        ]

    # ---- 写（保守）----

    def update_custom_field(
        self, issue_id: int, field_name: str, value: str, custom_field: Optional[str] = None
    ) -> None:
        """写入自定义字段（分诊结论主通道）。field_name 需先在 Req 项目设置中创建。"""
        from huaweicloudsdkprojectman.v4.model import (
            IssueRequestV4,
            NewCustomField,
            UpdateIssueV4Request,
        )

        if not custom_field:
            try:
                detail = self.show_issue(issue_id)
                for cf in detail.get("new_custom_fields") or []:
                    if cf.get("field_name") == field_name or cf.get("custom_field") == field_name:
                        custom_field = cf.get("custom_field")
                        break
            except Exception:
                pass

        if not custom_field:
            logger.info("Custom field '%s' not configured on issue %s, skipping API call", field_name, issue_id)
            return

        body = IssueRequestV4(
            new_custom_fields=[NewCustomField(custom_field=custom_field, field_name=field_name, value=value)]
        )
        request = UpdateIssueV4Request(project_id=self.project_id, issue_id=issue_id, body=body)
        self._client.update_issue_v4(request)

    def update_description(self, issue_id: int, description: str) -> None:
        """更新描述（追加分诊摘要用，调用方负责拼接原文）。"""
        from huaweicloudsdkprojectman.v4.model import (
            IssueRequestV4,
            UpdateIssueV4Request,
        )
        import re

        # 清理华为云不支持的 4 字节 emoji 表情字符（防 PM.02101004）
        sanitized = re.sub(r"[\U00010000-\U0010FFFF\uD800-\uDFFF]", "", description)
        body = IssueRequestV4(description=sanitized)
        request = UpdateIssueV4Request(project_id=self.project_id, issue_id=issue_id, body=body)
        self._client.update_issue_v4(request)

    def update_fields(
        self,
        issue_id: int,
        severity_id: Optional[int] = None,
        priority_id: Optional[int] = None,
        module_id: Optional[int] = None,
        assigned_id: Optional[int] = None,
    ) -> None:
        """可选自动改字段（默认关闭，需成员明确授权）。只传非 None 字段。"""
        from huaweicloudsdkprojectman.v4.model import (
            IssueRequestV4,
            UpdateIssueV4Request,
        )

        kwargs: dict[str, Any] = {}
        if severity_id is not None:
            kwargs["severity_id"] = severity_id
        if priority_id is not None:
            kwargs["priority_id"] = priority_id
        if module_id is not None:
            kwargs["module_id"] = module_id
        if assigned_id is not None:
            kwargs["assigned_id"] = assigned_id
        if not kwargs:
            return
        body = IssueRequestV4(**kwargs)
        request = UpdateIssueV4Request(project_id=self.project_id, issue_id=issue_id, body=body)
        self._client.update_issue_v4(request)

    def update_status(self, issue_id: int, status_id: int = 3) -> None:
        """更新缺陷状态（默认 3: 已解决，2: 进行中，1: 新建，5: 已关闭）。"""
        from huaweicloudsdkprojectman.v4.model import (
            IssueRequestV4,
            UpdateIssueV4Request,
        )

        body = IssueRequestV4(status_id=status_id)
        request = UpdateIssueV4Request(project_id=self.project_id, issue_id=issue_id, body=body)
        self._client.update_issue_v4(request)


class FakeClient:
    """测试用内存客户端：记录调用、返回固定数据，不联网无凭据。

    与真实 API 一致：list_issues 返回的条目**不含** description，
    description 只能通过 show_issue 拿到（防止写回时覆盖描述这类回归）。
    """

    def __init__(self, issues: Optional[list[dict]] = None, details: Optional[dict[int, dict]] = None):
        self.issues = list(issues or [])
        self.details = dict(details or {})
        self.calls: list[str] = []
        self.custom_field_writes: list[tuple[int, str, str]] = []
        self.description_writes: list[tuple[int, str]] = []
        self.field_updates: list[dict] = []
        self.status_updates: list[tuple[int, int]] = []
        self.comment_writes: list[tuple[int, str]] = []

    def list_issues(self, updated_time_interval=None, tracker_ids=None, limit=100, offset=0, include_deleted=False):
        self.calls.append(f"list_issues:{updated_time_interval}")
        out = self.issues
        if tracker_ids:
            out = [i for i in out if (i.get("tracker") or {}).get("id") in tracker_ids]
        # 与真实 API 一致：列表条目不含 description
        stripped = [{k: v for k, v in i.items() if k != "description"} for i in out]
        return stripped[offset : offset + limit]

    def show_issue(self, issue_id):
        self.calls.append(f"show_issue:{issue_id}")
        item = next((i for i in self.issues if i.get("id") == issue_id), {})
        detail = self.details.get(issue_id, {})
        merged = {**item, **detail}
        merged.setdefault("description", "（无描述）")
        return merged

    def list_comments(self, issue_id, limit=100, offset=0):
        self.calls.append(f"list_comments:{issue_id}")
        return list(self.details.get(issue_id, {}).get("comments", []))[offset : offset + limit]

    def add_comment(self, issue_id, notes):
        self.calls.append(f"add_comment:{issue_id}")
        self.comment_writes.append((issue_id, notes))
        comments = self.details.setdefault(issue_id, {}).setdefault("comments", [])
        comment_id = f"ai-{len(comments) + 1}"
        comments.append({"id": comment_id, "comment": notes, "created_time": "2099-01-01"})
        return {"status": "success"}

    def list_associated_commits(self, issue_id, limit=50, offset=0):
        self.calls.append(f"associated_commits:{issue_id}")
        return self.details.get(issue_id, {}).get("commits", [])

    def update_custom_field(self, issue_id, field_name, value, custom_field=None):
        self.calls.append(f"update_custom_field:{issue_id}")
        self.custom_field_writes.append((issue_id, field_name, value))

    def update_description(self, issue_id, description):
        self.calls.append(f"update_description:{issue_id}")
        self.description_writes.append((issue_id, description))

    def update_fields(self, issue_id, severity_id=None, priority_id=None, module_id=None, assigned_id=None):
        self.calls.append(f"update_fields:{issue_id}")
        self.field_updates.append(
            {
                "issue_id": issue_id,
                "severity_id": severity_id,
                "priority_id": priority_id,
                "module_id": module_id,
                "assigned_id": assigned_id,
            }
        )

    def update_status(self, issue_id, status_id=3):
        self.calls.append(f"update_status:{issue_id}:{status_id}")
        self.status_updates.append((issue_id, status_id))
