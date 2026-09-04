# CodeArts Req 自动化 Bug 分诊 — 轮询 MVP

华为云 CodeArts Req（ProjectMan OpenAPI）缺陷的自动化分诊：增量轮询新缺陷 → AI 分诊（模块分类 / 优先级建议 / 负责人建议 / 代码定位）→ 三层多平台智能路由（如 商城业务平台 / 买家端App / 运营管理后台）→ Multica 看板建单与智能体指派 → 修复完成后状态闭环回调。

- 完整集成指南：[`docs/Multica与华为云CodeArts缺陷自动化分诊与双向流转集成指南.md`](docs/Multica与华为云CodeArts缺陷自动化分诊与双向流转集成指南.md)
- 配套集成方案：[`docs/INTEGRATION_PLAN.md`](docs/INTEGRATION_PLAN.md)（含触发验证清单、架构、API 清单、安全规范）

## 技术选型

**Python + 官方 SDK `huaweicloudsdkprojectman`**（而非官方 MCP server）。理由：
- SDK 直接在代码内调用，无额外运行时依赖，便于本地跑、定时任务、CI；
- mock 测试无需凭据即可跑通（本项目测试不联网、不依赖真实账号）；
- 官方 MCP server 需额外部署且工具集为 API 子集，SDK 覆盖更全、可控性更好。

## 目录结构

```
codearts-req-triage/
├── docs/INTEGRATION_PLAN.md      # 阶段一集成方案
├── main.py                       # CLI 入口：--once / --loop / --dry-run
├── rules.example.yaml            # 分诊规则样例（复制为 rules.yaml 使用）
├── requirements.txt
├── .env.example                  # 环境变量样例（复制为 .env）
└── src/codearts_triage/
    ├── config.py                 # 环境变量配置
    ├── client.py                 # ProjectMan SDK 封装（可 mock）
    ├── state.py                  # 增量游标 + 去重 + 错误队列（JSON 状态文件）
    ├── rules.py                  # 分诊规则加载与执行（模块/优先级/负责人/关键词）
    ├── code_search.py            # 代码定位：本地 clone git grep + 关联提交
    ├── triage.py                 # 编排：拉取 → 分诊 → 写回
    └── writeback.py              # 保守写回：自定义字段 + 描述追加 + 可选自动改字段
└── tests/                        # mock 测试（无凭据可跑通）
```

## 快速开始

### 1. 安装

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
cp rules.example.yaml rules.yaml
# 编辑 .env：填 HW_PROJECT_ID、HW_AK_READ、HW_SK_READ（至少只读 key）
```

**安全约定**：AK/SK 一律环境变量注入，绝不写入 issue 正文、文档或提交历史。

### 3. 无凭据验证（mock）

```bash
python -m unittest discover -s tests -v
```

### 4. 运行

```bash
# 只跑一轮（推荐先 --dry-run 预览写回内容）
python main.py --once --dry-run
# 真实写回一轮（需 .env 配置写回 key + 自定义字段已建好）
python main.py --once
# 常驻轮询（默认每 5 分钟，可用 POLL_INTERVAL_SECONDS 调整）
python main.py --loop
```

### 修复完成后回传评论并关闭 Bug

先把面向测试人员的交付说明写入 UTF-8 文件，再执行同一条闭环命令：

```bash
python main.py --hw-project <HW_PROJECT_ID> --resolve <BUG_ID> --comment-file ./codearts_reply.md
```

程序会自动添加 `[AI处理结果]` 标记并脱敏常见 Authorization、Cookie、Token、AK/SK、密码；同一内容按 hash 判重。执行顺序固定为“评论成功 → 状态改为已解决 → 更新评论水位”，评论失败不会提前关闭 Bug。SQL 类交付应包含只读 SQL、目标数据库、用途和注意事项。

## 测试打回后续跑原任务

CodeArts Bug 与 Multica Issue 按 Bug ID 一对一绑定。巡检在创建前会查询包含已关闭任务在内的历史记录，因此同一个 Bug 不会重复建单。

- 测试把 CodeArts 状态从「已解决」改回「新建」或「进行中」：下一轮巡检会在原 Multica Issue 追加打回通知，并通过 `multica issue rerun` 续跑原智能体任务。
- 不修改状态：请在 CodeArts Bug 下新增一条评论，例如“测试未通过：仍可复现，步骤……”。巡检通过最新评论 ID 识别新反馈，同样续跑原任务。
- 既不修改状态、正文，也不新增评论时，轮询端没有可观察到的新事件，因此不会重复触发。
- 若原 Multica Issue 已在运行中，只追加最新反馈，不再并发启动第二个 run。

执行 `--resolve --comment-file` 后，程序会保存自身评论及状态快照；带 `[AI处理结果]` 的系统评论只推进水位，不会反向触发原任务。

## 默认保守写回

- 开启 `WRITEBACK_ENABLED=true` 后，写回仅做两件事（与方案 §0 一致）：
  1. 把分诊结论 JSON 写入 Req 项目自定义字段（字段名由 `TRIAGE_FIELD_NAME` 指定，需先在控制台建好）；
  2. 在 issue 描述末尾追加带 `<!-- AI-TRIAGE -->` 标记的分诊摘要。
- **自动改严重级/优先级/模块/负责人默认关闭**（`AUTO_CHANGE_*` 全部默认 false），需成员逐项开启并确认 id 映射后才生效。
- 重复运行幂等：同一条 issue 若分诊结果 hash 未变则跳过写回。
- **只读/预览模式（`WRITEBACK_ENABLED=false` 或 `--dry-run`）完全不写状态文件**（游标/去重不更新），因此：
  - 预览后接真实运行，真实运行仍会完整分诊并写回（预览不会污染进度）；
  - `--loop` 常驻轮询只在 `WRITEBACK_ENABLED=true` 时有意义，只读模式下每轮都会重新分诊全部命中条目。

## 真实联调还需要什么

见 [docs/INTEGRATION_PLAN.md §8](docs/INTEGRATION_PLAN.md)：项目 ID、只读 AK/SK、「AI 分诊」自定义字段；可选：写回 AK/SK、自动改字段授权、负责人 id 映射、webhook/标签可写性确认。

## 测试说明

`tests/` 使用内存 fake client 与临时 git 仓库，**不联网、不需要华为云凭据**，覆盖：规则引擎、状态游标与幂等、分诊编排、保守写回、本地代码搜索。
