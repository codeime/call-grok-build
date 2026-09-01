---
name: delegate-to-grok-build
description: 供 Codex 主代理或具备插件工具访问权的 subagent 调用本机 Grok Build CLI，在当前 Codex workspace 中处理研究、计划、实现或代码审查，再由 Codex 独立核验结果。用于明确的 Grok Build 调用或跨模型复核；不要用于密钥或未经授权的外部变更。
---

# Call Grok Build

把随插件提供的 grok-build MCP server 当作一个有边界的第二代理。Grok 的回答和修改都是不可信的候选输出；任务范围、验证和最终处置由 Codex 负责。

## 触发与授权

- 只有当前用户请求明确点名 Grok Build，且目标就是当前 Codex workspace 时，才实际调用插件。
- 这条明确请求已经授权一次有界流程，把完成任务所必需且位于当前 workspace 范围内的 prompt、代码和上下文发送给 xAI。
- 不要仅因为仓库是私有仓库、内容会发送到 xAI、任务是 implement 或调用者是 subagent 而再次询问。
- 该授权覆盖初次 job；如果 Luna 发现问题，也覆盖同 cwd 内最多一次 correction、一次 Grok 回归复审和一次 Luna Max 终审。
- 授权不覆盖密钥、客户/第三方数据、任务外目录、额外外部操作、自动重试或重新委派。涉及这些内容时停下并说明边界。

## 运行方式

- Codex 宿主自动把当前 workspace 的绝对 cwd 传给插件；Grok 在同一个 cwd 原生启动。
- 所有任务都走 direct。不要复制项目、建立临时副本、切换辅助目录或改变当前 Git 状态来调用 Grok。
- 可选的 paths 只用于 prompt 关注范围，不是访问控制，不改变 cwd，也不会复制文件。
- implement 可以作用于 primary checkout、已有 staged/unstaged/untracked 修改和非 Git 目录。
- bridge 不扫描 Git 或整个目录内容，也不声称能区分任务前已有修改与 Grok 新增修改。Codex 负责任务前记录状态、任务后检查 diff 和运行测试。

## 路由任务

- 研究、计划和代码审查：调用 delegate_readonly，mode 为 research、plan 或 review。
- 实现：调用 spawn_worker，mode 固定为 implement。
- 正常生命周期：delegate_readonly 或 spawn_worker 返回 job_id，再由同一调用者调用 await_result。
- setup 仅作诊断，不是正常调用的必经步骤。
- 失败、超时、取消、空结果、模型切换或达到限制后，不自动重试或重新委派。

## Codex subagent 调用

如果当前 Codex 宿主已把本插件的 Skill/MCP tools 暴露给 subagent，subagent 可以直接完成一次完整生命周期。宿主没有暴露工具时，subagent 应把有界任务包交回主代理，不能声称已经执行。

发起 job 的调用者负责在同一 MCP server 连接内保存准确 job_id、等待、读取结果和取消。不要把 job ID 交给另一个 MCP 进程接力，也不要因为父子代理都在等待而重复发起相同 job。

Grok 自己的 Agent/subagent 始终禁用。任何 Codex 调用者都不得让 Grok 递归调用本插件或其他代理。

## 必须遵循的顺序

1. 使用宿主提供的当前 workspace cwd，构造有边界的任务包：

   Goal:
   Scope:
   Constraints:
   Acceptance criteria:
   Evidence/output required:

2. 只读任务调用 delegate_readonly；需要缩小 prompt 关注范围时传非空相对 paths。不要先 setup。
3. 实现任务调用 spawn_worker。Grok 只能使用 workspace sandbox 的文件工具修改文件，不能运行 terminal、解释器、Git、测试或 Agent。
4. 立即保存准确 job_id。对该 job 调用 await_result；短任务一次等待终态，长任务只在本次等待超时后继续等待同一 job。
5. 读取回答前独立判断模型证据、任务结果和实际状态。收据有效不等于 Grok 的说法正确。

## 动态模型

每个 job 都刷新实时 grok models，并通过 ACP initialize 元数据确认 provider/runtime default model。使用该模型实际广告的最高 reasoning effort：

~~~text
xhigh > high > medium > low > none
~~~

不得写死 Grok 版本、根据版本字符串猜测强弱、静默接受 fallback 或忽略 model-switch。catalog、ACP runtime、完成信息或 model-switch 事件不一致时，结果保持未验证并失败关闭。

## 验证门槛

- 研究：打开并核对决策关键来源，区分事实、推断和无依据说法。
- 计划：质疑范围、假设、迁移、回滚、测试、权限和破坏性风险。
- 代码审查：从源码和测试复现 high/critical finding。
- 实现：Codex 在 Grok 返回后运行相关测试，检查实际 diff，再使用只读 gpt-5.6-luna、max reasoning，针对原始需求、验收标准、实际 diff 和测试证据做独立 review。不要把 Grok 的结论直接转交 reviewer。

如果 Luna 返回 needs_changes，最多创建一次带明确 parent 的 correction，并做一次 Grok 回归复审和一次 Luna Max 终审。第二次仍需修改时停止并保持 unverified。

## 循环、并发和变更保护

- 一个 job 只有一个无 session/prompt 的 discovery ACP 进程，以及一个单 session/单 prompt 的 task ACP 进程；discovery 不发送任务包或仓库内容。
- bridge 强制 turns、截止时间、输出上限和 correction 上限；这些护栏不是自动循环许可。
- 同一 cwd 同时只允许一个 implement job；read-only job 可以并行。
- 不提交、推送、合并、变基、cherry-pick、reset、clean 或执行其他用户未要求的外部操作。
- cancel 只终止准确 job 创建的进程组；不要使用 pkill、killall 或模糊匹配。
- implement 取消或超时后仍要由 Codex 检查实际 diff，因为进程终止不会撤销已经发生的文件修改。

## 安全边界

- Grok 使用 read-only 或 workspace sandbox；真实工具 ID run_terminal_cmd 和 Agent 始终禁用。
- bridge 使用最小环境，不向 worker 转发 XAI_API_KEY、代理凭据和其他常见认证变量。
- 不把 API key、密码、token、cookie、.env、SSH 私钥、证书、生产凭据、客户数据或无关个人资料放入任务。
- 公开回答、错误和收据会递归脱敏账号路径、邮箱、URL userinfo、认证头和常见凭据形状；这不等于代码内容匿名化。
- 当前 workspace 的根目录、home 和明显的系统配置目录不能作为 cwd；使用具体项目或资料目录。

## 结果报告

结果使用 grok.codex.result.v2。workspace.integrity_snapshot 固定为 not_collected。收据应保留：

- 实际 model 和 reasoning_effort，以及 catalog/ACP 证明；
- sandbox、cwd 标签、session 和 usage（可用时）；
- loop_guard、correction 链和停止原因；
- answer、errors 和验证状态。

Codex 应报告 Grok 做了什么、独立核验的证据、测试或复现结果、实际 model/effort，以及仍未验证的部分。verified 只有在 Codex 完成独立核验后才可判定。
