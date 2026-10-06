# 阶段 1 实施报告

## 1. 完成范围

阶段 1 已建立不依赖第三方包的 Python 3.11+ 最小工程，当前使用 `/opt/homebrew/bin/python3.12` 验证。实现内容：

- TOML 仓库配置加载与安全策略校验。
- allowlist 路径解析、include/exclude、目录逃逸和符号链接防护。
- 固定参数、无 shell 的只读 Git snapshot 检查。
- Golden case JSON 加载、证据 hash/行号/关键标记校验。
- Mapper/表证据验证及限定范围负向搜索。
- 文档—代码冲突结构化登记。
- CLI：`check-config`、`snapshots`、`verify-golden`。
- 9 项标准库 `unittest` 单元/回归测试。

阶段 1 没有实现代码索引、AST 分析、SQLite 或模型调用；这些属于后续阶段。

## 2. 模块

| 模块 | 职责 |
|---|---|
| `bankdev_agent.config` | TOML 解析、只读策略、仓库模型与安全路径解析 |
| `bankdev_agent.git_snapshot` | HEAD、分支、remote、upstream、ahead/behind 和工作树检查 |
| `bankdev_agent.golden` | Golden 证据、调用边、Mapper/表、负向范围和冲突验证 |
| `bankdev_agent.cli` | 无副作用命令行适配层 |

所有 Git 子进程使用参数数组、关闭 stdin、设置 15 秒超时且不经过 shell。路径必须是逻辑 repo ID 下的相对路径；绝对路径、`..`、排除目录、越界解析和符号链接会被拒绝。

## 3. 开发期间发现的真实漂移

首次执行 snapshot 门禁时，阶段 0 基线没有通过：DFBM、DCIS 和知识库 HEAD 已前进。只读比较确认后，基线更新到当前 upstream：

- DFBM `44f59fabb2235f612fc470b49b18a0d551a9b995`
- DCIS `350fbdae3506f79bc1a1cf99afeb7f32680db415`
- Fineract `d5636847ac556c30b437254c353f05526d172b97`
- 知识库 `f2cfbada05011c0d0f15f607feb4826d254b2526`（2026-10-06 契约校正和 macOS MCP 配置推送后更新）

`01100121` 发生契约变化：

| 来源 | HTTP 契约 | 状态 |
|---|---|---|
| 知识文档 2026-09-29 | `GET /internal/v1/loan-products`，query `dfpIdList` | 待审核的旧快照 |
| 当前 DFBM OpenAPI | `POST /dfbm-fbs/v1/loan-products`，可选 `LoanProductQueryRequest` body | 当前 commit 静态事实 |

当前 Controller 是 `LoanInternalController`。服务和 7 个 Mapper 的本地聚合链没有改变；产品查询方法仍不调用 DCIS/Fineract。同一 Controller 的其他开户/客户查询方法会调用 DCIS，因此后续调用图必须达到方法级，不能使用类级依赖替代方法级事实。

这次变化证明 snapshot/hash 门禁是必要的：如果只依赖知识库或旧行号，Agent 会输出已经过时的 HTTP 方法、路径和入口类。

## 4. 验证结果

执行命令：

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent check-config
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent snapshots
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent verify-golden tests/golden/cases/01100121/case.json
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
```

结果：TOML 与 JSON 可解析；Python 编译通过；4 个 snapshot 符合当前基线；Golden 的证据、8 张表、3 项限定范围负向搜索和 1 项文档—代码冲突全部通过；9 项测试全部通过。

## 5. 尚未解决

- 外部仓库继续变化时 snapshot 测试会按设计失败，需要只读复核后更新基线。
- DCIS 的 `.sdkmanrc` 及知识库的 `.DS_Store/.obsidian` 是用户环境既有文件，本阶段未处理。
- 当前 Golden 依靠明确证据定义，尚未由通用 Java 解析器自动生成调用图。

## 6. 后续校正

2026-10-01，用户明确裁决以当前 DFBM 契约为准。知识库已将 `01100121` 校正为 `POST /dfbm-fbs/v1/loan-products` 和可选 `LoanProductQueryRequest` body；Golden 将原 `known_conflicts` 迁移为 `resolved_conflicts`。2026-10-06，该校正随知识库 commit `f2cfbad` 推送，Golden 改为同时锁定 commit、文件 SHA-256 和行号。
