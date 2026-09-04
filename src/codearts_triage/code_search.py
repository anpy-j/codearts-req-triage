"""代码定位：在本地代码库 clone 中按关键词搜索相关文件/提交；另支持 API 关联提交。

本地搜索用 git（git grep + git log -S），无需额外凭据。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

MAX_FILE_HITS = 5
MAX_COMMIT_HITS = 5


def _run_git(repo_path: str, args: list[str], timeout: int = 30) -> tuple[int, str]:
    if not shutil.which("git"):
        return 1, ""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git command failed in %s (%s)", repo_path, exc)
        return 1, ""
    return proc.returncode, proc.stdout


def search_in_repo(repo_path: str, keywords: list[str]) -> list[dict]:
    """在本地 clone 中按关键词 grep，返回命中文件/行。repo_path 为空或不可用时返回 []。"""
    if not repo_path or not os.path.isdir(repo_path) or not keywords:
        return []
    hits: list[dict] = []
    # git grep -n -i -e kw1 -e kw2（关键词较多时分组，避免命令行过长）
    batch: list[str] = []
    for kw in keywords:
        batch.append(kw)
        if len(batch) >= 20:
            hits.extend(_grep_batch(repo_path, batch))
            batch = []
    if batch:
        hits.extend(_grep_batch(repo_path, batch))
    return hits[:MAX_FILE_HITS]


def _grep_batch(repo_path: str, keywords: list[str]) -> list[dict]:
    args = ["grep", "-n", "-i"]
    for kw in keywords:
        args += ["-e", kw]
    args += ["--"]
    code, out = _run_git(repo_path, args)
    if code != 0 or not out:
        return []
    hits = []
    for line in out.splitlines():
        # 格式 path:line:content
        parts = line.split(":", 2)
        if len(parts) >= 2:
            hits.append({"file": parts[0], "line": parts[1], "snippet": parts[2][:120] if len(parts) > 2 else ""})
    return hits


def search_commits_in_repo(repo_path: str, keywords: list[str]) -> list[dict]:
    """git log -S<keyword> 定位引入/删除关键词的提交。"""
    if not repo_path or not os.path.isdir(repo_path) or not keywords:
        return []
    commits: list[dict] = []
    for kw in keywords[:3]:  # 只查前 3 个关键词，控制成本
        code, out = _run_git(repo_path, ["log", "--oneline", "-n", "5", f"-S{kw}"])
        if code != 0 or not out:
            continue
        for line in out.splitlines():
            parts = line.split(" ", 1)
            commits.append({"commit": parts[0], "subject": parts[1] if len(parts) > 1 else "", "keyword": kw})
    return commits[:MAX_COMMIT_HITS]


def merge_code_hints(local_hits: list[dict], api_commits: list[dict]) -> list[dict]:
    """合并本地文件命中与 API 关联提交，本地优先。"""
    hints: list[dict] = []
    for h in local_hits:
        hints.append({"kind": "file", **h})
    for c in api_commits:
        hints.append({"kind": "commit", **c})
    return hints


def extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
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
