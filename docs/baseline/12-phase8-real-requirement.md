# 阶段 8：真实需求案例回放与差距分析

日期：2026-10-13。首例：统一订单 + 放款/还款各自明细。

本阶段交付 Agent 的案例回放入口和真实需求分析，不实施 DFBM Java/DDL 变更。三个业务仓库及知识库保持只读，不运行 JVM 构建、数据库、Kubernetes 或业务接口。

## 用户确认与分析输入

- 公共订单由 DFBM 维护；DCIS/Fineract 保持现有职责。
- 本期接入放款、还款；充值、提现、转账、支付后续接入。
- 公共订单承载共性字段，各交易明细保留专有字段。
- 统一订单查询为后续规划，本轮只分析扩展所需条件。

案例文件 `tests/cases/unified-loan-orders.json` 保存用户原意、已确认决策、分析约束、候选验收条件和待确认问题。候选验收条件明确标注“建议”，不冒充用户已批准规则。

## 新增可复用能力

`analyze-case` 从严格 JSON 案例读取 RequirementFrame 与四仓完整 SHA；复用阶段 6 的检索、trace、impact、冲突检测及 ClaimVerifier。默认不联网、不调用模型。

```bash
PYTHONPATH=src python3 -m bankdev_agent analyze-case \
  tests/cases/unified-loan-orders.json --build-index \
  --max-depth 12 --max-nodes 300 \
  --output var/reports/phase8-unified-loan-orders.json

PYTHONPATH=src python3 -m bankdev_agent analyze-case \
  tests/cases/unified-loan-orders.json --max-depth 12 --max-nodes 300 \
  --format markdown --output var/reports/phase8-unified-loan-orders.md
```

首次构建专用 `var/cases/unified-loan-orders/index.sqlite3`；后续可复用或增量重建。禁止使用默认 `var/bankdev.sqlite3`，不修改原配置、原 01100121 Golden 或各仓工作树。保留配置中的白名单和已登记修改清单，未登记的工作树修改仍进入差异报告。其他电脑须先配置真实仓库路径，并具备案例 SHA 对应对象。

代码索引读取案例固定 commit；知识索引沿用阶段 5 的当前受控文档机制。若实际知识 revision 不等于案例固定 commit，案例结果阻断，不把当前知识冒充历史知识。暂不支持历史知识文件检出回放。

报告额外记录案例 ID、规范化输入 hash、原配置和案例版本。不能由案例传入可执行命令、仓库根路径、事实或解除门禁的开关。partial/blocked 沿用 CLI 退出码 1，不等同程序崩溃。

## 本次版本与门禁结果

| 仓库 | 原基线 | 本次固定源码/知识版本 |
| --- | --- | --- |
| DFBM | 44f59fabb2235f612fc470b49b18a0d551a9b995 | b1b929fb71617aa9a324791d312b0bb47096a8f1 |
| DCIS | 350fbdae3506f79bc1a1cf99afeb7f32680db415 | 4d02edad8e455506aeba0ee40dcd48f97076bf7b |
| Fineract | d5636847ac556c30b437254c353f05526d172b97 | d5636847ac556c30b437254c353f05526d172b97 |
| 知识库 | f2cfbada05011c0d0f15f607feb4826d254b2526 | 060dc26a2d07b93482ee0a15bbeb016db17c116f |

首次实际回放：239 条事实通过模板核验，13 个入口候选，52 个活动知识文档；整体为 `partial`，不能视为实现验收通过。剩余项包括 DFBM 未登记工作树修改、知识库领先本地 origin/main 一次提交、静态 unresolved、无匹配字段事实及待确认问题。未更新远端引用，不将本机 origin/main 当作实时 GitHub 状态。

此次代码观察的 7 个关键文件逐一与固定 Git blob 比较一致；DFBM 另有产品映射等本机修改，未覆盖、还原或提交。

## 发现的 Agent 能力边界

1. 广泛术语如“订单”“幂等”会召回带很多接口号的知识块，扩大字段冲突检查并达到预算。报告如实保留 `conflict_query_budget_exhausted`，不能声称检查完所有相关规则。
2. 自然语言产品规则尚未自动转换为受控事实；搜索命中不等于已纳入事实报告。人工阅读的知识草案单列在业务分析稿。
3. trace 能给出调用和表引用，无法自动证明“每笔交易必有且只有一行公共订单”，也无法证明事务、并发和恢复行为。需人工差距分析及后续业务测试。
4. 本例结果的静态事实数不是业务质量评分。未来应扩展不同类型的真实案例并进行人工验收。

业务分析稿见 `docs/requirements/unified-loan-orders.md`。其中人工推断和建议未自动获得 ClaimVerifier 的事实认证。

## 验证记录与剩余任务

- 新增案例入口单元测试覆盖严格输入、重复 JSON key、固定 SHA 与路径隔离、原配置不变、默认索引保护、partial 不提升、知识版本不符阻断、CLI 诊断输出及退出码。
- 最终定向验证：新增 6 项案例测试与既有 10 项 Web 测试全部通过；案例 SQLite integrity_check 为 ok，JSON/Markdown 内容一致。
- 真实 JSON 与 Markdown 回放均执行，返回退出码 1，与报告 partial 状态一致；未调用模型。
- 全量测试实际运行 78 项，10 个失败、3 个错误（含旧接口分析类 setUpClass 失败，导致该类未执行）。该次运行在新增 CLI 测试之前。错误集中在原真实仓库基线、旧文件位置/hash、知识版本及其派生的 complete/model_assisted 断言；不能继续宣称此前 77 项基线在当前环境全绿。
- 原 01100121 Golden 保留历史含义。后续需要建立隔离的历史 fixture，或另建经评审的当前版本 Golden；不修改断言掩盖工作树漂移。
- 尚未进行业务 Java/SQL 实现与运行测试；当前报告是开发评审输入。
