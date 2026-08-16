# AutoCommentReply

把“完整抓取整段视频评论区”的旧目标，转向“**用户选择一条评论讨论，工具为大模型提供完整可见上下文，并在用户确认后回复**”的上下文与回复助手。

> **当前状态（2026-08-16）**：当前 MVP（roadmap 的 M1：讨论定向读取）已完成——一条评论分享链接（b23.tv 短链或展开 URL）即可只读同步该根评论及其当前可见楼中楼。旧的全量能力保留为 legacy 整视频只读基线（旧 MVP），仅作诊断兼容，不再是产品目标。SQLite、账号身份、通知事件、LLM/MCP 上下文与回复写接口**均未实现**；下文“目标工作流”的第 3–5 步仍是计划，不是当前命令。

## 目标工作流（第 1–2 步已可用，第 3–5 步 planned）

1. 在手机 B 站把一条评论“分享 → 复制链接”（b23.tv 短链或展开后的 bilibili.com URL）作为入口。**（当前 MVP 已可用）**
2. 工具规范化链接，只同步该根评论及它当前可见的楼中楼回复，不翻整段视频的根评论列表。**（当前 MVP 已可用）**
3. 当用户问“有人在评论区回复了我”时，工具用本地已认证账号按需读取“回复我的”通知，定位受影响讨论并重新同步；通知只是发现来源，当前重新抓取的根讨论才是上下文事实。**（planned）**
4. 大模型基于完整可见上下文（话题、所有参与者在该话题下说过什么、新增或当前不可见的差异）推断意图与立场并生成草稿；评论、用户名、通知等外部内容只作为证据，不作为模型或工具的指令；推断按次基于证据生成，不保存为长期用户画像。**（planned）**
5. 用户确认后，工具只发送这一条回复；发送幂等、可审计、保守节流，凭证留在本机。**（planned）**

计划中的三个工具能力是：打开并同步指定讨论、获取待回复上下文、确认后发送一条回复。当前 MVP 已实现“打开并同步指定讨论”的只读同步部分（CLI 定向模式）；“获取待回复上下文”与“确认后发送一条回复”仍不可运行；契约见 [docs/roadmap.markdown](docs/roadmap.markdown)。

## 当前代码：当前 MVP（M1 定向）+ legacy 基线

现有代码有两条只读读路径，由同一 CLI 入口按输入自动分流：

- **当前 MVP（roadmap 的 M1：讨论定向读取，已实现）**：输入是评论分享链接（b23.tv 短链或展开 URL，含 `comment_root_id` / `comment_secondary_id` / `#reply` 标记）。程序把这条评论视为入口焦点，归约到它所属的根楼层，读取该根评论及楼层内当前全部可见回复，输出 schema 1.1。`focus` 不改变同步范围，也不当作 parent。
- **legacy 整视频只读基线（旧 MVP，仅诊断兼容）**：输入是**视频**引用（BV/av/aid/视频 URL/b23.tv 视频短链），完整翻取该视频当前可见的**全部根评论**及每个根楼层，构建评论树与根到叶对话链，输出 schema 1.0。它不再是产品目标。

定向模式的路由与限制：含评论标记的链接进入严格定向模式，普通视频引用才进入 legacy；b23.tv 最多 5 跳，循环/畸形/非 http(s)/userinfo/危险端口/外站跳转被拒绝；缺 `comment_root_id` 或 focus 冲突时 fail closed，不回退全量。根无效（invisible、ID 不一致、关系非法）时 `complete=false`，不声称永久删除；只保留可确认属于请求根的 page1 回复为孤儿，外部根回复排除。

两条路径都只有 Cookie 文本，没有登录 session 与 viewer 身份（`platform_user_id` / B 站 mid）；无持久化、无通知、无 AI、无写接口。

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

不带 Cookie 时按匿名账号可见范围读取；需要登录可见范围时，推荐把 Cookie 放进工作区之外的本机私有文件：

```powershell
uv run auto-comment-reply YOUR_BVID `
  --cookie-file C:\private\bilibili.cookie `
  -o comments.json
```

也支持 `BILIBILI_COOKIE` 环境变量。不要把真实 Cookie 写入源码、README、日志、命令示例或版本库；`.gitignore` 已忽略 `*.cookie`、`.env*` 和 `auth.json`。

常用选项：

```text
-o, --output PATH       输出 JSON；默认 '-' 为标准输出
--force                 允许覆盖已有输出文件
--compact               输出紧凑 JSON
--request-delay 0.25    相邻请求最小间隔
--timeout 15            单次请求超时
--retries 2             网络与临时服务错误的重试次数
--max-root-pages N      主评论分页安全阀（仅 legacy 全量模式生效；定向模式不翻主评论，root_pages_fetched=0）
--max-reply-pages N     单个楼中楼分页安全阀
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 读取完成，JSON 中 `complete=true` |
| `1` | 输入/引用解析、视频解析、Cookie 文件或输出文件等在评论读取前发生致命错误（含缺 root、focus 冲突、短链安全拒绝） |
| `2` | 已输出结构化结果，但接口、网络、鉴权、解析、分页或树关系使 `complete=false` |

### 输出结构与完整性

JSON 顶层包含（`schema_version` 为 `1.1` 的定向结果另有 `discussion`；`1.0` 为 legacy 全量结果）：

```text
schema_version           # 1.1=讨论定向，1.0=legacy 整视频
generated_at
complete
video
discussion               # 仅定向模式：规范化讨论身份与 focus
stats
comments                 # 规范化评论平面列表
trees                    # 嵌套评论树
conversation_chains      # 每条根到叶分支的 comment_id 列表
orphan_comment_ids       # 缺父、错根或循环等无法可靠挂树的节点
duplicate_comment_ids
diagnostics
```

`discussion` 包含 `platform / object_type / oid / aid / bvid / root_comment_id / focus_comment_id / identity`。讨论身份 `(bilibili, video, oid, root_comment_id)` 与 focus/viewer 无关；`focus_comment_id` 只记录入口焦点，不参与建树、不当 parent。定向模式下 `stats.root_pages_fetched == 0`（不翻主评论）。

核心字段映射：`rpid → comment_id`、`member.mid → user_id`（legacy 输出字段；目标模型统一为 `author_id`）、`member.uname → username`、`content.message → content`、`root → root_id`、`parent → parent_id`、`ctime → created_at`。根评论的 `root_id` 和 `parent_id` 都是 `0`。

`complete=true` 只表示：程序按接口的明确终止信息读取完当前账号在本次运行时可见的数据，并通过了父链完整性检查；不承诺补全已删除、已屏蔽或当前账号无权看到的评论。

### 当前接口实现

| 用途 | 接口 | 模式 |
| --- | --- | --- |
| 视频 BV/AV 元数据 | `GET /x/web-interface/view` | 定向 + legacy |
| 获取 WBI 密钥 | `GET /x/web-interface/nav` | 仅 legacy |
| 主评论游标分页 | `GET /x/v2/reply/wbi/main` | 仅 legacy |
| 根评论元数据 + 楼中楼分页 | `GET /x/v2/reply/reply` | 定向 + legacy |

**定向模式（当前 MVP）**：先用 `GET /x/web-interface/view` 做视频归一化，再用 `GET /x/v2/reply/reply` 一次取得 `data.root` 与第 1 页回复，之后从 `pn=2` 继续分页；不调用 `nav`/WBI/main，主评论分页数为 0。

**legacy 全量模式**（2026-08-14 真实只读验证）还使用 `GET /x/web-interface/nav` 取 WBI 密钥，并用 `GET /x/v2/reply/wbi/main` 翻主评论。

接口均为 B 站网页端非公开接口，路径、参数和字段随时可能变化，未来变化只应修改 `BilibiliAdapter` 边界。

### 开发与验证

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run pytest --cov=auto_comment_reply --cov-report=term-missing
```

当前验证状态：`129 passed`，覆盖率 89%。测试覆盖 WBI 签名、视频标识解析、主楼/楼中楼多页读取、嵌套父链、分支失败隔离、孤儿节点、错根、循环、重复 ID、网络重试、短链跳转安全，以及 M1 的引用解析、讨论身份、focus 语义、b23 安全跳转、定向分页与 fail-closed 路由。已做一次匿名真实 CLI smoke：1 根评论 + 1 回复、`root_pages_fetched=0`、`complete=true`。

## 目标能力与当前差距

| 能力 | 状态 |
| --- | --- |
| 评论分享链接解析与讨论定向读取 | **已实现**（当前 MVP，CLI 定向模式，只读） |
| 本地认证 session 与 viewer 身份（`platform_user_id`） | planned，未实现 |
| SQLite 持久化（仅选中讨论）与同步语义 | planned，未实现 |
| “回复我的”通知事件 ledger | planned，未实现 |
| 面向 LLM 的完整上下文输出 | planned，未实现 |
| 人工确认式回复写入（outbox、幂等） | planned，未实现 |

M2+ 的目标命令/API 不在本 README 中给成可运行示例；其设计见 [docs/roadmap.markdown](docs/roadmap.markdown)，当前实现见 [docs/architecture.markdown](docs/architecture.markdown)。

## 文档导航

| 文档 | 作用 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码 Agent 的范围规则 |
| [docs/project.markdown](docs/project.markdown) | 项目为什么存在、为谁、边界与常青原则 |
| [docs/architecture.markdown](docs/architecture.markdown) | 当前实现架构（当前项目画像） |
| [docs/roadmap.markdown](docs/roadmap.markdown) | 未来里程碑与依赖顺序（计划，不是已实现事实） |
| [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md) | 三个参考项目的代码级调研与取舍 |

`docs/comet/` 由 Comet 管理具体 change 的 brief/spec/state/verification/archive 与功能生命周期；当前没有 active change，其与三份长期文档的分工见 [docs/project.markdown](docs/project.markdown)。

## 路线图指针

**当前 MVP（roadmap 的 M1：讨论定向读取）已完成；legacy 整视频只读基线（旧 MVP）保留为诊断兼容。** 后续依赖顺序为：认证身份 → SQLite 与同步语义 → 通知事件 → LLM/MCP/CLI 上下文 → 确认式 Writer → 加固；以上 M2+ 均未实现，当前没有 active Comet change。依赖顺序与验收方向见 [docs/roadmap.markdown](docs/roadmap.markdown)。

## 参考项目（参考不等于复制）

- [Yotsuki2213/BiliBili_VideoRead_MCP](https://github.com/Yotsuki2213/BiliBili_VideoRead_MCP)：参考视频元数据、凭证注入、接口分层和结构化输出。
- [FunnySaltyFish/bilibili_comments_crawl](https://github.com/FunnySaltyFish/bilibili_comments_crawl)：参考 `parent` 邻接关系与根到叶 DFS 思路。
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot)：参考节流、错误退避和未来自动化模块边界。

本仓库没有复制上述项目代码。它们的范围、旧接口、分页缺口和许可证差异均记录在 [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md)；最终实现始终以本仓库文档和当前 B 站实际响应为准。
