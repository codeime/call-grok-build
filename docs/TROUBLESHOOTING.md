# 故障排查

先保留失败 job 的 job_id、状态和错误码。不要新开相同任务；先判断失败发生在 cwd 校验、模型探测、ACP 启动、任务执行还是结果核验。

## 快速检查

~~~text
python3 --version
grok --version
grok login
grok models
~~~

日常调用直接使用当前 Codex workspace，不需要复制项目、准备额外目录或执行 Git 目录管理命令。插件会使用宿主传入的当前绝对 cwd，并让 Grok 在同一目录启动。

在 Codex 中：

1. 只读任务调用 delegate_readonly，不必先 setup；
2. 短任务用 await_result 等到终态，长任务只在一次等待超时后继续等待同一个 job；
3. 需要完整答案和收据时再调用 result，避免高频 status；
4. implement 失败、取消或超时后检查当前目录实际 diff，再决定人工处理；
5. 不要因为内部 revision 变化、输出延迟或一次失败自动重试。

## 插件不可见或仍在使用旧版本

- 确认安装对象的根目录包含 .codex-plugin/plugin.json、skills/ 和 .mcp.json；
- 在 Codex 插件管理界面重新加载或重新安装插件；不要直接编辑生成的缓存目录；
- 更新后新建一个 Codex task，旧 task 不一定重新发现新增 Skill/MCP server；
- 请求列出 grok-build MCP tools，确认有 delegate_readonly、await_result、setup、spawn_readonly、spawn_worker、status、result、list 和 cancel。

### subagent 看不到工具或拿不到结果

插件不按主代理或 subagent 身份拒绝调用，但宿主必须确实把 Skill/MCP tools 暴露给当前 subagent。看不到工具时，让 subagent 返回有界任务包，由主代理执行，不要声称已经调用。

发起 job 的代理必须在同一 MCP server 连接中保存 job_id、等待终态和读取收据。server 连接结束后，其他进程不能接力旧 job。每个调用者只管理自己发起的 job；一个父任务不要为同一目标重复提交。

### 不再重复询问授权

当前 task 已明确点名 Grok Build 且目标是当前 workspace 时，不要仅因仓库私有、代码会发送到 xAI、任务是 implement 或由 subagent 发起而再次询问。该授权覆盖本次初始 job，以及同 cwd 内最多一次 correction、一次 Grok 回归复审和一次 Luna Max 终审，但不覆盖密钥、客户数据、任务外目录、额外外部操作或自动重新委派。

## setup 或 job 返回未就绪

### CLI 找不到或未登录

检查 grok --version 是否成功，并确认 grok 在启动 Codex 的 PATH 中。登录后再运行 grok models。插件只使用 CLI 自己的认证缓存，不转发 XAI_API_KEY 或代理凭据。

### 模型目录或 ACP 证明失败

每个实际 job 都会刷新模型目录并通过 ACP initialize 证明 provider/runtime default model；setup 不是绕过证明的方法。目录、ACP 或完成信息不一致、fallback、model switch、未知 effort 和无可验证默认模型都会失败关闭。

插件从当前模型实际广告的 effort 中选择最高档位：

~~~text
xhigh > high > medium > low > none
~~~

不要在任务包中写死旧模型版本或自行指定看似更强的模型。以本次 v2 收据中的 model、reasoning_effort 和 model_evidence 为准。

## 常见错误

| 错误或状态 | 原因 | 处理 |
| --- | --- | --- |
| E_CWD、E_CWD_SCOPE、E_CWD_CHANGED、E_CWD_FD | cwd 不是稳定、具体且可访问的项目目录，或排队期间身份变化 | 使用当前 workspace 的实际目录，检查权限和路径替换；不要扩大到账号或系统目录 |
| E_CWD_BUSY | 同一 cwd 已有活动 implement job | 等待准确 job 结束或取消它；不要再次提交相同任务 |
| E_GROK_NOT_READY、E_AUTH | CLI、登录、目录刷新或能力探测失败 | 检查 grok login、grok models 和 PATH；不要自动重试同一 job |
| E_MODEL_ATTESTATION、E_MODEL_MISMATCH、E_EFFORT_MISMATCH、E_MODEL_FALLBACK、E_MODEL_SWITCHED | 运行时模型证明不一致或发生 fallback/switch | 停止使用结果，保留收据并检查 CLI/服务配置 |
| E_GROK_START、E_ACP_EXIT、E_ACP_PROTOCOL、E_ACP_SESSION | ACP 进程或协议启动异常 | 保留有界 stderr，检查 CLI 版本和登录状态；不要把部分回答当作成功 |
| E_EMPTY_RESULT、E_OUTPUT_LIMIT、E_STDERR_LIMIT、E_TURN_LIMIT | 没有答案或达到回答/turn 限制 | 缩小任务和输出要求；该 job 不会自动继续 |
| E_TIMEOUT、timed_out | job 达到执行 deadline | 记录状态；implement 要检查实际 diff；不要自动重试 |
| E_CANCELLED、cancelled | 准确 job 已被取消 | 读取收据并检查当前目录；取消不会撤销已发生的文件修改 |
| failed、unverified | bridge 或 Codex 无法证明结果，或独立核验未完成 | 停止使用该结果，按证据修复后由 Codex 决定是否发起唯一 correction |

错误消息可能因平台和 CLI 版本不同而变化；稳定错误码和 v2 收据优先于 stderr 文案。

## 只读任务出现文件变化

read-only 任务按约定不应修改文件。如果 Codex 观察到变化，可能来自 Grok CLI、编辑器、构建工具或其他并发进程。不要假设来源，也不要把结果当作只读成功；先保留 job_id、收据和实际 diff，确认来源后再继续。

## implement 结果如何处理

implement 直接作用于当前 workspace，允许 primary checkout、已有 staged/unstaged/untracked 修改和非 Git 目录。bridge 不扫描内容，也不声称能自动把差异归因给 Grok。

Codex 应：

1. 在任务前记录 status、diff 和与目标有关的文件状态；
2. 在任务完成、取消或超时后检查实际 diff；
3. 运行相关测试；
4. 让 gpt-5.6-luna 使用 max reasoning 独立 review 原始需求、验收标准、实际 diff 和测试证据；
5. 只有 review 通过后才考虑人工提交或交付。

如果 Luna 返回 needs_changes，最多安排一次带准确 parent 的 correction 和一次回归复审。失败 job、取消 job 和第二次仍需修改都不能自动接续。

## 任务似乎卡住

- 优先使用 await_result 做 1 到 60 秒有界等待；
- 短任务通常一次等待即可，长任务只在等待超时后对同一个 job 再等；
- status 仅用于 compact 诊断，避免高频轮询；
- result 只在需要完整答案或审计字段时读取；
- MCP server 重启后，内存中的 job 不保证存在，不要假称可以恢复；
- stdout 背压或 MCP 关闭时，以终态和收据为准，不要因没有即时文本就重新发起任务。

## 取消后仍有 Grok 进程

正常 cancel 或 MCP server 关闭时，bridge 会尝试清理准确 job 的进程组。宿主被强制终止、崩溃或断电时可能留下孤儿 Grok 进程。只根据准确 job_id 和进程证据核对，不使用 pkill、killall 或模糊匹配。

## 结果收据

结果使用 grok.codex.result.v2。公开 cwd 通常为 .，workspace.integrity_snapshot 固定为 not_collected，verification.verified 在 Codex 独立核验前为 false。

收据格式正确不等于答案正确。模型、sandbox、loop_guard、usage、errors、任务前后状态、测试和 Luna review 必须一起判断。
