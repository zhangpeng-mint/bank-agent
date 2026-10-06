# 阶段 7：模型辅助需求理解与报告生成

## 架构

新增 `model_gateway.py`（有界 HTTP 调用、结构化输出契约、敏感内容检查、审计）和 `model_analysis.py`（模型辅助编排）。继续复用阶段 6 的确定性工作流、事实模板和 `ClaimVerifier`，SQLite schema 保持 v4，无第三方运行依赖。

入口为 `analyze-requirement --backend template|mock|zhipu`。模板是默认模式，mock 不联网，智谱通过固定 HTTPS 通用端点调用，禁止跟随重定向。密钥仅来自 `BANKDEV_MODEL_API_KEY`，不放入配置、报告、命令行参数或测试 fixture。

## 输入、输出和信任边界

1. Intake：模型收到用户输入的 RequirementFrame，返回 `proposals` 和 `queries`。提案包含字段、值、原文引用和 explicit/inference 分类。explicit 必须引用连续原文，但这只是位置校验，不能证明语义正确；所有候选保留在单独区域，不自动覆盖用户输入。
2. 检索建议：最多 5 个检索词，经类型/长度校验后只传给已有只读知识/接口检索，每项最多 3 条命中。补充结果不直接扩展主分析图或提升覆盖状态；需要用户确认范围后再次分析。
3. Draft：模型可选择已有 fact ID，并生成 `inference`、`question`、`recommendation`。不允许自由撰写 `fact`、指定用户决策、路径、链接或工具调用。
4. 引用验证：只能引用本次上下文提供的事实及 evidence ID。推断必须包含证据、理由和反证条件。引用有效不等同于语义成立，全部模型建议带 `requires_review=true`。
5. 最终重新运行 `ClaimVerifier`。模型不能将 partial/blocked 提升为 complete，也不能解除冲突和证据阻断。

模型候选无需“自动采纳”即可供需求梳理与报告阅读使用。验收条件、非目标和用户决策仍由显式输入决定；不会让模型补全这些字段来绕过覆盖门禁。

## 上下文策略

- `requirement-only`：默认，仅发送输入需求字段；不发送知识或代码事实。模型可以提出问题和通用建议。
- `verified-facts`：显式选择后，额外发送最多 40 条已通过门禁的事实文本、分类、fact ID 和 evidence ID。不发送证据完整文件、路径、Git 远程地址或完整报告。主报告被阻断时不发送事实、不进行 Draft 调用。
- 本阶段提供关键字/密钥形态、联系方式、长数字、连接串、路径和 URL 的保守拦截，不能替代完整密级识别与脱敏系统。受限资料继续使用 template/mock；使用远程事实上下文前需确认资料允许出域。

## 预算、降级和审计

每次分析最多 2 次模型调用，无自动重试；输入最多 24,000 字符，响应最多 128,000 字节，默认输出 2,048 token，每次 HTTP timeout 默认 30 秒（允许 1–60 秒）。拒绝截断 completion、重复 JSON key、NaN、错误字段/类型、未知引用和工具调用。`model-smoke` 使用固定合成需求，完全不读取业务证据。

HTTP、格式、引用或敏感内容检查失败时丢弃本次模型结果，保留确定性报告。HTTP 错误只记录状态码和数字供应商错误码，不记录原始错误响应。审计位于报告 `model_assistance.events`，包含 backend、model、prompt 版本/hash、输入 hash/字段/字符数、实际提供的 evidence ID、输出 hash、耗时、用量和固定错误码，不保存完整 prompt、原始响应或密钥。

`status` / `ok` 保留确定性分析含义，模型辅助状态单独标记 accepted/fallback；模板完整且模型失败时仍可退出 0。`model-smoke` 若未得到合法模型输出则退出 1，不把降级冒充真实联通成功。

## 使用

```bash
# 智谱合成需求测试：调用前将密钥安全注入当前进程环境
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent model-smoke \
  --backend zhipu --model glm-4-flash-250414 --output var/reports/phase7-zhipu-smoke.json

# 离线、可重复的完整事实辅助流程
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-requirement \
  '分析 01100121 贷款产品查询' --backend mock --model-context verified-facts \
  --non-goal '不修改业务代码' --acceptance '定位契约与静态链路' \
  --format markdown --output var/reports/phase7-01100121.md
```

模型可以通过 `--model` 更换，首次选用时需核对官方能力和账户权限。默认 `glm-4-flash-250414`。GLM-5.3 不支持关闭思考，因此适配器不会为该系列发送 `thinking.type=disabled`；GLM-4.5/4.7 等带点号系列使用关闭思考参数以控制输出预算。实际思考内容不进入报告。

## 测试范围

阶段 7 回归覆盖模型加入前后事实集合和用户输入不变、需求上下文不带源码事实、部分分析不被升级、伪造 fact/evidence ID 被拒绝、模型异常回退、输出结构错误、无依据推断、用户决策注入、非法字段/工具注入、输入响应调用预算、无密钥、HTTP/超时错误、重定向、截断 completion 和审计不泄露密钥。

这验证的是程序权限和证据边界，不宣称已经解决所有自然语言提示注入，也不宣称模型生成的开发建议可免审执行。

## 2026-10-06 实测记录

- 原有 47 项测试基线通过；最终全量 66 项测试通过，随后新增的用量记录修复通过全部 14 项网关单元测试（含该新增用例）。合计 67 个不同测试用例已通过，本阶段新增 20 项。
- 01100121 mock 端到端：`complete` / `model_assisted`，40 条事实核验通过，与模板模式事实集合一致。产物为 `var/reports/phase7-01100121.json` 和 `.md`。
- 真实智谱合成测试模型：`glm-4-flash-250414`，3 轮 Intake 均通过，Draft 2 轮通过、1 轮因 `invalid_output_fields` 被拒绝并降级。因此完整流程为 2/3 通过，不能宣称模型输出始终可靠。
- 本次三轮 Intake 平均耗时 5.495 秒、最慢 5.822 秒；Draft 平均 10.996 秒、最慢 12.603 秒。样本仅为合成测试，不能外推真实需求质量；没有模型生成的自由文本被采纳为 fact。
- 三轮结构化结果与审计保存在 `var/reports/phase7-zhipu-evaluation.json`；首次单步成功联通记录为 `var/reports/phase7-zhipu-smoke-flash250414.json`。未发送私有源码或知识库内容。
- 初始 GLM-5.3 关闭思考参数不兼容已修正；GLM-4.7-Flash 测试返回 `1305`（服务繁忙），没有自动循环重试。
- 四仓 snapshot、01100121 Golden、SQLite 完整性检查均通过；凭证形态扫描未在项目源码、测试、配置、文档和报告中发现密钥字面量。
- 密钥仅为联通测试临时注入进程环境，结束后清除；未持久化。后续使用需在调用进程环境设置 `BANKDEV_MODEL_API_KEY`。

真实模型失败的原始输出不保存；本次第三轮失败记录在修正前未保留 token 用量，因此三轮日志不能用于计算完整费用。现已修正为先记录供应商用量再做输出校验，并加入回归测试。

## 官方协议依据

- [智谱 HTTP API](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)：固定通用端点、Bearer 鉴权。
- [结构化输出](https://docs.bigmodel.cn/cn/guide/capabilities/struct-output)：`response_format=json_object`，程序另行做严格字段校验。
- [模型概览](https://docs.bigmodel.cn/cn/guide/start/model-overview)：模型代码与可用能力。
- [错误码](https://docs.bigmodel.cn/cn/faq/api-code)：`1210` 为参数错误，`1305` 为模型访问繁忙。
