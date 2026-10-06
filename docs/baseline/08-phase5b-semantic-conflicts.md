# 阶段 5B：字段级知识冲突检测

## 范围

阶段 5B 在阶段 5A 的文档快照上增加确定性字段事实层。它从主接口文档中抽取带精确行号的结构化事实，比较不同文档的当前值与历史值，并把当前 HTTP 契约与固定 commit 的代码端点做精确匹配。

当前支持以下字段：

- `http_method`
- `route`
- `success_status`
- `request_schema`
- `downstream_dependency`

抽取只接受稳定表格或明确的 schema 句式，不使用模型猜测自由文本。未抽取到的字段保持未知，不能据此判定文档与代码一致或冲突。

## SQLite schema v4

新增两张派生表：

| 表 | 作用 |
| --- | --- |
| `document_claims` | 保存接口号、字段、规范值、原值、当前/历史作用域、文档状态、证据 ID 和精确行号 |
| `semantic_conflicts` | 保存字段冲突类型、严重度、候选值和关联 claim ID |

文档快照切换时，claim 和 conflict 通过外键级联替换，不会跨活动 snapshot 混合。解析器版本升级为 `markdown-structure-v4`，旧 schema 1/2/3 数据库可就地迁移到 schema 4。

## 冲突规则

1. 同一接口同一字段存在多个不同的当前值：`active_field_conflict`，严重度 `high`，命令返回失败状态。
2. 当前值与历史文档值不同：`historical_drift`，严重度 `info`，用于解释演进，不阻断验收。
3. 当前文档的 method/path 与当前代码 endpoint 精确一致：`aligned`。
4. 找不到精确代码 endpoint：`unresolved_no_exact_endpoint`，不能自动宣称代码漂移。

历史状态根据文档 status 中的“历史/废弃/superseded/obsolete/deprecated”识别。`待审核`不会被提升为已生效，只作为当前候选事实并完整保留原状态。

## CLI

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-docs
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent detect-conflicts 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent detect-conflicts
```

指定接口号时返回该接口的 claims、字段冲突和代码对齐证据；不指定时返回整个活动知识快照的检测结果。`search-knowledge <八位接口号>` 也会附带该接口的 `semantic_conflicts`。

## `01100121` 验收结果

- 抽取 5 个当前字段事实。
- HTTP 契约为 `POST /dfbm-fbs/v1/loan-products`。
- 请求 schema 为 `LoanProductQueryRequest`，成功状态为 `200`，文档声明查询链无下游依赖。
- 当前活动知识文档之间没有字段冲突。
- method/path 与 DFBM `44f59fabb2235f612fc470b49b18a0d551a9b995` 的 `queryLoanProducts` OpenAPI endpoint 精确一致。
- 每个 claim 和代码对齐结果均带可点击文件证据。

阶段 5B 不尝试理解所有自然语言规则，也不把“未识别”解释为“没有冲突”。响应字段逐字段 schema 比较、规则表达式归一化和模型辅助候选提取可在后续阶段扩展。
