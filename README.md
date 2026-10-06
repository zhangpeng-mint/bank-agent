# BankDev Agent

当前交付范围截止阶段 7：本地 CLI 分析 MVP，附带可选智谱模型辅助。

| 功能 | 命令 |
| --- | --- |
| 仓库范围与 Git 版本检查 | `check-config`、`snapshots` |
| 代码与知识库增量索引 | `index-code`、`index-docs` |
| 接口、符号与中文知识检索 | `search-interface`、`search-code`、`search-knowledge` |
| 上下游静态调用链与影响候选 | `trace`、`impact` |
| 知识字段冲突与代码契约对齐 | `detect-conflicts` |
| 接口分析、需求工作流、证据报告 | `analyze-interface`、`analyze-requirement` |
| 固定案例回归与模型合成测试 | `verify-golden`、`model-smoke` |

报告支持 JSON/Markdown，区分知识事实、代码事实、历史记载、用户决策、推断和待确认项。事实必须通过证据门禁；缺失证据、索引过期和覆盖不足会阻断或标为部分分析。当前主要真实验收案例是 01100121，尚无 Web UI、MCP 服务或自动业务代码修改功能。

首次拉取需复制 `config/repositories.example.toml` 为 `config/repositories.toml`，填写本机四个外部仓库的位置和实际 Git 基线。后者被 Git 忽略，避免不同电脑互相覆盖路径。`var/` 索引和报告在本机重新生成，不随仓库分发。

**Windows 用户请先阅读 [Windows 安装与使用](docs/windows-setup.md)。** 当前已在 macOS / Python 3.12 验证，尚未在 Windows 真机验收。

BankDev Agent 是面向 DFBM、DCIS 和 Fineract 的证据优先研发分析助手。当前已实现本地只读基础设施、SQLite 文档/代码增量索引、中文知识检索、字段级知识冲突检测、双向调用图、初步影响分析，以及 DFBM → DCIS → Fineract 的确定性跨仓库追踪。

阶段 6 已提供无需大模型的需求分析工作流与证据门禁，复用现有索引能力输出 JSON/Markdown 报告。详见 [阶段 6 文档](docs/baseline/09-phase6-requirement-workflow.md)。

阶段 7 已增加可选的模型辅助：`template`（默认）、`mock`（离线回归）、`zhipu`（智谱通用 API）。模型只能生成需求候选、受控检索词、事实 ID 选择和待复核建议，不能改写已确认事实或用户决策。详见 [阶段 7 文档](docs/baseline/10-phase7-model-assistance.md)。

```bash
# 完全离线的模型辅助回归，附带已验证事实上下文
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-requirement \
  '分析 01100121 贷款产品查询' --backend mock --model-context verified-facts \
  --non-goal '不修改业务代码' --acceptance '定位契约与静态链路' \
  --format markdown --output var/reports/phase7-01100121.md

# 先在当前终端安全注入 BANKDEV_MODEL_API_KEY，再执行合成数据测试
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent model-smoke --backend zhipu
```

密钥仅从环境变量 `BANKDEV_MODEL_API_KEY` 读取，不支持命令行明文密钥参数。智谱默认模型为本次实测可用的 `glm-4-flash-250414`，可用 `--model` 指定账户可用模型；使用通用 API，不自动切换 Coding 套餐端点。`--backend zhipu` 默认只发送输入的需求字段，`--model-context verified-facts` 会额外发送最多 40 条已验证事实文本及引用 ID，请仅对允许出域的资料使用。不会发送完整源码文件、本机绝对路径或整个分析报告。

模型失败会保留模板分析，原因见 `model_assistance.events`；模板分析完整时，模型降级本身不会把 CLI 退出码改为失败。`model-smoke` 则必须真实通过结构化校验才返回 0。

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-requirement \
  '分析 01100121 贷款产品查询' \
  --non-goal '不修改业务代码，不连接业务服务' \
  --acceptance '定位当前契约、静态链和证据覆盖范围' \
  --format markdown --output var/reports/phase6-01100121.md
```

默认输出 JSON。`--business-object`、`--term`、`--interface-id`、`--constraint`、`--acceptance`、`--non-goal`、`--question` 和 `--decision` 均可重复。自由文本仅原样保留并抽取八位接口号，不猜测业务规则或用户决策。请先执行 `index-code` 和 `index-docs`。

退出码：完整分析为 0；部分分析或证据阻断为 1（仍输出诊断报告）；参数/路径等错误为 2。`publishable=true` 表示事实门禁通过，部分报告仍须结合 `status`、`coverage` 和待确认项阅读。

## 环境

- Python 3.11+；当前已验证运行时为 `/opt/homebrew/bin/python3.12`。
- 阶段 1 无第三方运行依赖，不需要安装包。
- 外部 Java 仓库和知识库保持只读，不执行其中的脚本或构建。

## 快速验证

```bash
cd /Users/god/Projects/bank-dev-agent
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent check-config
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent snapshots
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent verify-golden \
  tests/golden/cases/01100121/case.json
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent analyze-interface 01100121 \
  --golden tests/golden/cases/01100121/case.json \
  --format markdown \
  --output var/reports/01100121.md
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-code
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-docs
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-interface 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-knowledge 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-knowledge "贷款产品查询"
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent detect-conflicts 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-code FineractPersonalLoanClient --repo dcis
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent trace 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent trace createLoanClient --max-depth 20
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent impact \
  com.wefi.dcis.mis.service.PersonalLoanService.openAccount
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
```

业务仓库访问只执行读取、搜索和 Git 查询；索引/报告写入本项目 `var/`，显式选择智谱后会调用模型 API。配置模板见 [`config/repositories.example.toml`](config/repositories.example.toml)，阶段 0 基线见 [`docs/baseline/`](docs/baseline/)。

## 当前边界

- 不修改 DFBM、DCIS、Fineract 或知识库。
- 不运行 Gradle/Maven，不连接数据库、Kubernetes 或业务服务。
- 阶段 4B 索引是确定性轻量解析器，不是完整 Java Symbol Solver；多实现、反射、动态路由和动态 SQL 会显式保留为 unresolved 或在后续扩展。
- SQLite 是可删除重建的派生产物，只能写入本项目 `var/`；外部仓库继续保持只读。
- 知识库受控文档当前使用 `git:<commit>` 标识；若以后出现未提交的受控文档修订，会自动降级为 `worktree-sha256`，不会冒充 Git baseline。
- 阶段 5B 仅抽取明确结构化的接口事实；未识别的自然语言不会被静默解释为确定性字段。
- 搜索未命中只能作为限定 snapshot/路径内的负向证据。
