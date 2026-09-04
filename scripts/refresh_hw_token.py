#!/usr/bin/env python3
"""华为云 IAM Token 自动获取与刷新脚本。

用于由专属 Agent（如本地私有模型 Agent）通过环境变量 (custom_env) 定时执行，
请求华为云官方 IAM 接口换取最新的 X-Subject-Token，并安全写入项目的 .env 文件中。
"""

import argparse
import os
import re
import sys
from pathlib import Path

import requests


def refresh_token(
    domain: str,
    user: str,
    password: str,
    region: str = "cn-north-1",
    env_file_path: Path = None,
    quiet: bool = False,
) -> bool:
    if not domain or not user or not password:
        print("❌ 缺少必要的凭据信息：HW_IAM_DOMAIN, HW_IAM_USER, HW_IAM_PASSWORD 必须完整提供。", file=sys.stderr)
        return False

    url = f"https://iam.{region}.myhuaweicloud.com/v3/auth/tokens"
    payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": user,
                        "password": password,
                        "domain": {"name": domain},
                    }
                },
            },
            "scope": {"project": {"name": region}},
        }
    }

    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    except Exception as e:
        print(f"❌ 请求华为云 IAM 服务异常: {e}", file=sys.stderr)
        return False

    if resp.status_code not in (200, 201):
        print(f"❌ 获取 Token 失败 (HTTP {resp.status_code}): {resp.text}", file=sys.stderr)
        return False

    token = resp.headers.get("X-Subject-Token")
    if not token:
        print("❌ 响应头中缺少 X-Subject-Token", file=sys.stderr)
        return False

    if env_file_path:
        env_file_path = Path(env_file_path)
        if env_file_path.exists():
            content = env_file_path.read_text(encoding="utf-8")
            if "HW_AUTH_TOKEN=" in content:
                content = re.sub(r"HW_AUTH_TOKEN=.*", f"HW_AUTH_TOKEN={token}", content)
            else:
                content += f"\nHW_AUTH_TOKEN={token}\n"
            env_file_path.write_text(content, encoding="utf-8")
        else:
            env_file_path.write_text(f"HW_AUTH_TOKEN={token}\n", encoding="utf-8")
        if not quiet:
            print(f"✅ 成功刷新华为云 Token，并已安全更新至: {env_file_path}")
    else:
        if not quiet:
            print("✅ 成功获取华为云 Token（未指定 env_file_path，未写入文件）")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新华为云 IAM Token 并写入 .env 文件")
    parser.add_argument("--env-file", default=None, help=".env 文件路径（默认读取上级目录 .env）")
    parser.add_argument("--region", default=None, help="华为云区域（默认优先读取 HW_REGION，其次 cn-north-1）")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env_file = Path(args.env_file) if args.env_file else (repo_root / ".env")

    # 优先从进程环境变量读取（custom_env 注入的位置）
    domain = os.getenv("HW_IAM_DOMAIN", "").strip()
    user = os.getenv("HW_IAM_USER", "").strip()
    password = os.getenv("HW_IAM_PASSWORD", "").strip()
    region = args.region or os.getenv("HW_REGION", "cn-north-1").strip()

    # 如果环境变量没有，尝试从本地 .env 中读取备用
    if not (domain and user and password) and env_file.exists():
        try:
            from dotenv import dotenv_values
            env_vals = dotenv_values(env_file)
            domain = domain or env_vals.get("HW_IAM_DOMAIN", "").strip()
            user = user or env_vals.get("HW_IAM_USER", "").strip()
            password = password or env_vals.get("HW_IAM_PASSWORD", "").strip()
            region = region or env_vals.get("HW_REGION", "cn-north-1").strip()
        except ImportError:
            pass

    success = refresh_token(
        domain=domain,
        user=user,
        password=password,
        region=region,
        env_file_path=env_file,
        quiet=args.quiet,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
