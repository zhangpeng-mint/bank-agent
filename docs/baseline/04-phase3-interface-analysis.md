# 阶段 3：01100121 端到端确定性基准

## 1. 目标

在不调用模型、不构建 Java 项目、不连接数据库或服务的条件下，从知识库接口号导航到当前源码，并自动恢复：

```text
01100121
  → POST /dfbm-fbs/v1/loan-products
  → OpenAPI operationId queryLoanProducts
  → LoanInternalController.queryLoanProducts
  → ProductQueryService / ProductQueryServiceImpl
  → 7 个 MyBatis Mapper statement
  → 8 张 DFBM 本地产品表
  → 限定范围内未发现 DCIS/Fineract 下游调用
```

分析器先生成结构化证据图，之后才由 `BenchmarkEvaluator` 对照 Golden。Golden 不参与入口、调用或表关系的发现。

## 2. 事实分层

- **知识库事实**：接口文档当前工作树版本为 v1.1、待审核，记录 `POST /dfbm-fbs/v1/loan-products` 和“无下游”。
- **当前代码事实**：固定 DFBM commit 的 OpenAPI、Controller 方法体、Service 方法体、Mapper Java/XML 和 SQL 表名。
- **历史实测**：2026-09-29 dev 响应与数据记录，仅作为采集时观察，不证明当前部署状态。
- **用户确认决策**：DCIS 是封装层、DFBM 不直连 Fineract。
- **待确认决策**：DCIS 产品代理形态、孤儿表处置、数据源口径等，不能作为当前实现事实。

## 3. 漂移结论

用户输入中的 `GET /internal/v1/loan-products` 和 `ProductInternalController` 对应历史认知。确定性分析确认当前实现为 `POST /dfbm-fbs/v1/loan-products` 和 `LoanInternalController`；旧 GET 只在历史契约和勘误中保留。当前知识库工作树已经与当前代码对齐，因此比较结果是“当前文档与代码一致，同时检测到历史契约漂移”。

## 4. 验收维度

1. HTTP method、path、operationId Top-1 全部正确。
2. Controller、Service interface/implementation 均有文件、行号、commit 和片段 hash。
3. 自动发现 7 个 Mapper statement 与 8 张表。
4. 不产生 DFBM → DCIS/Fineract 的已确认调用边。
5. 图中所有节点和边均包含可点击本地证据。
6. 知识、当前代码、历史观察、用户确认和待确认分别输出。
7. Golden 评测必须为 PASS，伪造下游边为 0。

## 5. 执行

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-interface 01100121 \
  --golden tests/golden/cases/01100121/case.json \
  --format json \
  --output var/reports/01100121.json

PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-interface 01100121 \
  --golden tests/golden/cases/01100121/case.json \
  --format markdown \
  --output var/reports/01100121.md
```

输出只允许写入 BankDev Agent 自身的 `var/`，四个来源仓库保持只读。
