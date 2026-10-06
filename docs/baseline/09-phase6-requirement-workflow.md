# 阶段 6：需求分析工作流与证据门禁 MVP

## 实现与复用

新增 `requirement_analysis.py`，保留 SQLite schema v4，不重新实现阶段 3–5 的解析器。`RequirementAnalyzer` 顺序调用 `KnowledgeQuery.search`、`IndexQuery.search_interface/trace/impact`、`KnowledgeConflictQuery.report`，执行以下十步并记录状态：

Intake → ScopeCheck → SearchKnowledge → LocateInterfaces → TraceCode → AnalyzeImpact → DetectConflicts → CoverageGate → DraftReport → VerifyClaims。

`RequirementFrame` 保存目标、非目标、业务对象、接口号、术语、约束、验收条件、待确认项及用户决策。模板模式只从输入提取八位接口号；其他字段由 CLI 显式输入，不推测业务规则。查询最多 20 个，每个图默认深度 8、节点 200，可用 `--max-depth` / `--max-nodes` 调整。

## Claim / Evidence 门禁

- `Claim.type` 支持 `fact`、`inference`、`question`、`recommendation`。事实必须绑定索引实体和非空 evidence ID。
- 核验重新加载实体并重建允许的事实模板；伪造实体、把正确引用套给其他事实、篡改事实分类均不通过。
- 知识事实复用阶段 5 的 evidence ID，核验活动 snapshot、字段位置是否被证据覆盖、文件 SHA-256 和证据块 SHA-256。
- 代码证据 ID 根据 repo、commit、路径、行号确定性生成，核验索引 Git blob hash 与本机文件内容一致、commit 与配置一致、路径在白名单内、行号有效。
- 如果本机引用文件已经不同于固定源码，报告阻断，不展示一个看似有效但落到错误内容的本机链接。
- 知识索引 snapshot 还会与所有受控已跟踪知识文件重新比较，防止漏检其他文档更新导致的新冲突。
- 失效事实不进入正常事实列表，阻断原因留在 `coverage.blockers`。报告中的 `verification` 反映最终声明核验；整体发布状态还必须检查 `publishable` 和 `status`。
- 当前知识、历史知识、当前代码和用户决策分开；文档状态保留在知识事实中，历史记载不冒充历史实测。

`ClaimVerifier.verify(claims, evidence_ledger)` 可重新验证报告事实。该核验限定于已有索引和受控模板，不是任意自然语言的语义证明，也不把有效引用等同于索引解析器完全正确。

## 状态与报告

| 状态 | 意义 | CLI 退出码 |
| --- | --- | --- |
| complete | 所请求查询及证据门禁完成；必要输入已提供 | 0 |
| partial | 缺索引、未命中、静态 unresolved、预算截断、缺验收/非目标或用户问题待确认 | 1 |
| blocked | 高严重度冲突、知识 snapshot 失效、证据/事实核验失败 | 1 |

参数和路径错误返回 2。诊断报告会保留供复核；`publishable=false` 的报告不能当作已确认结论发布。`complete` 仅表示本次限定范围内工作流完成，不代表业务需求已实现或运行验证通过。

JSON 保留查询命中、图、影响候选、字段对齐、四仓 snapshot、知识索引版本、配置路径/排除规则及证据目录。Markdown 按事实类别展示，并附冲突、覆盖、unresolved、用户决策、待确认问题和可点击本机证据。

未命中严格限定配置路径、所列 snapshot、查询和预算。HTTP 对齐复用阶段 5B 的精确 method/path 匹配；无精确端点仍是 unresolved，不能推断漂移。影响分析从定位入口反向遍历，属于静态候选；尚不根据自然语言自动推测要修改的业务实体。

修正了既有 `trace` 到达深度上限却不标记截断的问题，避免覆盖门禁误判。深度边界检测采用保守策略，边界仍有未访问邻居即标记截断。

## 验证

`tests/regression/test_phase6_requirement.py` 覆盖 01100121 端到端及重复结果一致性、四仓快照、5 条知识字段、7 个 Mapper、本地 DFBM 图、事实证据绑定、伪造实体、错误事实套用有效引用、无证据事实、失效 snapshot/hash/行号、历史分类、高冲突阻断、信息级漂移、缺索引、未知接口、预算截断、输入字段缺失、输出格式、CLI 退出码和 var 路径限制。测试通过篡改临时派生索引验证失效场景，不修改真实仓库。

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent snapshots
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent verify-golden tests/golden/cases/01100121/case.json
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-docs
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent detect-conflicts 01100121
```

不增加模型、网络服务或 JVM 构建依赖，不写业务仓库和知识库。SQLite 和输出报告限定于 BankDev Agent `var/`。本阶段没有可靠恢复/checkpoint、业务规则推理或运行时验证能力。

## 2026-10-06 本机验收结果

- 全量 47 项 unittest 通过（原有 33 项、新增 14 项）。
- 四仓 snapshot、01100121 Golden 和 SQLite `integrity_check` 均通过。
- 两次知识增量索引均为 47/47 缓存命中；47 文档、577 块及证据保持不变。
- 知识版本仍为 `git:f2cfbada05011c0d0f15f607feb4826d254b2526`。
- 明确提供非目标和验收条件的 01100121 分析：`complete`，40 条事实核验通过，其中 5 条为知识字段事实；阻断冲突为 0。
- 产物：`var/reports/phase6-01100121.json` 和 `var/reports/phase6-01100121.md`，源码/知识事实均可从报告内证据目录定位。
