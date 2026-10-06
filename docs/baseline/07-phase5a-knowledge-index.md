# 阶段 5A：知识库增量索引与证据检索

## 范围与安全边界

阶段 5A 把现有 Git Markdown 知识库接入 BankDev Agent 的共享 SQLite。索引器只读取配置登记的
`bank_knowledge` 仓库，只处理 Git 已跟踪且符合 include/exclude 规则的 `.md/.txt` 文件；演示目录、
`.obsidian`、`.DS_Store` 和其他未跟踪内容不会入库。唯一写入仍位于 BankDev Agent 的 `var/`。

受控知识文档当前均已提交，活动文档 snapshot 标记为
`git:f2cfbada05011c0d0f15f607feb4826d254b2526`，每份文档另存当前 Git blob hash 和是否匹配配置 baseline。
未跟踪的 `.DS_Store/.obsidian` 本机元数据不参与索引，也不会改变文档 snapshot。

## SQLite schema v3

新增的数据表包括：

| 表 | 作用 |
| --- | --- |
| `document_index_runs` | 文档索引运行和统计 |
| `document_snapshots` | 原子活动文档快照和 source revision |
| `document_parse_cache` | `(content_hash, parser_version)` 增量解析缓存 |
| `documents` | 标题、路径、状态、版本、资料性质和密级 |
| `document_versions` | 逻辑文档键、版本、状态、生效时间和替代关系 |
| `document_chunks` | 标题路径、正文、接口号、系统和精确行号 |
| `document_chunks_fts` | 使用 trigram tokenizer 的 SQLite FTS5 中文全文索引 |
| `evidence` | 稳定 evidence ID、内容 hash 和 `bankdev://` URI |
| `document_conflicts` | 多个非废弃主版本等确定性冲突 |

schema 1/2 的本地数据库可迁移到 schema 3；外部业务仓库没有迁移动作。

## 文档处理与检索

Markdown 按标题层级切分；大章节控制在约 5,000 字符以内，保留原始起止行。Front matter 存在时解析
基础标量元数据，没有 Front matter 时读取现有 `版本/状态/资料性质` 行并使用路径推断文档类型。

检索合并以下信号并保留命中原因：

1. 主接口号精确命中。
2. 正文接口号精确命中。
3. 标题和标题路径命中。
4. FTS5 trigram/BM25 中文全文命中。

默认过滤明确标记为 superseded/obsolete/废弃的文档；可用 `--include-superseded` 显式包含。单份文档
最多占默认结果两项，避免长文档淹没其他来源。

## CLI

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent index-docs
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-knowledge 01100121
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-knowledge "贷款产品查询"
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m bankdev_agent search-knowledge "开户" --system dcis
```

每个结果返回活动 snapshot、文档版本/状态、命中原因、受限 excerpt、内容分类以及可点击行号证据。

## 当前验收结果

- 47 个受控文档。
- 577 个结构化知识块和 577 条 evidence。
- 最大块长度 4,999 字符。
- 第二次索引 47/47 命中缓存，0 个重新解析，文档和块无重复。
- `01100121` 第一结果为主接口说明，而不是历史引用文档。
- 中文“贷款产品查询”同时命中接口说明和其他架构来源。
- 当前没有检测到多个非废弃主接口文档冲突。

版本冲突检测是保守门禁；没有检测到冲突不代表不同文档中的所有自然语言陈述都一致。字段级语义冲突
比较属于阶段 5B。
