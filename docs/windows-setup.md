# Windows 安装与使用

## 前提与范围

安装 Git for Windows、Python 3.12（最低 3.11），确保 PowerShell 中 `git --version` 和 `py -3.12 --version` 可用。当前运行时无第三方依赖，无需 Java、Gradle、Maven或业务数据库。

项目已在 macOS 验证；本说明提供 Windows 配置方法，尚未完成 Windows 真机验收。部分历史 Golden/回归断言绑定原机工作树状态和文件链接格式，不能将它们在新机器失败直接判断为功能失效。

## 克隆与本机配置

```powershell
git clone git@github.com:zhangpeng-mint/bank-agent.git
Set-Location bank-agent
Copy-Item config/repositories.example.toml config/repositories.toml
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
$env:PYTHONUTF8 = '1'
```

公司电脑需要能访问此 GitHub 仓库的 SSH 凭据。DFBM、DCIS、Fineract、bank-knowledge 是单独的外部仓库，需要各自授权并单独克隆，本项目不包含它们的源码、知识原件、数据库或模型密钥。

建议在克隆外部源码时用 `git -c core.autocrlf=false clone <仓库地址> <目标目录>`，避免自动 CRLF 转换导致本机内容与固定 Git blob 的证据 hash 不一致。不要为匹配旧基线覆盖已有工作树修改；必要时使用独立克隆。

编辑 `config/repositories.toml`：

- 把四个 `root` 和知识库 `content_root` 改成本机绝对路径，TOML 中建议用 `C:/bank/repos/dfbm` 这种正斜杠形式。
- 核对各仓库 `origin`、`baseline_branch`、`baseline_commit`（完整 40 位 SHA）和 `upstream`。`git remote get-url origin` 的结果须与配置一致，包括 SSH/HTTPS 格式。
- 示例保留 01100121 的固定 commit；若改用其他版本，旧 Golden 不能自动视为新版本验收依据。
- 干净克隆的 `preexisting_changes` 保持 `[]`。如果确有本机修改，可在核对后登记实际路径；不要照搬原机 `.DS_Store`、`.sdkmanrc` 等修改记录。
- 保留只读安全限制、include/exclude 规则；不要把配置文件移出本项目 `config/`，当前 `var/` 位置按该目录结构解析。

本机配置已加入 `.gitignore`，后续 pull 不会覆盖它；公共模板更新需要人工核对并合并到本机配置。

## 建索引与分析

也可先启动图形工作台（仍需先完成上述环境变量与仓库配置）：

```powershell
py -3.12 -m bankdev_agent web
Start-Process 'http://127.0.0.1:8765/'
```

在“仓库与索引”页面点击更新代码/知识索引，然后使用检索、调用链和需求分析页面。服务仅供本机访问，无需 Node.js 或前端构建；浏览器内可以直接预览证据，无需打开 macOS 文件路径。结束时在启动服务的终端按 Ctrl+C，正在执行的任务会完成后退出。

在项目根目录运行：

```powershell
py -3.12 -m bankdev_agent check-config
py -3.12 -m bankdev_agent snapshots
py -3.12 -m bankdev_agent index-code
py -3.12 -m bankdev_agent index-docs
py -3.12 -m bankdev_agent search-interface 01100121
py -3.12 -m bankdev_agent detect-conflicts 01100121
py -3.12 -m bankdev_agent analyze-requirement '分析 01100121 贷款产品查询' --non-goal '不修改业务代码' --acceptance '定位契约与静态链路' --format markdown --output var/reports/01100121.md
```

输出在本机 `var/`，不共享原机 SQLite；它的文件路径和证据链接不能直接跨机器复用。`complete` 表示限定扫描范围内完成分析，不表示需求已经开发或运行测试通过。

## 模型辅助（可选）

无密钥可使用 `--backend mock`，默认 `template` 完全不调用模型。仅测试网关无需访问业务源码，但当前 CLI 启动仍需有可解析的四仓配置。

需要智谱时，在当前 PowerShell 会话安全输入密钥：

```powershell
$secureKey = Read-Host '智谱 API Key' -AsSecureString
$env:BANKDEV_MODEL_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
try {
    py -3.12 -m bankdev_agent model-smoke --backend zhipu
} finally {
    Remove-Item Env:BANKDEV_MODEL_API_KEY -ErrorAction SilentlyContinue
    $secureKey.Dispose()
}
```

真实需求使用 `--backend zhipu`；默认仅发送需求字段。`--model-context verified-facts` 会额外发送已验证事实文本/ID，应仅用于允许发送给外部模型的资料。密钥不写入源码、配置、`.env` 或提交记录。

## 验证与更新

无需外部业务仓库内容的网关测试：

```powershell
py -3.12 -m unittest tests.unit.test_model_gateway -v
```

完整测试命令为 `py -3.12 -m unittest discover -s tests -v`，但真实仓库测试绑定历史配置；Windows 首次验收应先确认 snapshots、索引和接口分析，再逐项核对涉及本机路径、原机未提交修改及 Golden 的差异。

部分 Python 的 SQLite 构建可能缺少 FTS5/trigram；若 `index-docs` 报相应模块/分词器不可用，需要安装带有这些能力的 Python/SQLite 构建。不要把索引失败当成知识检索无结果。

后续在本项目根目录 `git pull --ff-only`，核对配置模板和文档变化，然后按需重新执行 `index-code`、`index-docs`。保持业务仓库只读，不执行其中的构建脚本或连接业务服务。
