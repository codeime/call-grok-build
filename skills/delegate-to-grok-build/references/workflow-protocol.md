# 工作流协议

在选择工具、构造任务包、等待任务或解释结果收据时阅读本文。

## 工具契约

- `setup(cwd, timeout_seconds)`：在 `cwd` 刷新实时模型目录，再执行仅初始化的 ACP preflight。可选 timeout 范围为 10..180 秒、默认 120 秒，同一 deadline 覆盖 catalog probes 与 ACP runtime attestation。它证明 provider/runtime default model，并选择该模型实际广告的最高、可确定排序的 effort。目录过期/离线/仅缓存、默认值含糊、缺少 ACP model state、effort 不支持或无法排序、catalog/ACP 不一致时失败关闭。
- `spawn_readonly(task, cwd, mode)`：请求 OS 级 `read-only` sandbox，且运行时只接受 `research`、`plan` 或 `review`。不暴露 Grok subagents 或可用的外部 MCP server。可选字段包括 `web_access`、`timeout_seconds`、`max_turns` 和 `max_output_chars`；只有确实需要来源研究时才启用 web access。
- `spawn_worker(task, cwd)`：在 `workspace` sandbox 中异步执行实现。首次 job 要求目标是干净的 linked Git worktree；这些校验在 job 的 `running` 阶段完成，失败会进入 `failed` 终态。Grok 的 `run_terminal_cmd` 和 `Agent` 被禁用，只能用文件工具修改；测试由 Codex 在返回后执行。correction 必须把 `correction_of_job_id` 设为紧邻的上一个成功 worker job；bridge 会验证 parent 快照未变，并限制 correction 链最多两轮。每个实现结果都带 `review_required: true`。
- `status(job_id)`：返回生命周期元数据，不返回可能很大的回答。
- `result(job_id, offset, limit)`：返回已完成的收据和有界的公开回答分页；只在确有必要时请求后续页。
- `list(limit)`：列出内存中的 job。活动 job 不保证在 MCP server 重启后继续存在。
- `cancel(job_id)`：只终止这个准确 job 所拥有的进程组。

`setup` 与选定任务必须使用同一个绝对 `cwd`。默认并发数是两个异步 job worker；`setup` 不走该 executor，也不占用这两个槽位。写入任务会锁定目标 worktree，防止当前 server 的另一个写入任务同时使用。job 默认执行超时 30 分钟、硬上限 60 分钟，从 `running` 开始且不包含排队时间；它覆盖 scope/content snapshot、Git、probe、attest 和 ACP。默认 turns 为 24，硬上限 48。

## Codex 调用者与并发

插件的 MCP 工具不绑定“主代理”身份。只要 Codex 宿主把这些工具提供给当前 subagent，主代理或 subagent 都可以直接调用；宿主没有暴露工具时，subagent 必须把任务包交回主代理，不能伪装成已执行。

发起 spawn 的 Codex 调用者应拥有该 job 的完整生命周期：在同一 MCP server 连接中保存准确 `job_id`，只查询、读取或取消这个 job，并向父任务返回收据。`list` 和 job 状态属于同一个 MCP server 的共享内存；同一任务内的受信任 Codex 调用者可能看到彼此 job，因此不要把 `list` 当作调用者隔离，也不要让同级 subagent 共享或猜测 job ID。默认两个异步 job worker 槽位在同一 server 的调用者之间共享，超出的 job 排队；`setup` 不占用这些槽位。同一进程内的 worktree implement 锁和 correction 链规则不会因调用者不同而放宽。

若 Codex 宿主为每个 subagent 启动独立 MCP server，则每个进程都有独立的内存 job manager：旧 `job_id` 在另一进程中不可查询，server 连接关闭会取消其活动 job，correction parent 也不能跨进程引用。跨进程的并发计数和 worktree 锁不共享，因此多个并行 implement 调用者必须使用不同的 linked worktree；不要依赖另一个进程返回 `E_WORKTREE_BUSY`。

一个父任务只能为同一个有界目标指定一个生命周期负责人。父代理不得在 subagent 已经 spawn 后再次提交相同任务，subagent 也不得再派生一个调用本插件的 subagent。Codex subagent 调用与 Grok subagents 是两件事：前者受宿主工具暴露能力控制，后者在 CLI 层始终禁用。

## 模型和 effort 选择

bridge 不根据模型名字推断强弱，也不把某个版本号写死。每个目标都会刷新 CLI catalog，确认 provider/runtime default model，再从该模型实际广告的 reasoning effort 中选择最高档位，并把这两个值锁定到任务调用中。第二次 ACP initialize attest、任务完成元数据（如果提供）和 model-switch 事件都会与选择值核对。任何 fallback 警告、不一致或模型切换都让结果保持未验证并失败关闭。

## 任务包

保持 prompt 有边界并明确：

```text
Goal:
Scope (directories/files):
Constraints:
Acceptance criteria:
Evidence/output required:
```

实现任务要补充文件归属，并说明 commit 和 push 不在范围内。审查任务要求带严重度和 `file:line` 证据的 actionable findings。研究任务要求直接来源 URL，并分别标记事实和推断。不要包含秘密或无关上下文。

## 生命周期和停止条件

启动后以大约 10–30 秒的间隔查询 `status`，并在 job 截止时间前停止。进入终态后：

- `succeeded`：读取 `result`，检查收据，并执行独立验证门槛；
- `failed`、`timed_out` 或 `cancelled`：检查错误和目标状态，不要把部分输出当作可接受结果。

自动重试和自动重新委派均为零。模型切换、turn limit、output limit、超时、空回答或进程重启都是停止条件。Luna 发现问题后的 correction 是一个新的、有明确 parent 的 worker job，不是重试，并且只能在两轮 correction 安全上限内进行。产品流程最多安排一次 Grok 修复回归复审和一次 Luna Max 独立终审；Codex subagent 可以作为一次生命周期负责人，但不得递归调用本插件。

## 收据

结果使用 `grok.codex.result.v1` envelope。收据记录 job/session 身份、生命周期和停止原因、动态选择的 model/effort、catalog 与 ACP 证据、脱敏目标标签（`.`）、sandbox、有界回答和诊断、可用时的 usage，以及 loop guard。

Git 收据记录 HEAD 身份、哈希化的 branch/worktree 身份、worktree 数量、status/diff hash、untracked/ignored 内容指纹，并记录有界的 Git admin/object 指纹、变化文件、primary-checkout 对比和工作区证据。非 Git 只读任务记录有界文件树完整性证据。绝对 Git 根目录和 checkout 路径不会出现在公开收据中。correction 链记录 root、parent、round 和两轮上限。

每次 job 先对 exact cwd 做 20,000 条目的 metadata-only scope preflight；顶层 `.git` 只允许真实目录或 regular pointer，symlink/越界/悬空目标被拒绝。non-Git、untracked、ignored 和普通 Git admin 内容扫描为 20,000 条目/128,000,000 字节；tracked symlink scan 为 200,000 条目；Git object database 为 200,000 条目/512,000,000 字节。Git root 必须是 `cwd` 的真实祖先；Git diff 禁用 ext-diff/textconv，config include/外部 path/filter 与 object alternates 被拒绝，index/locks 纳入指纹。等于边界可接受，超过即失败关闭；Git 命令输出另有硬上限。`.git` pointer 或 common/worktree admin/object 证据变化会以 `E_GIT_ADMIN_CHANGED` 失败关闭。

`schema_valid: true` 只表示 envelope 结构正确；bridge 有意返回 `verified: false`，实现收据还会暴露 `review_required: true`。只有 Codex 的独立检查（实现任务还包括必需的 Luna Max review）才能支持最终的 verified 结论。

## 进程清理

准确的 `cancel` 或 MCP server 正常关闭会尝试清理对应进程组。宿主被 SIGKILL、崩溃或断电时，清理代码无法运行，仍可能残留孤儿 Grok 进程；这是已知残余风险。不要使用 `pkill`、`killall` 或模糊进程匹配，也不要因孤儿进程自动重试或重新委派。
