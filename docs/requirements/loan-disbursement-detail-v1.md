# 借款明细记录：简版设计 v1

日期：2026-10-13。状态：设计建议，尚未实施。

## 范围

按用户本轮要求，只设计 DFBM 借款/放款交易明细。一笔放款业务一条记录；不作为贷款全生命周期账户表，不存实时剩余本金、逾期状态或完整还款计划。公共订单表沿用当前实现，不评审、不改造；不改还款、DCIS 或 Fineract。

本稿替代上一轮方案中“借款明细”的建议；旧报告保留历史版本，不作为本次最新代码结论。

## 核对过的现状

DFBM 固定提交：`2b1b7f226efa79753eafde6ed00d422e46c9dcfe`。下面路径相对 DFBM 根目录，本次引用文件与该提交一致；其他产品映射文件仍有本机改动，未修改。

| 证据 | 路径与位置 | 当前代码事实 |
| --- | --- | --- |
| E1 | `dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/txn/service/impl/LoanDisburseServiceImpl.java:34–46` | 放款先 quoteService.bind，再调用 DCIS；结果查询也经 DCIS。此类中未见本地借款明细登记 |
| E2 | `dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/txn/service/impl/LoanQuoteService.java:88–147` | 校验报价归属、业务内容和绑定键，返回冻结的命令、金额、费用及日期 |
| E3 | `dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/txn/model/bo/LoanDisburseElementBO.java:6–42` | 已有产品、计划、币种、本金、处理费、税额、净放款额、期限与执行利率要素 |
| E4 | `dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/txn/model/vo/LoanDisburseVO.java:3–15` | 成功响应可能暂缺 loanAccountNo，不能据此判放款失败 |
| E5 | `dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/txn/model/bo/LoanDisburseResultQueryBO.java:6–28` | 查询 200 只表示查询成功；有业务流水、借据号、类型、金额、币种、冲销标记 |
| E6 | `dfbm-fbs/src/main/resources/db/migration/V28__create_order_record.sql:1–28` | 公共订单按 biz_type + biz_seq_no 关联业务明细；现有唯一键也是这两个字段 |
| E7 | `dfbm-fbs/src/main/resources/db/migration/V9__create_loan_borrow_record.sql:1–48` | 保留了旧 BAS 口径的 loan_borrow_record 迁移，含 PUSHED/UNVERIFIED、ref_nbr 等语义；迁移存在不证明当前数据库已部署 |

知识参考 K1：`02_knowledge/05_架构/2026-09-27_架构现状/一期业务与可靠性.md:36–51`，snapshot `git:060dc26a2d07b93482ee0a15bbeb016db17c116f`。该文是待审核设计草稿，关于“未知先查原流水、事务外调用下游”的记载仅作设计参考，不视为本需求已批准规则。本轮无业务运行实测。

## 一张表即可

建议表名 `loan_disbursement_record`，单独承载当前 DCIS 放款链路，不复用旧 BAS 表字段语义。不删除或迁移旧表；历史回填不在本版范围。

公共订单关联固定为：`biz_type = LOAN_DISBURSE`，`biz_seq_no = disburse_biz_seq_no`。本版不额外增加 order_no 外键，也不调整公共表的状态机。

以下均为建议字段；金额采用精确十进制，先与现有订单金额精度一致使用 DECIMAL(20,6)。标识长度实施时与当前请求契约核对。

| 字段 | 类型建议 | 来源/用途 |
| --- | --- | --- |
| id | BIGINT 主键 | 本地记录 ID |
| disburse_biz_seq_no | VARCHAR(100)，非空、唯一 | 请求放款业务流水；与公共订单关联 |
| bank_code / user_id | VARCHAR(32) / VARCHAR(64)，非空 | 请求归属；所有重放和查询均校验 |
| quote_id / config_version | VARCHAR(64) / VARCHAR(128)，非空 | 保留报价与配置版本引用 |
| dfp_id / plan_id | VARCHAR(32) / VARCHAR(64)，非空 | 绑定后的产品和期限计划 |
| principal | DECIMAL(20,6)，非空 | 申请本金，不是净到账金额 |
| currency_code | VARCHAR(8)，非空 | command.element.currencyCode |
| processing_fee_amount / tax_amount | DECIMAL(20,6)，非空 | 报价冻结的处理费和税额；合法零值保存为 0 |
| net_disbursement | DECIMAL(20,6)，非空 | 报价预计净放款额，不命名为“实际到账金额” |
| loan_term / term_unit / number_of_repayments | INT，非空 | 冻结期限数值、单位编码、还款期数 |
| interest_rate_per_period / interest_rate_frequency_type | DECIMAL(18,6) / INT，非空 | 执行利率及其周期，成对保存，不能默认当成年利率 |
| business_date | DATE，非空 | command.actualDisbursementDate：命令业务日期，不冒充到账时间 |
| loan_account_no | VARCHAR(100)，可空 | 成功响应或结果查询回填的借据号 |
| status | VARCHAR(16)，非空 | PROCESSING / SUCCESS / FAIL / UNKNOWN；本表自己的放款处理状态 |
| manually_reversed | BOOLEAN，可空 | 结果查询的冲销标记；未知不填 false，冲销不改写为“原交易失败” |
| result_code / result_message | VARCHAR(64) / VARCHAR(512)，可空 | 最近业务结果或待核验原因；消息限制长度并脱敏 |
| created_at_utc / updated_at_utc | DATETIME(3) | 记录创建及更新时间 |

**索引只保留三个：**主键 id、唯一键 disburse_biz_seq_no、普通索引 (bank_code, loan_account_no)。首版不另做明细列表，用户订单列表继续由公共订单负责。

全局放款流水唯一是为匹配当前公共表的关联方式所采用的设计假设；同号不同银行/用户一律拒绝，不能直接回放别人的记录。若实际编号只在银行内唯一，实施前需确认编号契约，本稿不擅自修改公共表。

不另存完整请求 JSON、全部产品配置或完整还款计划；更详细的执行参数通过 quote_id 对应的冻结报价追溯。因此已绑定报价及其快照应与明细一起保留，不能按普通过期报价直接清理。

## 写入流程

1. **校验通过后、发送 DCIS 前**：复用 quoteService.bind；用返回的冻结 command 创建 PROCESSING 明细。字段从冻结要素取，不重新读取产品当前配置。明细写失败则本次不调用 DCIS；已绑定报价允许原键恢复。
2. **重复请求**：在已有报价归属和同键同内容校验通过后查询明细。SUCCESS 回放；PROCESSING/UNKNOWN 引导结果查询；FAIL 首版不自动再次发送。并发插入以唯一键兜底，只有成功创建记录的一方继续原有下游调用，另一方读取现状。
3. **调用返回**：已有 DCIS 放款成功语义对应 SUCCESS，借据号可暂空；确认未执行的业务拒绝记 FAIL；超时、断网、5xx 或是否已执行不明记 UNKNOWN。重复业务冲突也须先核对原结果，不统一按 4xx 判失败。
4. **结果查询回填**：沿用 queryDisburseResult。核对归属、原业务流水、Disbursement 类型、金额与币种，且 manuallyReversed 明确为 false 后，按已确认契约将未决记录推进成功并补借据号；查询 404 或关键字段缺失仍待核验。若返回冲销标记为 true，单独记录并提示“已冲销/需核查”，不继续展示为当前有效放款，也不触发重放。
5. **本地结果保存失败**：不能把已知下游成功改为 FAIL；保留原记录，后续通过原流水查询修复。下游调用放在本地事务之外，避免把数据库事务锁持有到 HTTP 返回。

只有未决状态允许常规推进，重复查询幂等；成功状态不能被迟到的超时/失败结果覆盖。崩溃遗留的 PROCESSING 也走原流水结果查询，不自动重新发送。首版复用结果查询完成收敛，不另建定时任务、事件中心或通用交易框架。公共订单继续走现有服务，本稿不承诺或重设两表原子性。

## 最小改动范围与检查

- 新增一份数据库迁移、一个 DO、一个 Mapper/XML；不修改 V9 旧迁移，不提前指定可能冲突的迁移编号。
- 在 LoanDisburseServiceImpl 增加明细登记、结果更新、重复请求判断；在 queryDisburseResult 增加核对及回填。需要时抽一个 LoanDisbursementRecordService，避免把状态判断放进 Controller。
- 保留现有请求/响应契约，不新建公共订单 API、不改还款。
- 最少验证：正常成功、成功但借据号为空、明确失败、超时后查成功、重复/并发同号、同号异用户/异内容、查询 404、查询到冲销、下游成功后本地保存失败。

本次仅更新设计文件，未生成或执行业务迁移，未运行 Gradle/Maven 或连接业务服务。
