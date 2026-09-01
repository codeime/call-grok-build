# 工作流协议

本文说明 Codex 如何调用 grok-build MCP server、等待结果和处理 v2 收据。日常任务使用当前 Codex workspace，Grok 在同一 cwd 原生运行。

## 核心规则

- 用户明确点名 Grok Build 且目标是当前 workspace 时，不要重复询问私有仓库或 xAI 外发授权。
- Codex 宿主自动传入当前 workspace 的绝对 cwd；所有任务都使用 direct。
- 不复制代码、不建立额外目录、不做 Git 或全目录内容扫描。
- 可选 paths 只提示 prompt 的关注范围，不改变 cwd 或访问控制。
- 一个 job 只有一个生命周期负责人、一次无 session/prompt 的 discovery ACP 模型证明，以及一个单 session/单 prompt 的 task ACP 进程。
- 失败、超时、取消、模型切换或限制达到后不自动重试或重新委派。

## 工具契约

### delegate_readonly

delegate_readonly(task, cwd, mode, paths?) 是研究、计划和 review 的高阶只读入口，立即返回 job_id。mode 只能是 research、plan 或 review。cwd 是宿主当前 workspace 的绝对路径；用户通常不需要手写它。

paths 可省略，也可提供非空相对路径列表。它只用于让 Grok 聚焦 prompt，不改变 direct 路由，不复制文件，不产生独立访问边界。

每个 job 在启动时刷新实时模型目录，并通过 ACP initialize 证明 provider/runtime default model 及该模型最高 advertised reasoning effort。

### spawn_readonly

spawn_readonly(task, cwd, mode) 是兼容入口，只接受 research、plan 或 review。新调用优先使用 delegate_readonly。

### spawn_worker

spawn_worker(task, cwd) 启动 implement 任务，直接在当前 workspace 使用 workspace sandbox。它允许 primary checkout、已有 staged/unstaged/untracked 修改和非 Git 目录。Grok 只能使用文件工具，terminal、解释器、Git、测试、Agent 和外部 MCP 均禁用。

correction_of_job_id 只用于 Luna 指出的那一次有界修复，必须指向同一 cwd、紧邻且成功的 implement job。最多一次 correction；失败 job 不能作为 parent。

### await_result

await_result(job_id, after_revision, max_wait_seconds, offset, limit) 对准确 job 做有界等待。短任务一次等到终态；长任务只在这次等待超时后继续等待同一个 job。running 或 model revision 变化不会触发新 job，终态调用幂等。offset/limit 只用于答案分页。

### setup

setup(cwd, timeout_seconds) 是可选诊断，用于刷新模型目录和进行无 prompt ACP 初始化证明。每个实际 job 仍会自己证明模型，因此正常流程不需要先 setup。

### status、result、list、cancel

- status 返回准确 job 的 compact 生命周期状态，适合低频诊断；
- result 返回完整幂等 v2 收据和分页答案；
- list 只列出当前 MCP server 进程内的 job；
- cancel 只终止准确 job 创建的进程组，不使用模糊进程匹配。

发起 job 的调用者必须在同一 MCP 连接中保存 job_id、等待、取回收据和取消。不同 MCP 进程之间不能接力旧 job。

## 模型与 sandbox

bridge 不写死 Grok 版本，也不根据版本字符串猜测强弱。实际 model 由 catalog 和 ACP runtime default 共同证明，reasoning effort 取该模型实际广告的最高档位：

~~~text
xhigh > high > medium > low > none
~~~

catalog、ACP、完成信息或 model-switch 不一致，或出现 fallback、未知 effort、空结果时，任务失败关闭或保持 unverified。

research、plan、review 使用 read-only sandbox；implement 使用 workspace sandbox。所有任务显式禁用真实工具 ID run_terminal_cmd 和 Agent，防止 Grok 递归调用本插件或其他代理。

## Codex subagent

如果宿主向 Codex subagent 暴露本插件工具，subagent 可以直接成为一次 job 的生命周期负责人；否则它应把任务包交回主代理。父任务不要让多个调用者为同一目标重复发起。

Codex subagent 和 Grok subagent 是两件事：前者可作为插件调用者，后者始终禁用。插件不检查主代理身份，但每个调用者只操作自己准确持有的 job_id。

## 实现后的核验

Codex 任务前记录当前 workspace 状态，Grok 完成后检查实际 diff、运行测试，再让只读 gpt-5.6-luna 使用 max reasoning 独立 review 原始需求、验收标准、实际 diff 和测试证据。bridge 不声称能区分任务前已有修改与 Grok 新增修改。

如果 Luna 返回 needs_changes，最多创建一次带 parent 的 correction，并完成一次 Grok 回归复审和一次 Luna Max 终审。第二次仍需修改时停止并报告 unverified。

## 收据

结果使用 grok.codex.result.v2。收据至少记录：

- job/session、status、mode、route、公开 cwd 标签；
- model、reasoning_effort、model_evidence、sandbox 和 memory_policy；
- loop_guard、correction_chain、stop_reason、usage 和 errors；
- answer 及其分页信息；
- workspace.integrity_snapshot: not_collected；
- verification.schema_valid、review_required 和 verified。

schema_valid 只表示 envelope 格式正确。verified 只有 Codex 完成独立核验后才可判定。

## 停止条件

遇到失败、超时、取消、ACP 异常、模型 fallback/switch、turn/output 上限、空回答或 MCP 进程关闭时，停止使用该结果。不要改路由、再开 job 或把部分输出当作成功。
