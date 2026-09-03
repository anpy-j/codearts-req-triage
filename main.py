#!/usr/bin/env python3
"""CodeArts Req 自动化 Bug 分诊 — CLI 入口。

用法：
  python main.py --once --dry-run    # 只读分诊一轮，预览写回动作（无需写回凭据）
  python main.py --once              # 真实写回一轮（需 .env 配置 + 自定义字段）
  python main.py --loop              # 常驻轮询（默认每 5 分钟）
  python main.py --init-state        # 仅初始化状态文件（重置游标）

环境变量见 .env.example（AK/SK 一律环境变量注入，绝不入库）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# 使 src 可导入（未安装为包时）
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# 加载 .env（若存在；AK/SK 仍只从环境变量读取，.env 被 .gitignore 排除）
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def build_pipeline(cfg) -> tuple:
    from codearts_triage.client import ProjectManClient
    from codearts_triage.config import Config
    from codearts_triage.rules import Rules
    from codearts_triage.state import State
    from codearts_triage.triage import TriagePipeline

    if not cfg.has_read_credentials:
        raise SystemExit(
            "缺少只读凭据：请在 .env 设置 HW_PROJECT_ID / HW_AK_READ / HW_SK_READ（AK/SK 勿提交到 issue 或仓库）"
        )
    # 写回凭据：若开启写回但无写回 key，回退用只读 key 并告警（默认保守，仅在成员明确允许时）
    ak = cfg.ak_write or cfg.ak_read
    sk = cfg.sk_write or cfg.sk_read
    if cfg.writeback_enabled and not cfg.has_write_credentials:
        logging.warning("WRITEBACK_ENABLED=true 但未配置写回 key，将使用只读 key（仅写自定义字段/描述，需成员确认权限）")
    client = ProjectManClient(cfg.region, ak, sk, cfg.project_id)
    rules = Rules.load(cfg.rules_file)
    state = State(cfg.state_file)
    return TriagePipeline(client, cfg, rules, state), rules, state


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeArts Req 自动化 Bug 分诊")
    parser.add_argument("--once", action="store_true", help="只运行一轮")
    parser.add_argument("--loop", action="store_true", help="常驻轮询")
    parser.add_argument("--dry-run", action="store_true", help="只读分诊，不真实写回（预览动作）")
    parser.add_argument("--init-state", action="store_true", help="初始化状态文件后退出")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示 Bug 详细信息与分诊完整内容")
    parser.add_argument("--rules", default=None, help="规则文件路径（覆盖环境变量 RULES_FILE）")
    parser.add_argument("--state", default=None, help="状态文件路径（覆盖环境变量 STATE_FILE）")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from codearts_triage.config import Config

    cfg = Config()
    if args.rules:
        cfg.rules_file = args.rules
    if args.state:
        cfg.state_file = args.state

    if args.init_state:
        from codearts_triage.state import State

        State(cfg.state_file).save()
        print(f"state initialized at {cfg.state_file}")
        return 0

    pipeline, rules, state = build_pipeline(cfg)

    def run_once() -> None:
        summary = pipeline.run_once(dry_run=args.dry_run)
        mode = "dry-run" if args.dry_run else ("write-back" if cfg.writeback_enabled else "read-only")
        print(
            f"[{mode}] fetched={summary['fetched']} triaged={summary['triaged']} "
            f"skipped={summary['skipped']} written={summary['written']} "
            f"errors={summary['errors']} poisoned={summary['poisoned']}"
        )
        if pipeline.state.error_count() > 0:
            print(f"  ⚠ 状态文件仍有 {pipeline.state.error_count()} 条待重试错误（下轮自动重试）")
        if summary["poisoned"] > 0:
            print(f"  ⚠ 本轮有 {summary['poisoned']} 条连续失败已放弃自动重试，需人工排查（见 MAX_ERROR_ATTEMPTS）")
        for d in summary["details"]:
            title_str = f"「{d['title']}」" if d.get("title") else ""
            print(f"  issue {d['issue_id']} {title_str}: module={d['result'].get('module')} priority={d['result'].get('priority_suggestion')}")
            if d.get("result", {}).get("summary"):
                print(f"    分诊结论: {d['result']['summary']}")
            for a in d["actions"]:
                print(f"    - {a}")
            if args.verbose:
                if d.get("description"):
                    print(f"    原始描述: {d['description']}")
                import json
                print(f"    完整分诊数据: {json.dumps(d['result'], ensure_ascii=False)}")

    if args.loop:
        print(f"常驻轮询启动，间隔 {cfg.poll_interval_seconds}s（Ctrl+C 退出）")
        try:
            while True:
                run_once()
                time.sleep(cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            print("已停止")
            return 0
    else:
        run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
