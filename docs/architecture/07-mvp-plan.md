# MVP 范围、结构与四周计划

## 1. 最小技术栈

- Python 3.11+、标准库 `sqlite3`、Pydantic（边界模型）、Typer/argparse（CLI）。
- SQLite + FTS5：元数据、文档块、符号、关系、checkpoint 和审计。
- `rg`：候选召回；Tree-sitter Java：结构抽取。
- 模型提供商适配层：默认支持“禁用模型/模板模式”，授权后接入兼容 API。
- pytest：单元、golden、契约和回归测试。
- FastAPI 与 MCP Python SDK：核心稳定后作为可选适配层，不让业务逻辑依赖它们。

所有实际依赖版本在实施时根据组织允许清单锁定；本轮不安装任何依赖。

## 2. 建议项目结构

```text
bank-dev-agent/
├── pyproject.toml
├── README.md
├── config/
│   ├── bankdev.example.toml
│   ├── repositories.example.toml
│   └── redaction-rules.example.toml
├── docs/architecture/
├── knowledge/                  # Git 管理的规范知识
├── src/bankdev_agent/
│   ├── domain/                 # Evidence、Claim、Graph、Snapshot
│   ├── application/            # search/trace/impact/analyze 用例
│   ├── ingestion/              # 文档与代码增量索引
│   ├── retrieval/              # FTS、精确查找、融合排序
│   ├── code_analysis/          # rg、AST、Spring、MyBatis、图遍历
│   ├── workflow/               # 单编排器和报告门禁
│   ├── security/               # scope、脱敏、注入隔离、审计
│   ├── model_gateway/          # 本地/云模型策略适配
│   └── adapters/
│       ├── cli/
│       ├── http/               # 可选 FastAPI
│       └── mcp/                # 可选 MCP
├── var/                        # gitignored：索引、审计、缓存
├── tests/
│   ├── fixtures/synthetic_bank/
│   ├── unit/
│   ├── contract/
│   ├── golden/
│   └── regression/
└── scripts/                    # 明确、可复现的索引/评测入口
```

`knowledge/` 与 `var/` 分离：前者是可审阅事实源，后者是可删除重建的派生物。真实仓库在配置中登记，不复制进项目。

## 3. 核心接口与数据结构

```text
DocumentIngestor.ingest(source, snapshot) -> IngestReport
CodeIndexer.index(repo_id, commit) -> IndexReport
KnowledgeSearch.search(SearchQuery) -> SearchResult
InterfaceService.get(InterfaceRef) -> InterfaceResult
TraceService.trace(TraceQuery) -> TraceResult
ImpactService.analyze(ImpactQuery) -> ImpactResult
RequirementAnalyzer.run(AnalysisRequest) -> AnalysisReport
EvidenceVerifier.verify(AnalysisDraft) -> VerificationReport
```

关键模型：

- `SourceSnapshot(id, kind, git_commit, content_hash, created_at)`
- `Evidence(id, source_id, snapshot_id, location, excerpt_hash, classification, access_level)`
- `Claim(id, text, type, evidence_ids, reasoning, status)`
- `GraphNode/GraphEdge`（见 `04-code-analysis.md`）
- `Coverage(queries, visited_nodes, unresolved_count, truncated, limitations)`

## 4. 配置原则

TOML 配置只存逻辑信息：repo ID、允许根目录、默认 branch/commit 策略、知识库目录、索引文件、模型策略、预算和日志保留期。密钥只从 OS keychain/受控环境注入，禁止出现在 TOML、Git、命令行参数和日志。

启动时验证：目录存在且为允许类型、真实路径没有逃逸、索引 schema 兼容、SQLite FTS5 可用、配置无明文秘密、云模型策略与资料密级兼容。

## 5. 索引和工作流

文档按结构切分并写入 `document/chunk/entity` 和 FTS 表；代码写入 `repository/file/symbol/edge/endpoint/mapper`。两者共享 snapshot 和 evidence 表。索引流程可重复且幂等，以 hash 增量更新。

需求工作流执行“解析需求 → 检索文档 → 定位接口 → 追踪代码 → 影响/冲突 → 生成草稿 → 引用校验 → 报告”。每步保存结构化状态，模型失败时仍可输出检索与追踪结果。

## 6. 四周研发计划

### 第 1 周：证据化知识检索

- 目标：用模拟/获授权资料完成可重复的文档入库与检索。
- 任务：领域模型、SQLite schema/迁移、Markdown 解析、front matter 校验、FTS5 中文策略、增量 hash、CLI、引用 URI、安全路径。
- 产出：`index-docs`、`search` CLI；至少 30 个合成文档块和查询集；索引报告。
- 验收：精确接口号 Recall@5=100%；业务查询 Recall@5≥80%；所有结果带有效出处；重复入库不产生重复记录；越权路径被拒绝。
- 难点：中文分词、表格切分、版本冲突。

### 第 2 周：Java 定位与初步调用链

- 目标：在合成的三仓库 fixture 上恢复已知入口和跨层链路。
- 任务：rg adapter、Tree-sitter 索引、Spring/MyBatis 规则、符号/边 schema、跨仓库签名、预算图遍历、unresolved 报告。
- 产出：`index-code`、`search-code`、`trace` CLI 和 golden graphs。
- 验收：fixture 的入口 Top-3=100%；确认边 precision≥95%、recall≥85%；每条边有代码位置；反射/多实现用例不被误标为确认。
- 难点：常量路由、重载、多实现、XML 动态 SQL。

### 第 3 周：需求分析与 MCP

- 目标：从新需求生成可核验的完整报告，并让外部 Coding Agent 复用工具。
- 任务：显式工作流、模型网关/模板 fallback、影响分析、claim-evidence 门禁、Markdown 报告、四个 MCP 工具、stdio 契约测试；按需增加 FastAPI。
- 产出：`analyze` CLI、报告模板、MCP 配置样例与客户端说明。
- 验收：所有事实 claim 引用覆盖率=100%；伪造实体=0；MCP 与 CLI 同查询结果一致；超时产生 partial 而非错误结论。
- 难点：检索闭环、上下文预算、证据与推断隔离。

### 第 4 周：真实脱敏回归与硬化

- 目标：用 3 类已完成历史需求验证实用性并修正阈值。
- 任务：构建 gold set、盲测、错误分类、脱敏/注入测试、性能测试、备份恢复、使用文档；仅在授权后接入真实脱敏资料。
- 产出：评测报告、失败案例库、风险清单、v0.1 发布候选。
- 验收：达到 `08` 指标门槛；3 个历史需求均能生成可审阅报告；索引可全量重建；高风险安全用例通过；开发者评分≥4/5。
- 难点：gold 标注一致性、真实项目动态特性、数据授权。

## 7. MVP 完成标准

三个核心能力均有 CLI 和自动化回归；索引可从空目录重建；报告中的事实 100% 绑定可解析证据；能够显示已确认/推断/未知；失败和截断透明；不连接生产库、不写业务仓库、不外传未授权资料；至少一名实际开发者用历史需求完成盲评并达到既定门槛。
