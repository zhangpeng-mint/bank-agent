# Golden 回归用例

每个目录固定一个真实需求或接口的输入、仓库 snapshot、期望实体/关系、负向约束和证据。Golden 是评测基线，不自动代表业务审批或上线状态。

规则：

1. `case.json` 必须是合法 JSON，路径相对于对应 `repo_id` 根目录。
2. 代码事实固定到 commit 和文件 SHA-256；漂移后先重新核验，再更新 golden。
3. `confirmed_static` 只能来自当前代码/契约的确定性证据。
4. 搜索未命中记录为 `scoped_absence`，不得升级成全局不存在。
5. 文档状态必须原样保留；演示资料不得进入真实用例。
6. 已进入 snapshot commit 的知识证据标记 `worktree_state: committed`；尚未提交但经本次任务明确校正的证据必须标记 `modified_expected` 并同时锁定文件 SHA-256，不得伪装成 commit 内证据。
7. 分析器不得读取 Golden 生成结果；Golden 只在分析完成后作为评测输入，检查入口、表、负向边和证据覆盖。
