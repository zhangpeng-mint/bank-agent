# 阶段 4B：跨仓库代码图与影响分析

## 实现范围

阶段 4B 在阶段 4A 固定 Git snapshot 索引上增加：

- 类、方法、qualified name、路由和 operationId 的统一 `search-code`。
- `trace --direction downstream|upstream|both`。
- 基于反向证据图的 `impact`，区分直接影响和传递候选。
- JAX-RS `@Path`、HTTP method 注解和 OpenAPI `operationId` 入口识别。
- consumer contract 与 provider endpoint 的跨仓库协议签名关联。
- `INVOKES_REMOTE` 确认边及 `unresolved_relations` 明细。
- 多实现、缺失实现、Mapper 歧义不再静默处理。

SQLite schema 升级为版本 2。旧版本 1 数据库会在本地执行兼容迁移；代码解析器版本升级为
`phase4b-regex-v4`，因此首次运行会重新解析，后续仍按 Git blob hash 增量命中。

## 跨仓库确认规则

远程边只有同时满足以下条件才标记为 `confirmed_static`：

1. 调用点来自生成 API 字段调用，或显式 `getJson/postJson` 路径调用。
2. 调用仓库存在对应 consumer OpenAPI contract。
3. 另一仓库存在 method、route、operationId 一致的 provider endpoint。
4. 目标唯一。

只按名称相似、注释描述或仓库名称不会建立确认边。目标缺失或不唯一会写入
`unresolved_relations`，并随 trace 返回。

Fineract provider 的 JAX-RS 路由包含 `/v1`，而 DCIS base URL 已包含 `/api/v1`、业务代码调用
`/clients`。匹配器只允许这一种显式版本前缀归一化，同时仍要求 HTTP method 和 operationId 一致。

## 第二条 Golden 链路

入口：`createLoanClient`，当前固定 snapshot 得到 14 个节点、13 条确认边：

```text
DFBM POST /dfbm-fbs/v1/loan-accounts
  -> LoanInternalController.createLoanClient
  -> LoanClientServiceImpl.createLoanClient
  -> DcisLoanClientSaoImpl.createClient
  -> DcisClient.createClient
  => DCIS POST /dcp-dcis/v1/loan-accounts
  -> PersonalLoanController.openAccount
  -> PersonalLoanService.openAccount
  -> FineractPersonalLoanClient.createClient
  => Fineract POST /v1/clients
  -> ClientsApiResource.create
  -> Fineract 内部命令处理链
```

其中两条 `=>` 是由 consumer/provider 协议签名确认的 `INVOKES_REMOTE`。该完整链路没有 unresolved。
原 `01100121` 负向基准仍为 18 个节点、17 条边，只包含 DFBM 本地查询链。

## CLI

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-code FineractPersonalLoanClient --repo dcis
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent trace createLoanClient --max-depth 20
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent trace \
  com.wefi.dcis.mis.service.PersonalLoanService.openAccount --direction upstream
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent impact \
  com.wefi.dcis.mis.service.PersonalLoanService.openAccount
```

`impact` 是静态依赖候选，不等于最终业务影响结论。直接调用方列为直接影响，继续向上的节点列为
传递候选；必须结合具体变更内容复核。

## 已知限制

- 当前仍是确定性轻量 Java 解析器，不是完整 Symbol Solver。
- Spring Profile、Qualifier、反射、SPI、动态 URL 和配置驱动路由可能保留为 unresolved。
- DTO 字段级转换、常量表达式求值和测试入口识别仍可继续增强。
- “未找到”只表示在配置的 snapshot、路径和遍历预算内未找到。
