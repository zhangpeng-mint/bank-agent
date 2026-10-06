# 阶段 0：真实项目接入基线

## 1. 盘点结论

采集日期：2026-09-30（Asia/Shanghai）。本次只执行文件枚举、Git 只读查询、关键词搜索和源码阅读；未修改四个外部仓库，未安装依赖、未构建项目、未连接数据库或服务。

真实工作区由四个独立 Git 仓库组成：

| ID | 职责 | 本地目录 | 分支 / HEAD | 状态 |
|---|---|---|---|---|
| `dfbm` | MINT 上层银行业务系统 | `/Users/god/Projects/mint/dfbm/mint-dfbm` | `master` / `44f59fabb2235f612fc470b49b18a0d551a9b995` | 与 upstream 无偏离；存在用户既有改动 |
| `dcis` | DFBM 与核心之间的封装层 | `/Users/god/Projects/mint/dcis/dcp-dcis` | `main` / `350fbdae3506f79bc1a1cf99afeb7f32680db415` | 与 upstream 无偏离；存在用户既有未跟踪文件 |
| `fineract` | 银行核心系统 | `/Users/god/Projects/mint/fineract/dcp-fineract` | `1.15.0` / `d5636847ac556c30b437254c353f05526d172b97` | 干净，与 upstream 无偏离 |
| `bank_knowledge` | Git Markdown 知识库 | `/Users/god/Projects/mint/my_knowledge/bank-knowledge` | `main` / `f2cfbada05011c0d0f15f607feb4826d254b2526` | 与 upstream 无偏离；仅存在未跟踪的本机元数据文件 |

DFBM 的既有改动为 `.sdkmanrc` 新增和 `README.md` 修改；DCIS 有未跟踪 `.sdkmanrc`；知识库仅有未跟踪 `.DS_Store`、`02_knowledge/.obsidian/*.json`。知识库的 macOS Python 3.12 MCP 配置和按当前 DFBM 契约校正的知识文档已于 2026-10-06 以 `f2cfbad` 提交并推送。本项目不得还原、覆盖或混入其余既有文件；本机元数据不会进入内容索引。每次分析都要重新记录工作树状态。

阶段 1 开发期间检测到三个仓库相对阶段 0 的 HEAD 已前进。本文和配置已在重新核对后更新到上表 snapshot；这次漂移也验证了 snapshot 门禁能够阻止旧 Golden 静默通过。

只读接入配置见 [`config/repositories.toml`](../../config/repositories.toml)。配置拒绝任意路径、写操作、项目代码执行、数据库/服务连接和 push。

## 2. 实际规模与模块

| 仓库 | 追踪文件 | Java | YAML/YML | XML | 主要模块 |
|---|---:|---:|---:|---:|---|
| DFBM | 433 | 331 | 6 | 23 | `common`、`dfbm-fbs`、KE/TZ country extensions |
| DCIS | 130 | 60 | 4 | 6 | `common`、`dcis-mis` |
| Fineract | 7,772 | 6,507 | 61 | 416 | `fineract-provider`、`fineract-core`、`fineract-loan`、`fineract-savings`、`fineract-security` 等 |
| 知识库 | — | — | — | — | `02_knowledge` 有 47 份 Markdown/TXT，其中 46 份非演示资料 |

Fineract 明显大于另外两个项目。MVP 默认只索引与当前需求相关的主模块和 `src/main`，通过接口/业务词逐步扩展；不立即建立全仓深层调用图。

## 3. 已确认的项目关系

知识库记录的目标方向是 MINT/渠道 → DFBM → DCIS → Fineract；2026-09-29 用户确认 DCIS 是封装层，同时确认贷款产品数据路径不允许 DFBM 直连 Fineract。但资料同时声明目标方向不等于所有链路已经实现或上线。

证据：

- [`系统关系与现状.md:14`](</Users/god/Projects/mint/my_knowledge/bank-knowledge/02_knowledge/05_架构/2026-09-27_架构现状/系统关系与现状.md:14>)：项目关系、目标方向及证据限制。
- [`贷款产品数据路径决策与现状.md:14`](</Users/god/Projects/mint/my_knowledge/bank-knowledge/02_knowledge/05_架构/2026-09-29_贷款产品Fineract适配/贷款产品数据路径决策与现状.md:14>)：DCIS 封装层和禁止 DFBM 直连核心的用户确认。

该关系只能用作搜索导航。每个接口仍须从契约和当前源码确认是否经过 DCIS 或 Fineract。

## 4. 知识库现状

知识库已经具备：

- `tools/kb.py`：仅检索 `02_knowledge` 的 UTF-8 Markdown/TXT，支持 list/search/read。
- `tools/kb_mcp.py`：stdio MCP，暴露 `search_knowledge`、`read_document`、`list_documents`。
- `tools/verify.py`：验证搜索、读取边界和 MCP 工具契约。
- 路径边界：拒绝读取 `00_inbox`、`01_sources` 和知识根目录外文件。

macOS 接入已配置为知识库本地 Python 3.12 虚拟环境，`tools/verify.py` 于 2026-10-06 验证通过，覆盖搜索、读取边界和 MCP initialize/list/search/read/reject。无依赖的 `python3 tools/kb.py` 仍可作为未配置 MCP 主机上的直接入口；其他电脑按知识库 README 创建各自虚拟环境和本机路径配置。

知识文档有“待审核”“实测”“用户确认”“历史快照”等不同状态。入库不等于生效，检索结果必须保留状态和适用范围；`99_演示` 永远不能作为真实业务证据。

## 5. 安全与访问边界

1. 四个外部仓库只读；BankDev Agent 的派生索引只能写入自身未来的 `var/`。
2. 知识检索限定 `02_knowledge`；阶段 0 未扫描 `00_inbox`、`01_sources` 内容。
3. 配置类和证书类文件默认只记录路径/元数据，未明确允许不得读取内容。
4. 不运行 Gradle/Maven、Git hook 或仓库脚本，不访问 dev/生产数据库和 Kubernetes。
5. 用户已确认知识库远端是其私有 GitHub 仓库，并授权知识库更新直接提交、推送；真实资料仍不得复制到公开仓库或外部服务。

## 6. 阶段 0 后的实现入口

首个黄金用例选定 `01100121`。原因：DFBM 当前源码有 OpenAPI、Controller、Service 和 Mapper 的完整本地查询链，可同时验证正向定位、“不得虚构 DCIS/Fineract 下游边”，以及知识库旧契约与当前代码发生漂移时的冲突检测。详见 [`02-golden-case-01100121.md`](02-golden-case-01100121.md) 和结构化 [`case.json`](../../tests/golden/cases/01100121/case.json)。
