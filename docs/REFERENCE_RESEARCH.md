# REFERENCE_RESEARCH —— 三个参考项目的调研与取舍

调研日期：2026-08-14

原则：参考公开接口行为、模块边界和通用算法，不复制代码；本项目文档与当前 B 站实际响应优先。

## 1. Yotsuki2213/BiliBili_VideoRead_MCP

- 调研版本：`5ba31d0f2004cf2073f03e10118f0485d15bb92e`
- 默认分支：`master`
- 许可证：MIT
- 仓库：[Yotsuki2213/BiliBili_VideoRead_MCP](https://github.com/Yotsuki2213/BiliBili_VideoRead_MCP)

可借鉴：

- `/x/web-interface/view` 将 BV 转为 aid；
- UA、Referer、环境变量和项目外凭证文件；
- API 层、规范化与输出层分离；
- `rpid` 去重；
- `-101` 与 `12002` 的用户可读错误；
- Mock HTTP 测试。

没有采用：

- 它使用旧 `/x/v2/reply` 页码接口。2026-08-14 实测该入口只返回少量评论，不能作为完整读取依据；
- 主分页循环存在最后一个非满页可能不读取的边界；
- 楼中楼只使用根评论内嵌预览并截取前 5 条，没有调用独立分页；
- AV、纯数字和 b23 输入的 README 宣称能力与实际 BV 正则实现不完全一致；
- MCP、字幕、弹幕和扫码登录超出当前 MVP1。

## 2. FunnySaltyFish/bilibili_comments_crawl

- 调研版本：`aeac0ba2d92703fa26199a03816ff10fbb404a57`
- 默认分支：`master`
- 最近功能代码主要停留在 2023 年
- 许可证：仓库没有 LICENSE；README 另有学习交流和非商用声明
- 仓库：[FunnySaltyFish/bilibili_comments_crawl](https://github.com/FunnySaltyFish/bilibili_comments_crawl)

可借鉴：

- 主评论使用 offset 游标；
- 对每个根评论调用 `/x/v2/reply/reply`；
- `parent → children` 邻接表；
- DFS 输出多条根到叶分支；
- 分支共享前缀会在每条完整链中保留。

没有采用：

- 旧 `/x/v2/reply/main` 没有当前 WBI 签名；
- 楼中楼页数完全预先依赖 `rcount // 20 + 1`，评论变化时可能截断或多取；
- 没有跨页去重、缺父报告、root 校验或环检测；
- 空页访问首元素可能崩溃，页失败会静默提前结束；
- 412 重试耗尽后直接终止进程；
- 无许可证代码不能直接复制。

本项目独立实现了迭代父链校验、孤儿隔离、循环检测、游标环检测，以及“接口终止条件 + 计数校验 + 安全阀”的分页策略。

## 3. xiaoyaya191/bilibili_learning_bot

- 调研分支：`main`
- 调研时 README 版本：3.1.2，仓库显示 115 次提交
- 许可证：MIT
- 仓库：[xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot)

可借鉴：

- 独立 `api/` 层；
- 共享 HTTP 客户端和连接复用；
- 全局请求间隔与风控冷却；
- 网络错误和业务错误分开处理；
- Cookie 放在用户数据目录并原子写入；
- 对 `-799`、`12002` 等错误提供明确提示；
- AI、知识库、监听、回复、Web 面板等按模块拆分，为后续里程碑提供路线参考。

没有采用：

- 其 `get_hot_comments` 只读取热门评论第一页并取 `limit` 条，不是完整评论树抓取；
- 依赖 `bilibili-api-python`，与本项目“直接封装网页实际接口”的决定不同；
- 当前项目中的 AI、知识库、推荐流、私信、点赞、投币、评论回复、GUI 和自动化行为全部超出 MVP1；
- 本项目 WBI 逻辑以 2026-08-14 实际 `/x/v2/reply/wbi/main` 请求验证为准，不复制参考项目实现。

## 4. 最终采用的组合

```text
VideoRead_MCP
  └─ 视频元数据、凭证注入、Adapter/输出分层

bilibili_comments_crawl
  └─ root/parent 邻接关系、根到叶 DFS 的通用思路

bilibili_learning_bot
  └─ 请求节流、风控提示、未来模块边界

B 站 2026-08-14 实际只读响应
  └─ WBI 主评论接口、游标结构、楼中楼分页、匿名 nav 特例
```

接口事实来自实时响应；代码、测试、错误语义和输出 schema 均为本项目独立实现。

