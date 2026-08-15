# AutoCommentReply

自动读取视频评论，并在后续里程碑中按需回复的自动化项目。当前只实现 **MVP1：完整、只读地抽取一条 Bilibili 视频当前可见的评论，并还原成评论树与根到叶对话链**。

> 当前状态：MVP1 已有可运行的 Python 实现和离线测试。AI、数据库、自动回复、前端仍明确不在当前范围内。

## 这个项目想做什么

最终路线是：

```text
读取评论 → SQLite 增量去重 → AI 决策与生成 → 人工审核 → 自动回复
```

当前只完成第一段的数据基础：

- 使用 B 站网页当前实际使用的评论接口，不依赖第三方爬虫框架。
- 完整翻完主评论和每个根评论下的楼中楼分页。
- 用 `rpid` / `root` / `parent` 还原评论树。
- 支持从任意评论沿直接父链追溯到根评论。
- 深度优先导出每一条“根评论 → 叶子评论”对话链。
- 用户身份只使用稳定的 `mid`，用户名仅用于展示。
- 所有 B 站非公开接口细节都封装在 `BilibiliAdapter`。
- 局部失败时保留已读取数据，但把整体结果标记为不完整，绝不静默丢数据后宣称成功。

## 快速开始

需要 Python 3.11 或更高版本，推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境。

```powershell
uv sync --dev
uv run auto-comment-reply BV13YBpBCEE2 -o comments.json
```

也可以传入：

- 完整视频链接；
- BV 号；
- `av123` 形式的 AV 号；
- 纯数字 aid；
- `b23.tv` 短链。

不带 Cookie 时，程序读取匿名账号当前可见的数据。需要登录可见范围时，推荐把 Cookie 放进工作区之外的本机私有文件：

```powershell
uv run auto-comment-reply BV13YBpBCEE2 `
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
--max-root-pages N      主评论分页安全阀
--max-reply-pages N     单个楼中楼分页安全阀
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 读取完成，JSON 中 `complete=true` |
| `1` | 输入、视频解析、Cookie 文件或输出文件等在评论读取前发生致命错误 |
| `2` | 已输出结构化结果，但评论接口、网络、鉴权、解析、分页或树关系使 `complete=false` |

## 输出结构

JSON 顶层包含：

```text
schema_version
generated_at
complete
video
stats
comments                 # 规范化评论平面列表
trees                    # 嵌套评论树
conversation_chains      # 每条根到叶分支的 comment_id 列表
orphan_comment_ids       # 缺父、错根或循环等无法可靠挂树的节点
duplicate_comment_ids
diagnostics
```

核心字段映射：

| 模型字段 | B 站字段 |
| --- | --- |
| `comment_id` | `rpid` |
| `user_id` | `member.mid` |
| `username` | `member.uname` |
| `content` | `content.message` |
| `root_id` | `root` |
| `parent_id` | `parent` |
| `created_at` | `ctime` |

根评论的 `root_id` 和 `parent_id` 都是 `0`。回复的 `root_id` 指向所属根评论，`parent_id` 指向直接父评论。

## 当前接口实现

以下接口行为已于 2026-08-14 通过 B 站真实只读请求验证：

| 用途 | 接口 |
| --- | --- |
| 视频 BV/AV 元数据 | `GET /x/web-interface/view` |
| 获取 WBI 密钥 | `GET /x/web-interface/nav` |
| 主评论游标分页 | `GET /x/v2/reply/wbi/main` |
| 单个根评论的楼中楼分页 | `GET /x/v2/reply/reply` |

旧的 `/x/v2/reply` 当前仍可能返回少量评论，但不能可靠遍历全部主评论，因此没有作为 MVP1 的完整读取入口。接口均为非公开接口，未来变化只应修改 `BilibiliAdapter`。

`complete=true` 只表示：程序按照接口的明确终止信息读取完当前账号在本次运行时可见的数据，并通过了父链完整性检查；不承诺补全已删除、已屏蔽或当前账号无权看到的评论。

## 开发与验证

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run pytest --cov=auto_comment_reply --cov-report=term-missing
```

测试覆盖 WBI 签名、视频标识解析、主楼/楼中楼多页读取、嵌套父链、分支失败隔离、孤儿节点、错根、循环、重复 ID、网络重试和短链跳转安全。

## 文档导航

| 文档 | 作用 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码 Agent 的范围规则 |
| [docs/PROJECT.md](docs/PROJECT.md) | 做什么、不做什么、验收标准 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 数据流、模型、树算法和 Adapter 边界 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 已确定的技术决定及原因 |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | 当前实现、接口分页、JSON 与错误语义 |
| [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md) | 三个参考项目的代码级调研与取舍 |

## 路线图

1. **MVP1：完整评论树读取（当前）**
   - 只读抓取、规范化、建树、对话链、完整性和诊断。
2. **SQLite 增量去重**
   - 以 `comment_id` 为唯一键持久化并增量读取。
3. **AI 决策与生成**
   - 消费评论树和对话链，生成待审核草稿。
4. **人工审核**
   - 确认、修改或拒绝 AI 草稿。
5. **自动回复**
   - 仅发送审核通过的内容，并继续通过 Adapter 隔离写接口。

## 参考项目（参考不等于复制）

- [Yotsuki2213/BiliBili_VideoRead_MCP](https://github.com/Yotsuki2213/BiliBili_VideoRead_MCP)：参考视频元数据、凭证注入、接口分层和结构化输出。
- [FunnySaltyFish/bilibili_comments_crawl](https://github.com/FunnySaltyFish/bilibili_comments_crawl)：参考 `parent` 邻接关系与根到叶 DFS 思路。
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot)：参考节流、错误退避和未来自动化模块边界。

本仓库没有复制上述项目代码。它们的范围、旧接口、分页缺口和许可证差异均记录在 [docs/REFERENCE_RESEARCH.md](docs/REFERENCE_RESEARCH.md)；最终实现始终以本仓库文档和当前 B 站实际响应为准。
