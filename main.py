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
import os
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
    parser.add_argument("--handlers", default=None, help="华为云处理人白名单（逗号分隔，覆盖环境变量 MULTICA_SYNC_HANDLERS）")
    parser.add_argument("--assignee", default=None, help="Multica 接收人名字或用户 ID（覆盖环境变量 MULTICA_ASSIGNEE_ID）")
    parser.add_argument("--hw-project", "--project-id", dest="hw_project", default=None, help="华为云 Project ID（覆盖环境变量 HW_PROJECT_ID）")
    parser.add_argument("--multica-project", default=None, help="Multica 归属项目名称或 ID（如 buyer-app-service, trade-system-backend）")
    parser.add_argument("--project-mapping", default=None, help="模块/系统到 Multica 项目与指派人的映射（JSON 或 key:project:assignee,...）")
    parser.add_argument("--test-branch", "--target-branch", dest="test_branch", default=None, help="目标测试合并分支名称（如 test-cloud，默认: test-cloud）")
    parser.add_argument("--auto-assign-agent", action="store_true", help="是否直接指派给对应智能体并立即触发任务（默认存入成员收件箱，不直接触发任务）")
    parser.add_argument("--resolve", type=int, default=None, help="将指定华为云缺陷 ID 标记为「已解决」(status_id=3)")
    parser.add_argument("--comment-file", default=None, help="在 --resolve 前把 UTF-8 文件内容作为 [AI处理结果] 评论写入华为云")
    parser.add_argument("--set-status", nargs=2, metavar=("ISSUE_ID", "STATUS_ID"), type=int, default=None, help="修改指定华为云缺陷状态")
    args = parser.parse_args()

    if args.comment_file and not args.resolve:
        parser.error("--comment-file 必须与 --resolve <BUG_ID> 一起使用")

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from codearts_triage.config import Config

    PROJECT_ROOT = Path(__file__).resolve().parent

    cfg = Config()
    if args.rules:
        cfg.rules_file = args.rules
    if args.hw_project:
        cfg.project_id = args.hw_project.strip()
        if not args.state:
            cfg.state_file = str(PROJECT_ROOT / f"state_{cfg.project_id}.json")
    if args.state:
        cfg.state_file = args.state
    elif not os.path.isabs(cfg.state_file):
        cfg.state_file = str(PROJECT_ROOT / cfg.state_file)
    if args.multica_project:
        cfg.multica_project = args.multica_project.strip()
    if args.project_mapping:
        from codearts_triage.multica_sync import parse_project_mapping
        cfg.multica_project_mapping = parse_project_mapping(args.project_mapping)
    if args.test_branch:
        cfg.test_branch = args.test_branch.strip()
    if args.handlers is not None:
        cfg.multica_sync_handlers = [h.strip() for h in args.handlers.split(",") if h.strip()]
    if args.assignee is not None:
        cfg.multica_assignee_id = args.assignee.strip()
    if args.auto_assign_agent:
        cfg.multica_auto_assign_agent = True

    if args.resolve:
        ak = cfg.ak_write or cfg.ak_read
        sk = cfg.sk_write or cfg.sk_read
        from codearts_triage.client import ProjectManClient
        from codearts_triage.completion import complete_issue
        from codearts_triage.state import State
        client = ProjectManClient(cfg.region, ak, sk, cfg.project_id)
        try:
            comment_text = None
            if args.comment_file:
                comment_text = Path(args.comment_file).read_text(encoding="utf-8")
            result = complete_issue(
                client,
                State(cfg.state_file),
                args.resolve,
                comment_text=comment_text,
            )
            if result["comment_written"]:
                print(f"成功向华为云 Bug #{args.resolve} 写入 [AI处理结果] 评论")
            elif result["comment_skipped"]:
                print(f"华为云 Bug #{args.resolve} 的相同 [AI处理结果] 已写入，跳过重复评论")
            print(f"成功将华为云 Bug #{args.resolve} 状态更新为「已解决」 (status_id=3)")
            return 0
        except Exception as e:
            print(f"更新华为云 Bug #{args.resolve} 状态失败: {e}", file=sys.stderr)
            return 1

    if args.set_status:
        issue_id, status_id = args.set_status
        ak = cfg.ak_write or cfg.ak_read
        sk = cfg.sk_write or cfg.sk_read
        from codearts_triage.client import ProjectManClient
        client = ProjectManClient(cfg.region, ak, sk, cfg.project_id)
        try:
            client.update_status(issue_id, status_id=status_id)
            print(f"成功将华为云 Bug #{issue_id} 状态更新为 status_id={status_id}")
            return 0
        except Exception as e:
            print(f"更新华为云 Bug #{issue_id} 状态失败: {e}", file=sys.stderr)
            return 1

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
