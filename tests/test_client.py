"""ProjectMan 客户端请求参数测试（不导入 SDK、不联网）。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import (
    ProjectManClient,
    _associated_commits_request_kwargs,
    _issue_notes_body,
)


class ProjectManClientRequestTest(unittest.TestCase):
    def test_associated_commits_requires_commit_type(self):
        kwargs = _associated_commits_request_kwargs("project-id", 123, 50, 0)

        self.assertEqual(kwargs["type"], "commit")
        self.assertEqual(kwargs["project_id"], "project-id")
        self.assertEqual(kwargs["issue_id"], 123)
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["offset"], 0)

    def test_issue_notes_body_matches_working_codearts_contract(self):
        body = _issue_notes_body("project-id", 7, "修复完成\nSELECT a < b;")

        self.assertEqual(body["id"], 7)
        self.assertEqual(body["innerText"], "修复完成\nSELECT a < b;")
        self.assertEqual(body["projectUUId"], "project-id")
        self.assertEqual(body["type"], "scrum")
        self.assertEqual(
            body["notes"],
            "%3Cp%3E%E4%BF%AE%E5%A4%8D%E5%AE%8C%E6%88%90%3Cbr%3ESELECT%20a%20%26lt%3B%20b%3B%3C%2Fp%3E",
        )

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
                "id": 71083121,
                "innerText": "[AI处理结果]\n已修复",
                "notes": "%3Cp%3E%5BAI%E5%A4%84%E7%90%86%E7%BB%93%E6%9E%9C%5D%3Cbr%3E%E5%B7%B2%E4%BF%AE%E5%A4%8D%3C%2Fp%3E",
                "projectUUId": "a1b2c3d4e5f678901234567890abcdef",
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
                "id": 7,
                "innerText": "result",
                "notes": "%3Cp%3Eresult%3C%2Fp%3E",
                "projectUUId": "a1b2c3d4e5f678901234567890abcdef",
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
