"""本地代码搜索测试：在临时 git 仓库中验证 git grep 与 git log -S。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codearts_triage.code_search import merge_code_hints, search_commits_in_repo, search_in_repo


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class CodeSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.repo = cls.tmpdir.name
        _git(cls.repo, "init", "-q")
        _git(cls.repo, "config", "user.email", "t@t.t")
        _git(cls.repo, "config", "user.name", "t")
        src = os.path.join(cls.repo, "src")
        os.makedirs(src)
        with open(os.path.join(src, "auth.py"), "w") as fh:
            fh.write("def login():\n    token = get_token()\n    return verify(token)\n")
        with open(os.path.join(src, "payment.py"), "w") as fh:
            fh.write("def pay():\n    amount = 0\n")
        _git(cls.repo, "add", ".")
        _git(cls.repo, "commit", "-q", "-m", "feat: login token verification")
        # 第二个提交引入 bug 关键词
        with open(os.path.join(src, "auth.py"), "a") as fh:
            fh.write("# TODO: crash on timeout\n")
        _git(cls.repo, "add", ".")
        _git(cls.repo, "commit", "-q", "-m", "fix: timeout crash")

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_search_in_repo_finds_file(self):
        hits = search_in_repo(self.repo, ["token"])
        self.assertTrue(hits)
        self.assertEqual(hits[0]["file"], "src/auth.py")

    def test_search_in_repo_empty_path(self):
        self.assertEqual(search_in_repo("", ["token"]), [])
        self.assertEqual(search_in_repo("/nonexistent", ["token"]), [])

    def test_search_in_repo_no_keywords(self):
        self.assertEqual(search_in_repo(self.repo, []), [])

    def test_search_commits_in_repo(self):
        commits = search_commits_in_repo(self.repo, ["crash"])
        self.assertTrue(commits)
        self.assertIn("fix: timeout crash", commits[0]["subject"])

    def test_merge_code_hints(self):
        hints = merge_code_hints(
            [{"file": "a.py", "line": "1"}],
            [{"commit_id": "abc", "commit_msg": "msg"}],
        )
        self.assertEqual(hints[0]["kind"], "file")
        self.assertEqual(hints[1]["kind"], "commit")

    def test_missing_git_binary_returns_empty(self):
        import codearts_triage.code_search as cs

        orig = cs.shutil.which
        try:
            cs.shutil.which = lambda name: None  # 模拟无 git
            self.assertEqual(search_in_repo(self.repo, ["token"]), [])
            self.assertEqual(search_commits_in_repo(self.repo, ["crash"]), [])
        finally:
            cs.shutil.which = orig

    def test_git_timeout_returns_empty(self):
        import subprocess

        import codearts_triage.code_search as cs

        orig = cs.subprocess.run
        try:
            def boom(*args, **kwargs):
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

            cs.subprocess.run = boom
            self.assertEqual(search_in_repo(self.repo, ["token"]), [])
            self.assertEqual(search_commits_in_repo(self.repo, ["crash"]), [])
        finally:
            cs.subprocess.run = orig

    def test_git_oserror_returns_empty(self):
        import codearts_triage.code_search as cs

        orig = cs.subprocess.run
        try:
            def boom(*args, **kwargs):
                raise OSError("no such file")

            cs.subprocess.run = boom
            self.assertEqual(search_in_repo(self.repo, ["token"]), [])
        finally:
            cs.subprocess.run = orig


if __name__ == "__main__":
    unittest.main()
