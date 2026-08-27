---
name: delegate-to-grok-build
description: 供 Codex 主代理或具备插件工具访问权的 subagent 调用本机 Grok Build CLI，处理边界明确的研究、计划、实现或代码审查任务，再由 Codex 独立核验结果。用于明确的 Grok Build 调用或跨模型复核；不要用于密钥或未经授权的外部变更。
---

# Call Grok Build

把随插件提供的 `grok-build` MCP server 当作一个有边界的第二代理。Grok 的回答和改动都是不可信的候选输出；范围控制、验证和最终处置由 Codex 负责。

## 路由任务

- 研究、计划和代码审查：调用 `spawn_readonly`，并将 `mode` 设为 `research`、`plan` 或 `review`。
- 实现：仅在干净的 linked Git worktree 中调用 `spawn_worker`，绝不能把 primary checkout 作为实现目标。Grok 的 `run_terminal_cmd` 和 `Agent` 被禁用；它只修改文件，测试由 Codex 在返回后运行。
- 生命周期：先调用 `setup`，任务运行时使用 `status`，终态后调用 `result`；使用 `list` 查看内存中的 job，使用 `cancel` 取消一个准确的 job ID。

## Codex subagent 调用

- 如果当前 Codex 宿主已把本插件的 Skill/MCP tools 暴露给 subagent，subagent 可以直接执行完整的 `setup` → spawn → `status` → `result` 生命周期；插件不要求调用者必须是主代理。
- 发起 job 的 Codex 调用者负责在同一 MCP server 连接内保存准确 `job_id`、等待或取消该 job，并把结果收据与独立核验证据回传给父任务。不要把 job ID 交给另一 MCP 进程接力，也不要因为父子代理都在等待，就为同一任务重复 spawn。
- 同一 MCP server 默认共享两个异步 job 执行槽位；`setup` 不占用这些 worker 槽位，额外 job 请求排队，同一 worktree 的实现只允许一个。宿主若为 subagents 启动独立 MCP server，各进程不共享 job、correction 链、并发计数或 worktree 锁；并发实现必须使用不同的 linked worktree。只操作自己明确持有的 job ID，不要取消同级 subagent 的 job。
- 这里的 subagent 指 Codex subagent。Grok 自己的 `Agent`/subagent 始终禁用，且任何 Codex 调用者都不得让 Grok 递归调用本插件。
- 宿主若没有向 subagent 提供本插件工具，subagent 应把有界任务包交回主代理调用，不得声称已经直接执行。

## 必须遵循的顺序

1. 解析一个明确的绝对 `cwd`，setup 和任务必须使用同一个目录。确认选中的文件和上下文获准发送到 xAI；处理私有代码或写入任务前先阅读[安全边界](references/security-boundary.md)。
2. 调用 `setup`。只有在它同时报告 `ready: true`、`runtime_attested: true`、provider/runtime default model，以及该模型实际广告的最高 effort 时才继续。每个目标都实时解析；绝不把某个模型版本写死、根据名字猜更强模型，或接受 fallback、catalog/ACP 不一致。
3. 发送精简的任务包，明确目标、准确范围、约束、验收标准和所需证据。没有必要时不要转发完整对话。
4. 启动最窄的匹配任务，以适合人的间隔等待，job 进入终态后再读取 `result`。spawn 只负责排队；linked-worktree、scope 和快照校验在 `running` 阶段完成，失败时读取 job 错误。工具字段、收据和停止条件见[工作流协议](references/workflow-protocol.md)。
5. 在把回答当作结论前独立核验回答或 diff。收据有效不等于 Grok 的说法正确。

## 验证门槛

- 研究：打开决策关键来源，检查日期和范围，并区分事实、推断和无依据说法。
- 计划：质疑范围、假设、迁移、回滚、测试、权限和破坏性风险；安全、迁移、权限或破坏性计划应使用独立 Luna Max 复核。
- 代码审查：从源码和测试复现所有 high/critical 严重度的 finding 后再接受。
- 实现：Grok 返回后先由 Codex 运行相关测试，再使用只读的 `gpt-5.6-luna`、`max` reasoning，针对原始需求、验收标准、实际 worktree diff 和测试证据做独立 review。不要把 Grok 的结论提供给 reviewer；由 Codex 独立判断并检查 diff。
- 一次实现修复流程最多安排一次针对修复结果的 Grok 回归复审，再安排一次 Luna Max 独立终审；允许一个 Codex subagent 直接拥有一次调用生命周期，但不允许 Grok 或 Codex 调用者递归调用本插件、自动重试或自动重新委派。

如果 Luna reviewer 不可用或失败，保持实现为 `unverified`；不要 merge、commit、push，也不要声称已完成。如果它返回 `needs_changes`，可以创建一个明确的 correction job，并引用紧邻的上一个成功 worker job。bridge 最多允许两轮 correction，拒绝分支、复用 parent 和中间编辑；第二轮仍需修改时停止为 `unverified`。这个安全上限不代表工作流自动循环。

## 循环与变更保护

- 一个 job 的运行时 attest 与任务执行使用独立 ACP 进程；任务 ACP 只创建一个全新的会话并发送单条 prompt。每个 ACP task 都显式禁用 `run_terminal_cmd,Agent`；Grok worker 不得运行 shell/interpreter/Git/测试，不得调用本插件、另一个 Grok worker 或 Codex 委托。
- 失败、超时、取消、达到 turns/output 上限、模型切换或空结果的 job 不会自动重试或重新委派。检查收据后停止，除非调用方明确授权一个新的有界尝试。
- bridge 强制 turns 上限、截止时间、输出上限和最多两轮 correction；correction 是新的、有明确 parent 的 job，不是自动重试。
- bridge 不提交、推送、合并、变基、cherry-pick、reset、clean，也不创建或删除 worktree。这些操作不属于本 Skill 的委托边界。
- 正常取消或 MCP 关闭会清理对应的准确子进程组；宿主遭遇 SIGKILL 或崩溃时清理代码无法运行，仍可能留下孤儿 Grok 进程。这是已知残余风险，不得宣称绝对的父子进程存活保证。

## 报告

返回 Grok 做了什么、独立核验的证据、测试或复现结果、实际选择的模型/effort，以及仍未验证的部分。保留结果收据，包括循环护栏、模型证据、sandbox、Git/文件树快照、correction 链和验证状态。
