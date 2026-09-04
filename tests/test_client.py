"""ProjectMan 客户端请求参数测试（不导入 SDK、不联网）。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        client.auth_token = ""
        client._client = MagicMock()
        client._client.call_api.return_value.raw_content = b'{"status":"success"}'

        result = client.add_comment(71083121, "[AI处理结果]\n已修复")

        self.assertEqual(result["status"], "success")
        client._client.call_api.assert_called_once_with(
            "/v2/issues/update-issue-notes",
            "POST",
            header_params={"Content-Type": "application/json"},
            body={
                "id": "71083121",
                "notes": "[AI处理结果]\n已修复",
                "project_uuid": "a1b2c3d4e5f678901234567890abcdef",
                "type": "scrum",
            },
        )

    @patch("requests.post")
    def test_add_comment_uses_token_without_sdk_signing(self, mock_post):
        mock_post.return_value.ok = True
        mock_post.return_value.content = b'{"status":"success"}'
        client = ProjectManClient.__new__(ProjectManClient)
        client.region = "cn-north-1"
        client.project_id = "a1b2c3d4e5f678901234567890abcdef"
        client.endpoint = "https://projectman-ext.cn-north-1.myhuaweicloud.com"
        client.auth_token = "iam-token"
        client._client = MagicMock()

        client.add_comment(7, "result")

        client._client.call_api.assert_not_called()
        mock_post.assert_called_once_with(
            "https://projectman-ext.cn-north-1.myhuaweicloud.com/v2/issues/update-issue-notes",
            headers={
                "Content-Type": "application/json",
                "X-Auth-Token": "iam-token",
            },
            json={
                "id": "7",
                "notes": "result",
                "project_uuid": "a1b2c3d4e5f678901234567890abcdef",
                "type": "scrum",
            },
            timeout=30,
        )

    @patch("requests.post")
    def test_add_comment_reports_response_body_without_request_headers(self, mock_post):
        mock_post.return_value.ok = False
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = '{"error_code":"DEV_21_400"}'

        client = ProjectManClient.__new__(ProjectManClient)
        client.region = "cn-north-1"
        client.project_id = "a1b2c3d4e5f678901234567890abcdef"
        client.endpoint = "https://projectman-ext.cn-north-1.myhuaweicloud.com"
        client.auth_token = "secret-token"
        client._client = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "DEV_21_400") as ctx:
            client.add_comment(7, "result")

        self.assertNotIn("secret-token", str(ctx.exception))

    @patch("requests.post")
    def test_add_comment_fetches_token_with_iam_credentials(self, mock_post):
        token_resp = MagicMock()
        token_resp.status_code = 201
        token_resp.headers = {"X-Subject-Token": "auto-fetched-token"}
        comment_resp = MagicMock()
        comment_resp.ok = True
        comment_resp.content = b'{"status":"success"}'
        mock_post.side_effect = [token_resp, comment_resp]

        client = ProjectManClient.__new__(ProjectManClient)
        client.region = "cn-north-1"
        client.project_id = "a1b2c3d4e5f678901234567890abcdef"
        client.endpoint = "https://projectman-ext.cn-north-1.myhuaweicloud.com"
        client.auth_token = ""
        client.iam_domain = "test-domain"
        client.iam_user = "test-user"
        client.iam_password = "test-password"
        client._client = MagicMock()

        client.add_comment(7, "result")

        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            mock_post.call_args_list[1].kwargs["headers"]["X-Auth-Token"],
            "auto-fetched-token",
        )
        client._client.call_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
