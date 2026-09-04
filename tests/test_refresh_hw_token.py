import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.refresh_hw_token import refresh_token


class RefreshHwTokenTest(unittest.TestCase):
    @patch("requests.post")
    def test_refresh_token_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.headers = {"X-Subject-Token": "test-new-token-12345"}
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("HW_AUTH_TOKEN=old_token\nHW_PROJECT_ID=abc\n", encoding="utf-8")

            success = refresh_token(
                domain="test-domain",
                user="test-user",
                password="test-password",
                region="cn-north-1",
                env_file_path=env_file,
                quiet=True,
            )

            self.assertTrue(success)
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("HW_AUTH_TOKEN=test-new-token-12345", content)
            self.assertIn("HW_PROJECT_ID=abc", content)

    @patch("requests.post")
    def test_refresh_token_handles_auth_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("HW_AUTH_TOKEN=old_token\n", encoding="utf-8")

            success = refresh_token(
                domain="test-domain",
                user="test-user",
                password="wrong-password",
                region="cn-north-1",
                env_file_path=env_file,
                quiet=True,
            )

            self.assertFalse(success)
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("HW_AUTH_TOKEN=old_token", content)


if __name__ == "__main__":
    unittest.main()
