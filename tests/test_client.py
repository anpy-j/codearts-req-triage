"""ProjectMan 客户端请求参数测试（不导入 SDK、不联网）。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import ProjectManClient, _associated_commits_request_kwargs


class ProjectManClientRequestTest(unittest.TestCase):
    def test_associated_commits_requires_commit_type(self):
        kwargs = _associated_commits_request_kwargs("project-id", 123, 50, 0)

        self.assertEqual(kwargs["type"], "commit")
        self.assertEqual(kwargs["project_id"], "project-id")
        self.assertEqual(kwargs["issue_id"], 123)
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["offset"], 0)

    def test_add_comment_uses_official_v2_endpoint_with_sdk_signing(self):
        client = ProjectManClient.__new__(ProjectManClient)
        client.project_id = "a1b2c3d4e5f678901234567890abcdef"
        client._client = MagicMock()
        client._client.call_api.return_value.raw_content = b'{"status":"success"}'

        result = client.add_comment(71083121, "[AI处理结果]\n已修复")

        self.assertEqual(result["status"], "success")
        client._client.call_api.assert_called_once_with(
            "/v2/issues/update-issue-notes",
            "POST",
            header_params={"Content-Type": "application/json"},
            body={
                "id": 71083121,
                "notes": "[AI处理结果]\n已修复",
                "project_uuid": "a1b2c3d4e5f678901234567890abcdef",
                "type": "scrum",
            },
        )


if __name__ == "__main__":
    unittest.main()
