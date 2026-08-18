# AutoCommentReply

把“完整抓取整段视频评论区”的旧目标，转向“**用户选择一条评论讨论，工具为大模型提供完整可见上下文，并在用户确认后回复**”的上下文与回复助手。

> **当前状态（2026-08-16）**：当前 MVP 已包含 roadmap 的 M1（讨论定向读取）、M2（本地进程内认证 session 与 viewer 身份）与 M3（SQLite 持久化同步）——一条评论分享链接（b23.tv 短链或展开 URL）即可只读同步该根评论及其当前可见楼中楼；提供 Cookie 时程序在进程内建立认证 session，并在评论读取前用一次 nav 确认当前 viewer（与 legacy WBI 共用缓存）；匿名时使用显式 anonymous viewer 且不为身份请求 nav。提供 `--database PATH` 时，定向讨论会在输出前原子提交为一次持久化 sync run（viewer 隔离、ever-seen、完整基线、可见性差集与追加式账本），未提供时行为不变；legacy 整视频读取不支持持久化。旧的全量能力保留为 legacy 整视频只读基线（旧 MVP），仅作诊断兼容，不再是产品目标。通知事件、LLM/MCP 上下文与回复写接口（M4–M7）**未实现**；下文“目标工作流”的第 3–5 步仍是计划，不是当前命令。

## 目标工作流（第 1–2 步已可用，第 3–5 步 planned）

1. 在手机 B 站把一条评论“分享 → 复制链接”（b23.tv 短链或展开后的 bilibili.com URL）作为入口。**（当前 MVP 已可用）**
2. 工具规范化链接，只同步该根评论及它当前可见的楼中楼回复，不翻整段视频的根评论列表；同步按当前 session 的 viewer 视角执行（匿名显式、登录经一次 nav 确认）。提供 `--database PATH` 时，本轮同步作为原子 sync run 持久化（仅选中讨论）。**（当前 MVP 已可用）**
3. 当用户问“有人在评论区回复了我”时，工具用本地已认证账号按需读取“回复我的”通知，定位受影响讨论并重新同步；通知只是发现来源，当前重新抓取的根讨论才是上下文事实。**（planned）**
4. 大模型基于完整可见上下文（话题、所有参与者在该话题下说过什么、新增或当前不可见的差异）推断意图与立场并生成草稿；评论、用户名、通知等外部内容只作为证据，不作为模型或工具的指令；推断按次基于证据生成，不保存为长期用户画像。**（planned）**
5. 用户确认后，工具只发送这一条回复；发送幂等、可审计、保守节流，凭证留在本机。**（planned）**

计划中的三个工具能力是：打开并同步指定讨论、获取待回复上下文、确认后发送一条回复。当前 MVP 已实现“打开并同步指定讨论”的只读同步部分（CLI 定向模式，含本地认证 session 与 viewer 身份，可选 `--database` SQLite 持久化）；“获取待回复上下文”与“确认后发送一条回复”仍不可运行；契约见 [docs/roadmap.markdown](docs/roadmap.markdown)。

## 当前代码：当前 MVP（M1 定向 + M2 本地认证/viewer + M3 SQLite 持久化）+ legacy 基线

现有代码有两条只读读路径，由同一 CLI 入口按输入自动分流：

- **当前 MVP（roadmap 的 M1：讨论定向读取 + M2：本地认证 session/viewer 身份 + M3：SQLite 持久化同步，已实现）**：输入是评论分享链接（b23.tv 短链或展开 URL，含 `comment_root_id` / `comment_secondary_id` / `#reply` 标记）。程序把这条评论视为入口焦点，归约到它所属的根楼层，读取该根评论及楼层内当前全部可见回复，输出 schema 1.2。`focus` 不改变同步范围，也不当作 parent。定向输出顶层含 `viewer`，`comments`/`trees` 用 `author_id` 并带三态 `is_self`。可选 `--database PATH` 把该轮同步持久化（见下节）。
- **legacy 整视频只读基线（旧 MVP，仅诊断兼容）**：输入是**视频**引用（BV/av/aid/视频 URL/b23.tv 视频短链），完整翻取该视频当前可见的**全部根评论**及每个根楼层，构建评论树与根到叶对话链，输出 schema 1.0。它不再是产品目标；字段与行为保持不变（含 `user_id`）。

定向模式的路由与限制：含评论标记的链接进入严格定向模式，普通视频引用才进入 legacy；b23.tv 最多 5 跳，循环/畸形/非 http(s)/userinfo/危险端口/外站跳转被拒绝；缺 `comment_root_id` 或 focus 冲突时 fail closed，不回退全量。根无效（invisible、ID 不一致、关系非法）时 `complete=false`，不声称永久删除；只保留可确认属于请求根的 page1 回复为孤儿，外部根回复排除。

两条路径都支持可选本地认证输入（`--cookie-file` 优先，否则 `BILIBILI_COOKIE`），在进程内建立认证 session 与 viewer 身份；凭证不进输出、日志或诊断。默认无持久化；显式 `--database` 时定向讨论按 M3 持久化（见下节）；无通知、无 AI、无写接口。

## SQLite 持久化（M3，可选 `--database PATH`）

M3 已把用户主动选择的**定向讨论**接入本地 SQLite 持久化：显式提供 `--database PATH` 时，每次定向同步在输出 JSON 前原子提交为一个 sync run，跨进程保存 viewer、discussion、规范化评论事实、viewer-relative 观察、ever-seen、完整同步基线、可见性差集与同步账本；未提供 `--database` 时行为与之前完全一致。没有默认数据库路径，不读环境变量，不自动发现数据库，也不迁移旧 `comments.json`；数据库查询只通过 Python storage API（没有最终用户查询 CLI）。

```powershell
uv run auto-comment-reply "https://b23.tv/XXXXXX" `
  --database "$env:LOCALAPPDATA\AutoCommentReply\sync.db" `
  -o discussion.json
```

重复运行同一讨论链接并指定同一数据库即可跨小时累积观察；例如同一 viewer 的第二次完整同步会把上一轮基线中本轮未观察到的评论标为 `not_currently_visible`（只是“当前不可见”，不证明删除），并把新观察并入 ever-seen。

行为要点：

- 只支持定向评论分享链接。legacy 整视频引用与 `--database` 组合会在持久化前被拒绝：CLI exit 1，不产生 JSON，也不写入数据库。
- 第一次成功提交某个 `(viewer, discussion)` 的事务即自动建立 tracked/bound 状态，与该轮 `complete` 无关；没有单独的 track/untrack 命令。
- 每轮 sync run 追加写入账本：`observed_ids`、`newly_observed_ids`、`not_currently_visible_ids`、`previous_visible_ids`、最终 `complete` 与脱敏 `diagnostics`。`newly_observed` 无论 complete 与否都并入 ever-seen；只有最终 `complete=true` 才推进 last-complete baseline 并计算可见性差集；`complete=false` 只吸收新观察，不替换基线、不写任何缺失/删除/不可见推断。
- `comment_observation.current_visibility` 可以为空（该评论尚未进入任何完整基线），非空只允许 `visible` / `not_currently_visible`；`unavailable` 不是可见性值，只属于未来 M4 的 `reply_event.target_availability`。
- viewer 隔离：每个平台每个数据库只有一个稳定 anonymous viewer；登录 viewer 以 `(platform, platform_user_id)` 稳定识别，username 只是可更新展示字段。不同 viewer 的 baseline、observations 与 sync runs 互不串扰。
- 持久化是权威提交点：程序先构建并校验最终 schema 1.2 文档，再提交数据库，提交成功后才发布 JSON。SQLite 打开、schema、迁移、锁、约束或提交失败时整轮回滚、CLI exit 1、stdout 与输出文件均无本轮 JSON（即使远端结果本应 exit 2，也以持久化 fail-closed 优先）。
- 数据库使用标准库 `sqlite3`（无新运行时依赖），schema v1 带显式版本号与唯一约束；WAL、5 秒 busy timeout、外键约束开启；遇到比程序更新或无法识别的 schema 会拒绝访问，不猜测、不降级。

隐私：数据库包含本地私有产品数据（viewer mid、展示用户名与评论正文）。Cookie、CSRF、请求头与认证文件内容永不写入数据库；持久化错误使用固定脱敏文案，不回显数据库路径、SQL、评论正文或凭证。

### Python storage/query API（M3）

从 `auto_comment_reply.storage` 导入只读查询函数（全部显式接收 viewer/discussion 范围并按稳定顺序返回）：

- `find_viewer(db, viewer)` / `find_discussion(db, discussion)`：按稳定身份查找实体。
- `list_viewer_discussions(db, viewer)`：该 viewer 已跟踪的 discussions。
- `get_viewer_discussion_state(db, viewer, discussion)`：tracked/bound 状态、ever-seen、last-complete baseline 与 observations。
- `list_sync_runs(db, viewer, discussion)`：追加式 sync run 账本（含每轮 observed/newly-observed/visibility diff 与 diagnostics）。
- `list_comments(db, discussion)`：规范化评论事实（viewer 无关）。
- `list_observations(db, viewer, discussion)`：viewer 范围的 first/last seen 与 current_visibility。

示例：

```python
from pathlib import Path

from auto_comment_reply import DiscussionReference, ANONYMOUS_VIEWER
from auto_comment_reply.storage import (
    get_viewer_discussion_state,
    list_comments,
    list_sync_runs,
)

db = Path(r"C:\Users\You\AppData\Local\AutoCommentReply\sync.db")
disc = DiscussionReference(
    platform="bilibili",
    object_type="video",
    aid=170001,
    bvid="BV1xx411c7mD",
    root_comment_id=123456789,
)
state = get_viewer_discussion_state(db, ANONYMOUS_VIEWER, disc)
runs = list_sync_runs(db, ANONYMOUS_VIEWER, disc)
comments = list_comments(db, disc)
```

`persist_discussion_sync` 与 `SyncOutcome` 由包顶层 `auto_comment_reply` 导出；`PersistenceError` 带固定 `category`，供程序化处理。

### 安装与运行

需要 Python 3.11+，推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境。

```powershell
uv sync --dev
```

**当前 MVP：评论分享链接 → 定向同步一条讨论。** 用户在手机 B 站“分享 → 复制链接”，把链接原样传给程序即可；b23.tv 短链或展开 URL 都接受，可指向根评论或楼中楼（`#reply` / `comment_secondary_id` 只是入口焦点，不改变同步范围）：

```powershell
uv run auto-comment-reply "https://b23.tv/XXXXXX" -o discussion.json
uv run auto-comment-reply "https://www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=123456789&comment_secondary_id=987654321#reply987654321" -o discussion.json
```

**legacy 整视频只读基线（仅诊断兼容）。** 以下示例中的 `YOUR_BVID` 需要替换为目标视频的 BV 号：

```powershell
uv run auto-comment-reply YOUR_BVID -o comments.json
```

也可以传入完整视频链接、BV 号、`av123` 形式的 AV 号、纯数字 aid 或 `b23.tv` 视频短链；这些不带评论标记的引用都会进入 legacy 全量模式。

#### 安全准备登录 Cookie（Windows PowerShell + Chrome/Edge）

Cookie / `SESSDATA` 等价于敏感会话凭证：**绝不把账号密码、验证码、Cookie、Copy as cURL 或含 Cookie 的截图粘贴给 Agent 或聊天窗口**。Cookie 文件只保存在本机，且只保存一行浏览器实际发送的完整 Cookie 请求头值，不含 `Cookie:` 前缀、引号或 JSON；不要用 `document.cookie`（可能漏 HttpOnly），也不解码或改写内容。

浏览器已登录后：F12 → Network → 刷新 → 找 `api.bilibili.com/x/web-interface/nav` → 在 Headers 的 Request Headers 中复制 `Cookie`（或 Cookies/Request Cookies）里的实际请求值；若改用 Application → Storage → Cookies，需按 `name=value; name2=value2` 拼成一行。在工作区外创建文件并编辑：

```powershell
$cookieDir = Join-Path $env:LOCALAPPDATA "AutoCommentReply"
New-Item -ItemType Directory -Force $cookieDir | Out-Null
$cookiePath = Join-Path $cookieDir "bilibili.cookie"
notepad $cookiePath
```

粘贴上一步复制的 Cookie 值后保存。运行（首次建议使用全新输出文件名，避免认证失败时误读旧 JSON）：

```powershell
uv run auto-comment-reply "https://b23.tv/XXXXXX" `
  --cookie-file "$env:LOCALAPPDATA\AutoCommentReply\bilibili.cookie" `
  -o first-login-check.json
```

运行后读取输出中的 `viewer` 确认登录身份：

```powershell
$r = Get-Content first-login-check.json -Raw | ConvertFrom-Json
$r.viewer | Select-Object authenticated, platform_user_id, username
```

`--cookie-file` 优先；未指定时读取 `BILIBILI_COOKIE` 环境变量，两者都没有时按匿名运行。需要 Agent 代跑时，只提供 Cookie 文件路径和评论链接，**绝不提供 Cookie 内容**；认证输出虽不含 Cookie，但含 mid、username 与评论内容，也不应随意提交。当前登录态只由离线脱敏 fixture 验证，真实登录仍需本地运行确认。不要把真实 Cookie 写入源码、README、日志、命令示例或版本库；`.gitignore` 已忽略 `*.cookie`、`.env*` 和 `auth.json`。

认证与请求说明：

- 匿名定向**不请求 nav**，输出显式 anonymous viewer（`authenticated=false`、`platform_user_id=null`、`username=null`），所有 `is_self=null`。
- 认证定向会在评论读取前新增**一次** nav 身份请求（同一 Adapter 生命周期内缓存，并与 legacy WBI 取 key 共用；legacy 匿名仍会为 WBI 取 key 请求一次 nav）。
- nav 未登录（如 `code=-101` 或 `isLogin=false`）、mid 缺失/非正整数或响应结构无效 → 读取前致命错误，**exit 1 且不输出 JSON**，不静默降级为匿名。
- Cookie 文件去除首尾空白后为空，或仍包含内部换行 → exit 1，发生在任何网络读取之前。

常用选项：

```text
-o, --output PATH       输出 JSON；默认 '-' 为标准输出
--cookie-file PATH      从本机私有文件读取 Cookie（优先于环境变量）
--force                 允许覆盖已有输出文件
--compact               输出紧凑 JSON
--request-delay 0.25    相邻请求最小间隔
--timeout 15            单次请求超时
--retries 2             网络与临时服务错误的重试次数
--max-root-pages N      主评论分页安全阀（仅 legacy 全量模式生效；定向模式不翻主评论，root_pages_fetched=0）
--max-reply-pages N     单个楼中楼分页安全阀
--database PATH         本地 SQLite 数据库路径；仅定向讨论支持持久化（legacy 引用会在持久化前 exit 1）
-q, --quiet             只显示错误日志
-v, --verbose           显示调试日志
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 读取完成，JSON 中 `complete=true` |
| `1` | 输入/引用解析、视频解析、Cookie 文件、输出文件等读取前致命错误，含认证身份无法确认（提供 Cookie 但 nav 未登录/mid 非法/结构无效）；提供 `--database` 时，legacy 引用被拒绝或 SQLite 打开/schema/锁/约束/提交失败（整轮回滚）——均无 JSON 输出 |
| `2` | 已输出结构化结果，但接口、网络、鉴权、解析、分页或树关系使 `complete=false`；启用持久化且数据库事务成功时，按不完整同步规则落库后仍返回 `2` |

### 输出结构与完整性

JSON 顶层包含（`schema_version` 为 `1.2` 的定向结果另有 `discussion` 与 `viewer`；`1.0` 为 legacy 全量结果）：

```text
schema_version           # 1.2=讨论定向，1.0=legacy 整视频
generated_at
complete
video
discussion               # 仅定向模式：规范化讨论身份与 focus
viewer                   # 仅定向模式：当前 session 的无凭证 viewer 事实
stats
comments                 # 规范化评论平面列表
trees                    # 嵌套评论树
conversation_chains      # 每条根到叶分支的 comment_id 列表
orphan_comment_ids       # 缺父、错根或循环等无法可靠挂树的节点
duplicate_comment_ids
diagnostics
```

`discussion` 包含 `platform / object_type / oid / aid / bvid / root_comment_id / focus_comment_id / identity`。讨论身份 `(bilibili, video, oid, root_comment_id)` 与 focus/viewer 无关；`focus_comment_id` 只记录入口焦点，不参与建树、不当 parent。定向模式下 `stats.root_pages_fetched == 0`（不翻主评论）。

`viewer` 包含 `platform / authenticated / platform_user_id / username`；匿名时 `authenticated=false`、`platform_user_id=null`、`username=null`。定向 `comments`/`trees` 每条评论输出 `author_id` 与三态 `is_self`（viewer 认证且作者已知时按 `author_id == platform_user_id` 派生 true/false；匿名或作者未知为 `null`），不输出 `user_id` 别名。legacy schema 1.0 仍使用 `user_id`，且不含 `viewer`/`author_id`/`is_self`。

核心字段映射：`rpid → comment_id`、`member.mid → user_id`（内部事实模型；定向 schema 1.2 输出为 `author_id`，legacy schema 1.0 输出为 `user_id`）、`member.uname → username`、`content.message → content`、`root → root_id`、`parent → parent_id`、`ctime → created_at`。根评论的 `root_id` 和 `parent_id` 都是 `0`。

`complete=true` 只表示：程序按接口的明确终止信息读取完当前账号在本次运行时可见的数据，并通过了父链完整性检查；不承诺补全已删除、已屏蔽或当前账号无权看到的评论。

### 当前接口实现

| 用途 | 接口 | 模式 |
| --- | --- | --- |
| 视频 BV/AV 元数据 | `GET /x/web-interface/view` | 定向 + legacy |
| viewer 身份确认（仅认证 session） | `GET /x/web-interface/nav` | 定向（认证）+ legacy（认证） |
| 获取 WBI 密钥 | `GET /x/web-interface/nav` | 仅 legacy（认证时与身份请求共用同一次响应） |
| 主评论游标分页 | `GET /x/v2/reply/wbi/main` | 仅 legacy |
| 根评论元数据 + 楼中楼分页 | `GET /x/v2/reply/reply` | 定向 + legacy |

**定向模式（当前 MVP）**：认证时先用 `GET /x/web-interface/nav` 确认 viewer（评论读取前一次，缓存并与 legacy WBI 共用；匿名不请求 nav），再用 `GET /x/web-interface/view` 做视频归一化，最后用 `GET /x/v2/reply/reply` 一次取得 `data.root` 与第 1 页回复，之后从 `pn=2` 继续分页；不调用 `main`、不做 WBI 签名，主评论分页数为 0。

**legacy 全量模式**（2026-08-14 真实只读验证）还使用 `GET /x/web-interface/nav` 取 WBI 密钥，并用 `GET /x/v2/reply/wbi/main` 翻主评论。

接口均为 B 站网页端非公开接口，路径、参数和字段随时可能变化，未来变化只应修改 `BilibiliAdapter` 边界。

### 开发与验证

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run pytest --cov=auto_comment_reply --cov-report=term-missing
```

当前验证状态：**210 passed**，总覆盖率 **87%**；`ruff format --check` 与 `ruff check` 均通过。测试覆盖 WBI 签名、视频标识解析、主楼/楼中楼多页读取、嵌套父链、分支失败隔离、孤儿节点、错根、循环、重复 ID、网络重试、短链跳转安全，以及 M1 的引用解析、讨论身份、focus 语义、b23 安全跳转、定向分页与 fail-closed 路由；M2 新增匿名/有效登录/失效登录、viewer 解析、`is_self` 三态、nav 请求预算（至多一次且与 legacy WBI 共用）和 secret 泄漏路径（stdout/文件/stderr/repr/异常/JSON）。M3 新增 `tests/test_storage.py` 与 `tests/test_sync.py`，覆盖 schema v1 唯一键/约束、事务中途故障回滚与重开恢复、完整/不完整同步的 baseline 与 diff、空完整同步、幂等重复同步、viewer 隔离、`current_visibility` 枚举与 `unavailable` 隔离、大整数 ID 无精度损失、跨小时多讨论稳定查询、credentials 不落库、锁超时 fail-closed，以及 CLI 的 `--database` 成功/不完整/legacy 拒绝/持久化失败/无数据库回归等语义。

真实只读核验记录：

- 2026-08-16 匿名 nav 只读核验：`code=-101`、`isLogin=false`、`mid=null`、`uname=null`，且仍含可用的 WBI 数据（匿名取 WBI 密钥的合法形态）。
- 登录态**只**由脱敏离线 fixture 验证（`tests/_helpers.py` / `test_viewer.py` / `test_output.py` / `test_cli.py`），**未使用真实私人账号 smoke**，不伪称已做真实登录验证。
- M3 持久化路径全部由离线自动测试（`httpx.MockTransport` + 临时 SQLite 文件）验证，**没有对 `--database` 做过真实网络 smoke**。
- 早期真实运行（非自动测试）：匿名定向 CLI smoke 一次（1 根评论 + 1 回复、`root_pages_fetched=0`、`complete=true`）；legacy 全量模式于 2026-08-14 做过真实只读验证。

## 目标能力与当前差距

| 能力 | 状态 |
| --- | --- |
| 评论分享链接解析与讨论定向读取 | **已实现**（M1，CLI 定向模式，只读） |
| 本地认证 session 与 viewer 身份（`platform_user_id`） | **已实现**（M2：匿名显式且不请求 nav；登录一次 nav、失败 exit 1） |
| SQLite 持久化（仅选中讨论）与同步语义 | **已实现**（M3：显式 `--database`；viewer 隔离、ever-seen、完整基线、可见性差集、sync run 账本；无默认数据库与查询 CLI） |
| “回复我的”通知事件 ledger | planned，未实现（M4） |
| 面向 LLM 的完整上下文输出 | planned，未实现（M5） |
| 人工确认式回复写入（outbox、幂等） | planned，未实现（M6） |

M4+ 的目标命令/API 不在本 README 中给成可运行示例；其设计见 [docs/roadmap.markdown](docs/roadmap.markdown)，当前实现见 [docs/architecture.markdown](docs/architecture.markdown)。M3 的 `--database` 与 Python storage/query API 是当前可用能力，见上文。

## 文档导航

| 文档 | 作用 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码 Agent 的范围规则 |
| [docs/project.markdown](docs/project.markdown) | 项目为什么存在、为谁、边界与常青原则 |
| [docs/architecture.markdown](docs/architecture.markdown) | 当前实现架构（当前项目画像） |
| [docs/roadmap.markdown](docs/roadmap.markdown) | 未来里程碑与依赖顺序（计划，不是已实现事实） |
| [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md) | 三个参考项目的代码级调研与取舍 |

`docs/comet/` 由 Comet 管理具体 change 的 brief/spec/state/verification/archive 与功能生命周期；M2 与 M3 的正式需求、规格与验收历史分别由 `local-auth-viewer-identity` 与 `m3-sqlite-sync-semantics` change 承载，其与三份长期文档的分工见 [docs/project.markdown](docs/project.markdown)。

## 路线图指针

**当前 MVP（roadmap 的 M1：讨论定向读取 + M2：本地认证 session/viewer 身份 + M3：SQLite 持久化同步）已完成；legacy 整视频只读基线（旧 MVP）保留为诊断兼容。** 后续依赖顺序为：通知事件 → LLM/MCP/CLI 上下文 → 确认式 Writer → 加固；M4–M7 均未实现。依赖顺序与验收方向见 [docs/roadmap.markdown](docs/roadmap.markdown)。

## 参考项目（参考不等于复制）

- [Yotsuki2213/BiliBili_VideoRead_MCP](https://github.com/Yotsuki2213/BiliBili_VideoRead_MCP)：参考视频元数据、凭证注入、接口分层和结构化输出。
- [FunnySaltyFish/bilibili_comments_crawl](https://github.com/FunnySaltyFish/bilibili_comments_crawl)：参考 `parent` 邻接关系与根到叶 DFS 思路。
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot)：参考节流、错误退避和未来自动化模块边界。

本仓库没有复制上述项目代码。它们的范围、旧接口、分页缺口和许可证差异均记录在 [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md)；最终实现始终以本仓库文档和当前 B 站实际响应为准。
