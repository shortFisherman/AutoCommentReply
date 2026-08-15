# 架构：当前实现（截至 2026-08-15）

> 本文档是当前实现的权威描述，只写**已落地事实**，不写未来愿景。未来计划见 [roadmap.markdown](roadmap.markdown)，项目意图与长期原则见 [project.markdown](project.markdown)。

## 1. 当前范围

- Python 3.11+，运行时依赖 `httpx`；CLI 使用标准库 `argparse`，模型使用标准库 `dataclasses`。
- 平台：仅 Bilibili；远端行为：只读，不发送任何回复、删除或写操作。
- 当前源码中不存在数据库、AI 决策/生成、人工审核 UI 或自动回复实现。
- 产物：schema 1.0 的 JSON（文件或标准输出）。

范围刻意保持小：先在一个平台把“完整读取 → 建树 → 对话链”这条链路跑通并独立验收，而不是同时适配多平台。

## 2. 数据流

```text
CLI 输入（BV / av / aid / 视频 URL / b23.tv 短链）
  → 视频解析（/x/web-interface/view，得到 aid/bvid 与视频元数据）
  → Adapter（Cookie、请求头、WBI 签名）
  → 主评论游标分页（/x/v2/reply/wbi/main）
  → 每个根评论的楼中楼分页（/x/v2/reply/reply）
  → 规范化与按 comment_id 合并
  → 树关系校验与建树
  → 根到叶对话链（ID 列表）
  → schema 1.0 JSON（文件或 stdout）
```

读取是同步、串行、带最小请求间隔的：以完整性和分支级诊断优先，也降低对非公开接口的压力。

## 3. 模块边界

```text
src/auto_comment_reply/
├── __main__.py   # 包入口
├── cli.py        # 只读命令行：参数、Cookie 读取、输出写入、退出码
├── adapter.py    # B 站接口、WBI、分页、解析、错误分类与 Cookie（唯一 API 边界）
├── wbi.py        # WBI mixin key 派生与签名；只被 adapter 使用，属于 Adapter 边界
├── models.py     # 规范化模型与抓取结果：Comment、VideoInfo、Diagnostic、FetchResult、FetchStats
├── tree.py       # 父链校验、评论树、孤儿/错根/循环、trace_to_root、对话链
├── output.py     # schema 1.0 JSON 文档构建
└── errors.py     # 可诊断异常分类
```

硬约束：业务层不接触接口 URL、Cookie 或响应 JSON。CLI 只从本机私有文件或环境变量读取 Cookie 并传入 Adapter；接口 URL、响应 JSON、请求头注入与远端 Cookie 使用均位于 Adapter 模块边界内，`wbi.py` 属该边界。原因：B 站网页接口是非公开、易变的契约，把变化收口到一处后，接口变更只改这里及其测试。

## 4. 当前接口（非公开、易变）

| 用途 | 接口 |
| --- | --- |
| 视频 BV/AV 元数据 | `GET /x/web-interface/view` |
| WBI 密钥 | `GET /x/web-interface/nav` |
| 主评论游标分页 | `GET /x/v2/reply/wbi/main` |
| 楼中楼分页 | `GET /x/v2/reply/reply` |

这些都是 B 站网页端使用的非公开接口，路径、参数和字段随时可能变化，只能在 Adapter 边界内替换。WBI 流程概览：从 `nav` 的 `wbi_img.img_url`/`sub_url` 派生 mixin key（缓存 10 分钟）；主评论参数排序后加入 `wts`，以 `md5(query + mixin_key)` 生成 `w_rid`。匿名 `nav` 返回 `-101` 时仍可能携带可用密钥，是该接口的明确特例；主评论接口返回 `-403/-352` 时强制刷新一次密钥，仍失败则停止并标记不完整。长期文档不抄录签名置换常量与全部错误码，以源码为唯一权威实现。

## 5. 输入与安全

- 支持 BV 号、`av123` 形式的 AV 号、纯数字 aid、含 BV 的视频链接和 `b23.tv` 短链。
- 短链最多逐跳跟随 5 次；每一跳都检查下一跳域名，只允许 Bilibili 域名（`*.bilibili.com` 与 `b23.tv`），拒绝把用户输入变成任意 URL 请求入口。
- Cookie 来源：`--cookie-file`（本机私有单行文件）或 `BILIBILI_COOKIE` 环境变量；不提供 Cookie 时按匿名账号可见范围读取。日志、diagnostics 与输出不回显 Cookie。
- 凭证禁止写入源码、文档、日志或版本库。

## 6. 模型字段映射

| 模型字段 | B 站字段 | 语义 |
| --- | --- | --- |
| `comment_id` | `rpid` | 评论唯一 ID |
| `user_id` | `member.mid` | 用户身份唯一依据 |
| `username` | `member.uname` | 仅展示，不作身份 |
| `content` | `content.message` | 评论文本 |
| `root_id` | `root` | 所属根评论 ID |
| `parent_id` | `parent` | 直接父评论 ID |
| `created_at` | `ctime` | Unix 时间戳 |

模型还携带 `video_id`（B 站 BV 号）与 `reply_count`（接口提示的回复数）。根评论约定 `root_id == 0` 且 `parent_id == 0`。`rpid/root/parent` 缺失无法可靠建树，该评论被跳过并记录 error；展示或身份字段缺失时使用安全占位值（`user_id=0`、`username=""`、`content=""`、`created_at=0`）并记录 error，结果不完整。

统一命名是为了让业务代码不依赖 B 站字段名（`rpid/mid/uname/root/parent/ctime`），也为将来多平台预留一致接口；身份判断只允许使用 `user_id`，因为用户名可改名、可重复。

## 7. 分页与完整性

主评论：

- 第一页 offset 为空，之后使用 `data.cursor.pagination_reply.next_offset`。
- 正常终止：`cursor.is_end` 为 true；或 `replies` 为合法空值且接口未明确表示未结束。
- 未结束但缺少 `next_offset`、offset 重复（游标环）、请求/解析失败或达到 `max_root_pages` 安全阀 → 停止并把结果标记为不完整。
- `cursor.all_count` 是当前可见评论总数提示（含主评论与回复），不是根评论数量；它只在所有楼中楼结束后与最终唯一评论数比较，差异产生 warning。

楼中楼：

- 对 `rcount > 0` 或带有内嵌回复预览的根评论，以 `pn`（从 1 开始）、`ps=20` 逐页读取；`rcount == 0` 且无预览时视为接口明确报告“当前无回复”，不额外请求。
- 正常终止：已读取的唯一回复数达到 `data.page.count`；或接口计数为 0 且当前页 `replies` 为合法空值。
- 未达计数提前空页、重复页指纹（防循环）、解析失败或达到 `max_reply_pages` 安全阀 → 当前分支标记为不完整，其他已发现分支继续读取（分支隔离）。
- 抓取期间计数变化产生 `count_changed` warning，不推翻接口明确结束的事实。

语义：`complete=true` 只代表“本次运行中、当前账号可见的评论快照按接口终止信号读完，并通过父链完整性检查”。它不代表补回已删除、已屏蔽或账号无权看的内容，也不代表抓取期间远端静止。

## 8. 树、孤儿与对话链

- 建树前建立 `comment_id → Comment` 索引；输入中重复 ID 保留首次记录并进入 `duplicate_comment_ids`。
- 合法节点按直接 `parent_id` 连接成树。缺父、`root_id` 与父链实际根不一致、父链循环、非根节点 `root_id/parent_id` 为 0 等关系非法节点**不伪造边**：只保留在平面 `comments` 中，并进入 `orphan_comment_ids`，同时记录 error。把缺父节点挂到根下会制造原始数据中不存在的父子关系，与 `parent_id` 语义冲突，因此是错误做法。
- 根节点与同层子节点按 `(created_at, comment_id)` 升序排序，重复运行的结构稳定。
- 全部根到叶分支用迭代 DFS 导出，输出为 `comment_id` ID 列表（`conversation_chains`），避免复制正文；分叉共享前缀会在多条链中保留。
- `trace_to_root(comment_id, comments)` 从任意评论沿直接父链追溯并返回根优先路径；目标不存在、缺父、循环、错根，或非根节点 parent_id 为 0/非法终止时抛出 `CommentGraphError`，不静默截断。

## 9. 输出 schema 1.0

顶层固定字段：

```text
schema_version          "1.0"
generated_at            UTC ISO-8601
complete                bool
video                   视频元数据（aid/bvid/标题/UP 主/可见评论数提示）
stats                   分页页数、期望总数、根/回复/总评论数、去重计数、孤儿数与对话链数
comments                规范化评论平面列表
trees                   嵌套评论树
conversation_chains     根到叶 ID 链
orphan_comment_ids      无法可靠挂树的节点 ID
duplicate_comment_ids   tree 构建阶段收到重复节点的 ID
diagnostics             info / warning / error 列表
```

- 任何 `error` 级诊断都会让 `complete` 变为 `false`；`warning` 只说明可诊断现象（如计数漂移），不推翻 `complete`。
- 同时输出平面列表、嵌套树与 ID 链，是为了让消费方无需重复推导：平面列表便于索引，嵌套树便于检查结构，根到叶链便于恢复上下文。

## 10. 重复语义

正常 CLI 路径中，Adapter 是唯一解析入口：置顶、普通、内嵌预览与楼中楼页可能重复返回同一 `rpid`，Adapter 按 `comment_id` 合并（保留更完整的展示字段与更大的 `reply_count`；`root/parent` 冲突则记录 error），并在 `stats.duplicate_comments_seen` 计数。

因此正常 CLI 输出里 `duplicate_comment_ids` **通常为空**——它主要报告 tree builder 直接收到重复节点的程序化场景（例如绕开 Adapter 直接调用树构建 API）。

## 11. 错误分类、安全与退出码

- `NetworkError`：连接/超时，有限次指数退避重试。
- `AccessDeniedError`：HTTP 403，或 API `-352/-403/-412` 访问被拒或风控。
- `RateLimitError`：HTTP 412/429，或 API `-799/-509` 频率受限；HTTP 429 与 5xx 在有限重试耗尽前可重试。
- `HttpError`：其余非成功 HTTP；HTTP 5xx 在有限重试耗尽后以 `HttpError` 报告。
- `AuthenticationError`：API `-101`（匿名 `nav` 的 `-101` 特例除外）登录态无效。
- `ParameterError`：输入或 API 参数错误（`-400`）。
- `BusinessError`：视频/评论不存在（`-404/100100404`）、评论区关闭（`12002`）等业务错误。
- `ResponseParseError` / `PaginationError`：响应结构异常或分页契约被破坏。

CLI 退出码：`0` 表示已输出且 `complete=true`；`2` 表示已输出结构化结果但不完整；`1` 表示读取前致命错误（输入、视频解析、Cookie 文件、输出文件等）。

## 12. 测试策略

离线测试使用 `httpx.MockTransport`，不依赖真实网络，覆盖 WBI 固定向量、视频标识解析、主/楼分页、置顶与内嵌预览去重、分支失败隔离、孤儿/错根/循环/重复 ID、网络重试与短链安全跳转。真实 B 站运行只由项目所有者按需进行，不进入自动测试。

## 13. 已知限制

- 只能看到“当前账号在本次运行期间可见”的评论；已删除、已屏蔽或权限之外的评论不在数据中。
- 抓取期间远端仍在变化，`complete` 不承诺快照后静止。
- B 站接口是非公开契约，可能随时失效或加风控；重试只能缓解临时问题。
- 一次运行的数据全部保存在进程内，无持久化；大批量视频或超长历史需要后续里程碑的存储能力。
