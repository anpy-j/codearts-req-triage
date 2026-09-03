"""ProjectMan 客户端请求参数测试（不导入 SDK、不联网）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.client import _associated_commits_request_kwargs


class ProjectManClientRequestTest(unittest.TestCase):
    def test_associated_commits_requires_commit_type(self):
        kwargs = _associated_commits_request_kwargs("project-id", 123, 50, 0)

        self.assertEqual(kwargs["type"], "commit")
        self.assertEqual(kwargs["project_id"], "project-id")
        self.assertEqual(kwargs["issue_id"], 123)
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["offset"], 0)


if __name__ == "__main__":
    unittest.main()
