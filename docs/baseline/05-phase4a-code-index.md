# 阶段 4A：SQLite 增量代码索引

## 目标与边界

阶段 4A 把阶段 3 的单接口即时扫描扩展为可复用的本地派生索引。索引仅读取
`config/repositories.toml` 锁定的 Git commit，不执行 Java 项目、不连接数据库或业务服务，也不修改
DFBM、DCIS、Fineract 和知识库。唯一写入是 BankDev Agent 自身 `var/` 下的 SQLite 文件。

## 数据模型

SQLite schema 版本为 `1`：

| 表 | 用途 |
| --- | --- |
| `schema_meta` | schema 版本门禁 |
| `index_runs` | 每次索引的状态、范围和统计 |
| `repository_snapshots` | 已索引仓库与固定 commit |
| `files` | snapshot 文件、Git blob hash、语言和本次缓存状态 |
| `parse_cache` | `(blob_hash, language, parser_version)` 对应的解析结果 |
| `symbols` | Java 类型、方法及字段/调用元数据 |
| `endpoints` | OpenAPI method、path、operationId |
| `mapper_statements` | MyBatis namespace、statement 和 SQL 类型 |
| `sql_tables` | SQL 中静态识别的本地表 |
| `edges` | `IMPLEMENTED_BY`、`CALLS`、`READS`、`WRITES` 证据边 |
| `interface_aliases` | 知识库原服务号到当前 HTTP 契约的映射 |

所有事实带仓库、commit、文件和行号。知识库别名记录当前文档内容的 SHA-256，避免把未提交文档误标为
Git commit 事实。

## 增量策略

1. 使用 `git ls-tree -r -l <commit>` 获取固定 snapshot 的路径、blob hash 和大小。
2. 工作区文件的 Git blob hash 与 snapshot 完全一致时直接读取，否则通过
   `git show <commit>:<path>` 读取，因而不会混入未提交源码。
3. 命中 `parse_cache` 时跳过 Java/OpenAPI/MyBatis 解析。
4. 当前 snapshot 的派生节点和边在事务中重建；缓存跨运行保留。
5. 查询时只使用与当前配置 baseline commit 相同的 snapshot；发现已索引 commit 漂移则拒绝查询。

阶段 4A 使用确定性轻量解析规则，不把结果冒充完整编译器 AST。反射、运行时路由、动态 SQL 和未配置源码
仍属于明确限制。

## CLI

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-code
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-interface 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent trace 01100121
```

可以用重复的 `--repo dfbm` 限定索引仓库；`--db` 可选择 `var/` 下的另一个数据库。出于只读边界，CLI
拒绝把数据库写到 `var/` 之外。

## `01100121` 验收基准

当前索引能从知识库别名定位 `POST /dfbm-fbs/v1/loan-products`，并追踪为：

```text
OpenAPI operation
  -> LoanInternalController.queryLoanProducts
  -> ProductQueryServiceImpl.queryLoanProducts
  -> 7 个 MyBatis statement
  -> 8 张 DFBM 本地表
```

验收结果为 18 个节点、17 条证据边，且没有生成 DCIS/Fineract 调用边。每个节点和边均返回可点击的本地
文件行号证据。
