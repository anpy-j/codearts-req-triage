# CodeArts Req 自动化 Bug 分诊 — 集成方案（阶段一）

> 本方案基于 2026-08 已完成的对华为云 CodeArts Req（ProjectMan OpenAPI）的预调研与二次验证。
> 文档用途：作为阶段二轮询 MVP 的落地依据，并给出需要成员在控制台人工确认的清单。

---

## 0. 验证结论摘要（相对预调研的更新）

预调研中「写回 = `UpdateIssueV4` 打标签 + `ListIssueCommentsV4` 写分诊评论」的假设，经对官方 Python SDK（`huaweicloudsdkprojectman` 3.1.210）、Java/Go SDK、官方 MCP server 工具清单（`huaweicloud-samples/iaas-mcp-server` OpenAPI catalog）以及 API Explorer 在线检索的交叉验证，**需要修正**：

| 预调研假设 | 验证结论 | 影响 |
|---|---|---|
| `ListIssuesV4` 可增量拉取工作项 | ✅ 确证。`POST /v4/projects/{project_id}/issues`，支持 `created_time_interval` / `updated_time_interval` 时间窗口过滤、`tracker_ids`、`severity_ids`、`status_ids`、分页 `offset`/`limit` | 轮询方案成立 |
| `ShowIssueV4` 可查详情 | ✅ 确证。`GET /v4/projects/{project_id}/issues/{issue_id}`，含 `description` 等完整字段 | 分诊输入成立 |
| `UpdateIssueV4` 可改优先级/模块/负责人 | ✅ 确证。请求体 `IssueRequestV4` 含 `priority_id`、`module_id`、`assigned_id`、`severity_id`、`status_id`、`name`、`description`、`new_custom_fields` | 写回字段成立 |
| `UpdateIssueV4` 可「打标签」 | ⚠️ **修正**。`IssueRequestV4` 无 `tags`/`label` 字段；标签在响应 `Workitems.tags` 中可读，但**当前 OpenAPI 无写标签端点**（API Explorer 检索无任何 ProjectMan 标签写接口） | 标签写回降级为「自定义字段」或「描述追加」，见 §5 |
| `ListIssueCommentsV4` 可「写评论」 | ⚠️ **修正**。该 V4 接口本身仍为只读；华为云 2026-08 更新的官方 API 已新增 `AddIssueNotes`：`POST /v2/issues/update-issue-notes`。当前 Python SDK 尚未生成便捷方法，MVP 通过 SDK Core 的 AK/SK 签名请求封装 | 修复交付结果可写入真实评论；分诊主通道仍保留自定义字段 |
| webhook（服务钩子）可用 | ❓ 未确证。华为云文档站反爬，需成员在控制台确认（见 §1） | 阶段二默认走轮询，webhook 为后续增强 |

**结论**：阶段二 MVP 的写回策略调整为「**保守三通道**」——
1. 必做：`UpdateIssueV4` 写入**自定义字段**（如「AI 分诊」字段，需成员先在 Req 项目设置里建好，见 §6）；
2. 可选（默认关）：`UpdateIssueV4` 改 `severity_id` / `priority_id` / `module_id` / `assigned_id`（需成员明确授权，见 §7 规则）；
3. 信息记录：分诊阶段继续在 description 追加带标记摘要；修复交付阶段通过 `AddIssueNotes` 写入带 `[AI处理结果]` 标记的评论，并在评论成功后更新状态。

---

## 1. 触发方式验证清单（成员在控制台逐项确认）

> 「谁来确认」统一为**成员（Mika 无法登录华为云控制台）**；「确认结果填哪里」为下方表格右侧 + 本 issue 评论回复。

| # | 要确认的事项 | 具体操作路径 | 为什么需要 | 确认结果填写位置 |
|---|---|---|---|---|
| 1 | 项目里是否存在「服务钩子」/ webhook 入口 | CodeArts Req 控制台 → 项目 → 项目设置 → 服务钩子（旧称） | 若可用，可把「轮询」升级为「事件触发」，实时性更好 | 本表第 1 行 + issue 评论 |
| 2 | 服务钩子支持的事件类型 | 同上入口，查看可勾选事件列表（是否有「工作项创建/更新」） | 决定 webhook 方案的 payload 时机 | 本表第 2 行 |
| 3 | 服务钩子的鉴权/签名方式 | 服务钩子配置页，查看是否有 secret/token、签名算法说明 | 决定 webhook 接收端如何验签，防伪造 | 本表第 3 行 |
| 4 | 服务钩子 payload 格式示例 | 配置页通常提供「测试发送」或文档样例 | 决定接收端解析字段（工作项 ID 等） | 本表第 4 行 |
| 5 | Req 项目自定义字段能力 | 项目设置 → 工作项设置 → 自定义字段（新建「AI 分诊」多行文本字段） | MVP 写回依赖自定义字段（§0 结论） | 本表第 5 行 + §6 是否已建好 |
| 6 | Req 中「标签」是否为内置字段 + 是否可从 API 写 | 工作项详情页看「标签」字段；API Explorer 搜 ProjectMan 标签接口 | 若标签可写则恢复「打标签」方案 | 本表第 6 行 |
| 7 | 项目 ID（devcloud 项目的 32 位 id） | 项目设置 → 基本信息，或 Req 控制台 URL 中 `project_id` 参数 | MVP 所有 API 调用的路径参数 | §6 表格，写入 `.env`（不进 issue 正文） |
| 8 | 缺陷工作项的 `tracker_id` 值 | Req 控制台建一个 Bug，用 `ShowIssueV4` 或项目工作项类型设置确认（预期 3=Bug） | 轮询过滤 `tracker_ids` 用 | 本表第 8 行 |

> **本轮 MVP 不依赖 webhook**（第 1–4 项为「确认后可用则升级」项，不阻塞 MVP）。阻塞项只有：第 5 项（建自定义字段）与第 7 项（项目 ID）+ AK/SK。

---

## 2. 架构图与数据流

```
┌─────────────┐   定时(默认每 5 分钟)   ┌──────────────────────────────┐
│ 轮询进程 MVP │ ────────────────────► │ CodeArts Req (ProjectMan API) │
│ (Python)     │                       │   ListIssuesV4(增量时间窗口)  │
└──────┬──────┘                       └──────────────┬───────────────┘
       │                                             │ 返回缺陷列表(含 id/标题/severity/updated_time)
       ▼                                             │
┌─────────────┐   ShowIssueV4(单条详情)             │
│ 去重 & 增量  │ ───────────────────────────────────►│ 完整 description / 严重级 / 模块
│ 游标(State)  │◄───────────────────────────────────┤
└──────┬──────┘   description + severity + title     │
       ▼                                             │
┌──────────────────────┐                             │
│ AI 分诊(规则引擎)     │──► 分类(模块) / 优先级建议 / 负责人建议 / 关键词
│ + 可选 LLM 提示词      │
└──────┬───────────────┘
       │ 命中关键词
       ▼
┌──────────────────────┐   本地代码库 clone(可选)    ┌─────────────────┐
│ 代码定位(可选)        │ ──git grep / git log -S──► │ CodeArts Repo   │
│ 关联提交/文件          │                            │ (本地镜像或API)  │
└──────┬───────────────┘                            └─────────────────┘
       │
       ▼  保守写回(§0 三通道)
┌──────────────────────┐   UpdateIssueV4
│ 写回 WorkItem         │──► ① 自定义字段「AI 分诊」= 分诊结论 JSON
│ (只读→写回两把 key)    │──► ② (可选授权) severity/priority/module/assigned
│                       │──► ③ description 末尾追加分诊摘要(带标记)
└──────────────────────┘
```

**关键设计点：**

- **增量游标**：state 文件记录 `last_updated_time`（`年-月-日 时:分:秒`，与 API 返回格式一致）。每轮将游标转换为 Unix 毫秒时间戳后请求 `ListIssuesV4` 的 `updated_time_interval=[last_cursor, now]`，时间窗口向前重叠 60 秒（防边界丢失），取回后按 `id` 去重。
- **去重**：`processed_issue_ids` 集合 + 每条的 `updated_time`。已处理且未更新 → 跳过；已处理但 `updated_time` 变化 → 重新分诊（幂等：写回前先读已存 hash，结论未变则跳过写回但刷新 `updated_time`，避免每轮重复分诊）。
- **重试**：单条失败指数退避重试 3 次（1s/2s/4s）；本轮存在**待重试**失败时游标整体不推进，失败条目保留在窗口内下轮重试（重复处理幂等无害）。单条连续失败达到 `MAX_ERROR_ATTEMPTS`（默认 5）次后转入 poisoned（放弃自动重试、不再阻塞游标）；连续多轮未再出现的**悬空错误**（条目被删除/分页不一致）同样自动转 poisoned，避免永久阻塞游标。CLI 以 ⚠ 提示需人工排查。写回失败同样记录并重试。
- **幂等**：写回以「分诊结论 hash（不含时间戳）与状态文件一致」为判据；重复运行同一条不产生重复写。
- **只读优先**：轮询/详情用只读子账号 key；写回用最小权限子账号 key（§4）。
- **时间**：统一 UTC；`created_time_interval`/`updated_time_interval` 格式为 Unix 毫秒时间戳 `开始,结束`（逗号分隔）。

---

## 3. API 清单（以 API Explorer 实际为准，2026-08 核验）

> 路径/方法来自官方 SDK 源码与 MCP OpenAPI 目录交叉确认；字段为关键子集，完整字段以 API Explorer 为准。

### 3.1 ListIssuesV4 — 高级查询工作项
- 方法/路径：`POST /v4/projects/{project_id}/issues`
- 关键请求体参数（`ListIssueRequestV4`）：

| 参数 | 类型 | 说明 |
|---|---|---|
| `tracker_ids` | list[int] | 工作项类型：**3=缺陷/Bug**，2=Task，5=Epic，6=Feature，7=Story |
| `created_time_interval` | str | Unix 毫秒时间戳区间 `"开始,结束"` |
| `updated_time_interval` | str | Unix 毫秒时间戳区间 `"开始,结束"`（**轮询游标用**） |
| `severity_ids` / `priority_ids` / `status_ids` / `module_ids` | list[int] | 可选过滤 |
| `limit` / `offset` | int | 分页（limit 建议 100） |
| `include_deleted` | bool | 默认 false |

- 关键返回字段（`ListIssueItemResponse`）：`id`(int)、`name`(标题)、`severity{id,name}`、`priority{id,name}`、`module{id,name}`、`status{id,name}`、`tracker`、`created_time`、`updated_time`、`assigned_user{user_id,num_id,id,name}`。

### 3.2 ShowIssueV4 — 查询工作项详情
- 方法/路径：`GET /v4/projects/{project_id}/issues/{issue_id}`
- 路径参数：`project_id`、`issue_id`
- 关键返回：`description`（分诊主要输入）、`name`、`severity`、`priority`、`module`、`status`、`tracker`、`assigned_user`、`updated_time`。

### 3.3 UpdateIssueV4 — 更新工作项（写回）
- 方法/路径：`PUT /v4/projects/{project_id}/issues/{issue_id}`
- 请求体（`IssueRequestV4`）关键字段：

| 字段 | 说明 |
|---|---|
| `priority_id` / `severity_id` | 优先级 / 重要程度 id（自动改需授权，默认关） |
| `module_id` | 模块 id（自动改需授权，默认关） |
| `assigned_id` | 负责人（数字 id，自动改需授权，默认关） |
| `name` / `description` | 标题 / 描述（描述追加分诊摘要用） |
| `new_custom_fields` | `[{custom_field, field_name, value}]` — **写「AI 分诊」自定义字段的主通道** |

> ⚠️ 无 `tags` 字段 → 标签写回需成员确认是否有其他端点，否则用自定义字段代替（§0）。

### 3.4 ListIssueCommentsV4 — 获取工作项评论列表
- 方法/路径：`GET /v4/projects/{project_id}/issues/{issue_id}/comments`
- 返回：`comments[{id, comment, created_time, user}]`。用途：人工反馈水位与评论写入后的幂等确认；该 GET 接口本身不负责写评论。

### 3.5 AddIssueNotes — 添加工作项评论
- 方法/路径：`POST /v2/issues/update-issue-notes`
- 请求体：`{id, project_uuid, notes, type: "scrum"}`。
- 用途：智能体修复完成后回传根因、修改内容、提交/PR、测试结果、复测方式及只读 SQL（如有）。评论统一带 `[AI处理结果]`，先脱敏、再按内容 hash 判重；成功后才把 Bug 更新为已解决。
- SDK 现状：官方 Python 包当前未生成对应方法，使用 SDK Core 的现有凭据签名及 HTTP 异常处理能力调用。

### 3.6 ListIssueAssociatedCommits — 查询关联提交（代码定位辅助）
- 方法/路径：`GET /v4/projects/{project_id}/issues/{issue_id}/associated-commits?type=commit`（`type` 必填，可取 `commit` / `branch`；MVP 查询提交记录）
- 返回：`commits[{repository_id, commit_id, commit_short_id, commit_msg, commit_url, branch_name, user}]`。
- 用途：缺陷若已关联代码提交，可直接给出 commit 线索；未关联时用本地 clone `git log -S` 关键词定位（§2 可选模块）。

---

## 4. 认证与安全配置

- **AK/SK 一律环境变量注入**：`.env`（gitignore）/ CI Secret / 部署环境变量；**永不写入 issue 正文、README、文档、提交历史**。
- **两把 key 分离**：
  - `HW_AK_READ` / `HW_SK_READ`：只读子账号（IAM 策略仅 `ProjectMan:ProjectMember:List` + 查看权限 + `CodeArts Repo:ReadOnly`）。
  - `HW_AK_WRITE` / `HW_SK_WRITE`：最小权限写子账号（仅目标项目 `ProjectMan:WorkItem:Update` 等必要权限点，见 §6 权限表）。
  - MVP 若成员只提供一把 key，则默认 `WRITEBACK_ENABLED=false`（只读分诊、dry-run 输出），确认后再开写。
- **最小权限 IAM 权限点**（成员在 IAM 控制台按需授予）：
  - 读：`projectman:workItem:get` / `projectman:workItem:list`（或角色「项目成员-只读」）；
  - 写：`projectman:workItem:update`；
  - 仓库读：`codeartsrepo:repository:get`（本地 clone 场景可不授）。
- **传输**：SDK 默认 HTTPS；不打印/不记录响应中的敏感字段。
- **失败安全**：写回失败不吞异常，记录 error 队列；任何异常不导致游标跳过未处理条目。

---

## 5. 分诊规则与提示词

### 5.1 分类维度（模块/组件）

关键词 → 模块映射表（`rules.yaml` 可配置，示例）：

| 模块 | 关键词（标题+描述命中任一） |
|---|---|
| auth | 登录, 登出, 认证, 鉴权, token, 权限不足, 403, session, 密码 |
| payment | 支付, 订单, 退款, 金额, 余额, 发票, 交易 |
| order | 下单, 购物车, 结算, 库存, 商品, SKU |
| message | 通知, 短信, 邮件, 推送, 消息, 站内信 |
| data | 报表, 导出, 统计, 图表, 数据不一致, 查询慢 |
| upload | 上传, 附件, 图片, 文件, OBS, 存储 |
| api | 接口, API, 超时, 500, 502, 504, 网关, 报错 |
| ui | 页面, 样式, 前端, 白屏, 布局, 点击无反应, 显示异常 |
| other | （兜底） |

### 5.2 优先级建议规则（建议值，默认不自动写）

规则引擎按「严重级 + 影响面 + 关键词」给出 `suggested_priority`（1-5，对应 Req 优先级 id，需成员确认 id 映射）：

```
P0(紧急): 致命级 且 (崩溃|数据丢失|安全漏洞|全部用户不可用|资金)
P1(高):   严重级 且 (核心功能不可用|大面积报错|性能严重劣化)
P2(中):   一般级 或 (部分用户受影响|绕行可用)
P3(低):   轻微级 或 纯 UI/文案
P4(建议): 优化/体验类
```

### 5.3 负责人建议映射表

`rules.yaml` 配置 `module -> suggested_assignee`（成员提供姓名→Req 用户数字 id 的映射）。MVP 只在分诊结论中**建议**，不自动 `assigned_id`（除非成员开启 `AUTO_ASSIGN=true`）。

### 5.4 分诊输出格式

**① 自定义字段「AI 分诊」内容（JSON，一行）**：
```json
{"module":"auth","priority_suggestion":"P1","severity_suggestion":null,
 "assignee_suggestion":"张三","keywords":["登录","token"],
 "code_hints":[{"repo":"backend","file":"src/auth/login.py","line":42}],
 "summary":"登录接口在高并发下偶发 500：认证服务 token 校验超时",
 "triaged_at":"2026-08-18T08:00:00Z","rule_version":"1.0"}
```

**② description 追加摘要（带标记，可回滚）**：
```
<!-- AI-TRIAGE: 2026-08-18T08:00:00Z -->
## 🤖 AI 分诊摘要
- 模块：auth
- 优先级建议：P1
- 负责人建议：张三
- 代码线索：backend/src/auth/login.py:42（关键词：token）
- 说明：本摘要由自动化分诊生成，仅供参考；规则见 INTEGRATION_PLAN §5。
<!-- /AI-TRIAGE -->
```

**③ 标签规范（若成员确认标签可写后启用）**：
`triaged`、`module:auth`、`sev:P1`、`priority:P1`。未确认前不写标签。

### 5.5 LLM 提示词（可选扩展）

若成员提供 LLM API（不产生额外华为云费用），可把 5.1–5.3 规则作为 system prompt，正文作为 user input 生成结构化 JSON；默认 MVP 用纯规则引擎（零成本、可测、确定）。

---

## 6. 华为云侧准备清单（成员操作）

| # | 事项 | 操作路径 | 结果交付 |
|---|---|---|---|
| 1 | 获取项目 ID（32 位） | Req 控制台 URL `.../projectman/projects/{project_id}/...` 或项目设置 | 填入 `.env` 的 `HW_PROJECT_ID` |
| 2 | 建只读子账号 + AK/SK | IAM → 用户 → 创建用户（编程访问）→ 授权（项目只读角色） | `HW_AK_READ`/`HW_SK_READ` 环境变量 |
| 3 | 建最小写权限子账号 + AK/SK | IAM → 用户 → 创建用户 → 自定义策略（仅目标项目 workItem:update） | `HW_AK_WRITE`/`HW_SK_WRITE` |
| 4 | 建「AI 分诊」自定义字段 | Req → 项目设置 → 工作项设置 → 自定义字段 → 新建（多行文本） | 字段名（如 `AI分诊`）填入 `.env` `TRIAGE_FIELD_NAME` |
| 5 | 确认缺陷 tracker_id（预期 3） | 见 §1 第 8 项 | 填 `.env` `TRACKER_IDS=3` |
| 6 | 确认服务钩子/webhook 可用性 | 见 §1 第 1–4 项 | issue 评论回复 |
| 7 | 确认标签可写性 | 见 §1 第 6 项 | issue 评论回复 |
| 8 | 提供负责人姓名→数字 id 映射 | Req 项目成员列表（API `ListProjectMembersV4` 可查） | 填 `rules.yaml` assignee 映射 |

---

## 7. 阶段二实现计划（任务拆分）

| 任务 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| T1 骨架+配置 | 项目结构、`.env.example`、`config.py`、logging | 无 | 无凭据可 `--help` 运行 |
| T2 只读客户端 | `client.py`：ListIssuesV4 增量 + ShowIssueV4 详情（只读 key） | T1 | mock 测试通过 |
| T3 状态与去重 | `state.py`：游标、processed ids、error 队列、幂等 | T1 | 单测覆盖重跑幂等 |
| T4 分诊引擎 | `rules.py` + `triage.py`：模块/优先级/负责人/关键词 | T1 | 规则单测 + 样例 issue 测试 |
| T5 代码定位 | `code_search.py`：本地 clone git grep + associated-commits | T4 | 有/无 clone 两态测试 |
| T6 保守写回 | `writeback.py`：自定义字段 + description 追加 + 可选自动改字段（默认关） | T4 | mock 写回测试 + 幂等测试 |
| T7 主循环/CLI | `main.py`：`--once` / `--loop` / `--dry-run` | T2–T6 | 端到端 mock 跑通 |
| T8 文档与交付 | README（配置/启动/验证）、`.env.example`、mock 测试、本地 git 提交 | 全部 | 本 issue 完成标准 |

**默认保守配置**（`WRITEBACK_ENABLED=true` 时也仅写自定义字段+描述；`AUTO_CHANGE_SEVERITY/PRIORITY/MODULE/ASSIGNEE` 全部默认 `false`，需成员逐项开启）。

---

## 8. 需要成员提供（按优先级）

1. **项目 ID**（§6-1）
2. **只读 AK/SK**（§6-2，至少一把；注入环境变量，勿发 issue）
3. **「AI 分诊」自定义字段已建好 + 字段名**（§6-4）
4. 控制台确认：服务钩子是否存在（§1-1~4）——不阻塞 MVP
5. 控制台确认：标签是否可写（§1-6）——决定是否恢复标签方案
6. （可选）写回 AK/SK + 是否授权自动改严重级/优先级/模块/负责人（§6-3、§7）
7. （可选）负责人姓名→数字 id 映射（§6-8）
