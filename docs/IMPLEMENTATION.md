# IMPLEMENTATION —— MVP1 当前实现说明

记录日期：2026-08-14

本文描述已经落地的 MVP1 实现。B 站接口是非公开接口，本文不是稳定 API 承诺；接口变化时只修改 `BilibiliAdapter` 及其测试。

## 1. 技术栈与目录

- Python 3.11+
- `httpx`：HTTP 客户端和可注入的测试 Transport
- 标准库 `dataclasses`：统一模型
- 标准库 `argparse`：只读 CLI
- `pytest`：离线接口与算法测试
- `ruff`：格式与静态检查

```text
src/auto_comment_reply/
├── adapter.py      # B 站接口、WBI、分页、解析、错误与 Cookie
├── wbi.py          # WBI mixin key 与签名
├── models.py       # 平台无关的评论、视频、诊断与统计模型
├── tree.py         # 父链校验、评论树、孤儿、循环和对话链
├── output.py       # schema 1.0 JSON
├── errors.py       # 可诊断异常分类
├── cli.py          # 只读命令行入口
└── __main__.py
```

## 2. 输入与视频标识

支持 BV 号、含 BV 的视频链接、AV 号、纯数字 aid 和 `b23.tv` 短链。

短链最多跟随 5 次重定向。每次请求下一跳之前都会检查域名，拒绝跳转到 Bilibili 之外的地址，避免把用户输入变成任意 URL 请求入口。

识别标识后调用：

```text
GET https://api.bilibili.com/x/web-interface/view
```

统一得到 `aid`、`bvid`、标题、UP 主 `mid` 和接口提供的评论总数提示。后续评论接口使用 `aid` 作为 `oid`。

## 3. WBI 签名

主评论当前使用：

```text
GET https://api.bilibili.com/x/v2/reply/wbi/main
```

流程：

1. 请求 `/x/web-interface/nav`。
2. 从 `data.wbi_img.img_url` 与 `sub_url` 提取两个 key。
3. 使用 B 站网页当前的 64 位重排表派生 32 位 mixin key。
4. 对参数值移除 `!'()*`，加入当前 `wts`。
5. 按参数名排序并 URL 编码。
6. 计算 `md5(query + mixin_key)` 得到 `w_rid`。

匿名访问 `nav` 时，B 站会返回 `code=-101`，但仍提供可用的 `data.wbi_img`。Adapter 只在这个特定接口允许该返回码继续；其他接口的 `-101` 仍被分类为登录态无效。

WBI key 缓存 10 分钟。主评论返回 API `-403` 或 `-352` 时强制刷新一次 key，再失败则停止主分页并标记结果不完整。

## 4. 主评论完整分页

主评论参数：

```text
oid=<aid>
type=1
mode=3
pagination_str={"offset":"<cursor>"}
plat=1
seek_rpid=
web_location=1315875
wts=<timestamp>
w_rid=<signature>
```

第一页 offset 为空字符串，后续使用：

```text
data.cursor.pagination_reply.next_offset
```

每页合并 `top_replies` 和 `replies`，按 `rpid` 去重。内嵌的少量 `root.replies` 只作为局部失败时的部分数据保底，不能代替楼中楼独立分页。

主分页正常终止条件：

- `cursor.is_end == true`；或
- `replies` 为合法的 `null/[]`，同时接口没有明确表示 `is_end=false`，也没有下一页游标。

即使当前页为空，只要接口给出 `is_end=false` 和有效 `next_offset`，程序仍继续下一页。`replies` 字段完全缺失属于解析错误，不等同于合法空页。

以下情况会停止并标记不完整：

- 尚未结束但缺少 `next_offset`；
- offset 重复形成游标环；
- 请求或解析失败；
- 达到 `max_root_pages` 安全上限。

`cursor.all_count` 是当前可见评论总数提示，包含主评论和回复，不是根评论数量。所有楼中楼结束后，程序才将它与最终唯一评论总数比较；抓取期间的新增、删除和置顶变化只产生 warning，不推翻接口已明确结束的事实。

## 5. 楼中楼完整分页

对每个 `rcount > 0` 或带有内嵌回复预览的根评论调用：

```text
GET https://api.bilibili.com/x/v2/reply/reply
```

`rcount == 0` 且没有内嵌预览时，将该根评论视为接口明确报告“当前无回复”，不会额外发起楼中楼请求。根评论缺失或返回非法 `rcount` 则属于解析错误，结果标记为不完整。与所有在线快照一样，根页读取完成后才新增的回复不属于本次快照。

参数：

```text
oid=<aid>
type=1
root=<根评论 rpid>
pn=<页码，从 1 开始>
ps=20
```

正常终止条件：

- 已读取的唯一回复数达到 `data.page.count`；或
- 接口计数为 0 且当前页 `replies` 为合法空值。

如果唯一回复数尚未达到 `page.count`，但接口提前返回空页，则当前分支标记为不完整；不再根据 `page_number * page_size` 推断“应该已经读完”。`replies` 字段完全缺失属于解析错误。

达到 `max_reply_pages`、某页网络失败、风控或解析失败时，只把当前根评论分支标记为失败；其他已发现根评论仍继续读取。最终输出保留已获取数据，并设置 `complete=false`。

## 6. 规范化与去重

Adapter 是唯一解析 B 站 JSON 的模块。关系字段 `rpid/root/parent` 缺失时无法可靠建树，该评论会被跳过并记录 error。

展示或身份字段缺失时仍输出安全占位值：

- `user_id=0`
- `username=""`
- `content=""`
- `created_at=0`

但解析结果会被标记为不完整，避免把已删除或响应变化静默伪装成完整数据。

主接口置顶评论、普通评论、内嵌预览和楼中楼完整页可能重复返回同一个 `rpid`。Adapter 按 `comment_id` 合并，保留更完整的展示字段和更大的 `reply_count`；若同一 ID 的 `root/parent` 冲突，则记录 error。

## 7. 建树与对话链

建树前先创建 `comment_id → Comment` 索引。对每个节点迭代追踪父链并缓存解析结果：

- 父节点缺失：节点及其下游放入 `orphan_comment_ids`；
- `root_id` 与父链实际根不一致：放入孤儿并报告 `root_mismatch`；
- 父链成环：报告 `comment_cycle`，相关节点不进入正常树；
- 合法节点：按直接 `parent_id` 连接。

根节点和同层子节点按 `(created_at, comment_id)` 升序排序，保证重复运行的结构稳定。

全部对话链使用迭代 DFS 导出。叶节点就是一条链的终点；分叉会分别输出完整分支，共享前缀允许在多条链中重复出现。

程序还公开 `trace_to_root(comment_id, comments)`，用于从任意评论追溯直接父链。目标不存在、缺父、错根或循环都会抛出明确的 `CommentGraphError`；列表输入出现重复 ID 时与建树一致，保留首次记录。

## 8. JSON schema 1.0

```json
{
  "schema_version": "1.0",
  "generated_at": "UTC ISO-8601",
  "complete": true,
  "video": {},
  "stats": {},
  "comments": [],
  "trees": [],
  "conversation_chains": [],
  "orphan_comment_ids": [],
  "duplicate_comment_ids": [],
  "diagnostics": []
}
```

`conversation_chains` 使用 ID 数组，避免在分支较多时重复复制完整评论对象。消费方可从 `comments` 建立索引恢复正文；`trees` 已提供嵌套的完整评论对象。

只有 Adapter 抓取完整、解析无 error、树关系无 error 时，顶层 `complete` 才为 `true`。warning 说明抓取期间计数变化等非静默现象，但接口明确结束时不自动将结果改为不完整。

## 9. 错误与重试

- 网络连接、超时、HTTP 429 和 5xx：有限次数指数退避；
- HTTP 403/412：视为登录态或风控，立即停止当前作用域；
- API `-101`：除匿名 nav 特例外，视为登录失效；
- API `-352/-403/-412`：访问或风控错误；
- API `-799/-509`：频率限制，提示可重试但不在当前分支长时间自动等待；
- API `-400`：参数错误；
- API `12002`：评论区关闭；
- JSON/字段错误：解析错误，保留其他不受影响的数据。

日志和 diagnostics 从不包含 Cookie 或请求头。

## 10. 当前验证

离线测试使用 `httpx.MockTransport`，不依赖真实网络，覆盖：

- WBI 固定测试向量；
- 两页主评论游标；
- 两页楼中楼和三级父链；
- 置顶与内嵌预览去重；
- 一个分支失败、其他分支继续；
- 缺父、错根、循环与重复 ID；
- 网络重试；
- b23 安全跳转；
- 匿名 nav 的 `code=-101` 特例。

2026-08-14 使用匿名真实请求对 `BV13YBpBCEE2` 做过端到端冒烟验证：接口提示 2 条可见评论，实际读取 1 个根评论和 1 个回复，树、父链、对话链与总数一致，输出 `complete=true`。

同日对中型样本 `BV1qMgN6BE98` 做完整分页验证：读取 15 页主评论和 34 页楼中楼，共得到 265 个根评论、124 条回复、389 条唯一评论及 334 条根到叶对话链；接口总数与实际总数一致，孤儿和 diagnostics 均为 0，输出 `complete=true`。

审查修复后的再次回归中，该活跃视频的接口计数继续实时增长：一次运行读取到 390 条唯一评论，而结束时 `all_count=391`。全部分页仍明确结束、无孤儿，程序按设计输出一条 `count_changed` warning，未静默忽略这个快照差异。对于持续有新评论的视频，`complete` 表示本次分页快照按接口终止信号读完，不代表远端在运行结束后停止变化。
