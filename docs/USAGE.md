# 使用指南

本文说明如何把任务交给 Grok Build、如何等待结果，以及如何把结果交回 Codex 做独立判断。面向日常使用时，只需在 Codex task 中描述意图；Skill 会替你选择合适的 MCP 工具。需要精细编排时，下面的工具契约可作为参考。

## 开始前

目标 `cwd` 必须是已存在的具体项目/资料子目录，并且使用绝对路径。不要把文件系统根目录、home、账号目录父级、其他账号 home、整个临时目录或系统配置/程序目录作为目标；临时测试应先创建独立子目录。委托前先确认：

```bash
grok --version
grok login
grok models
```

如果本次要写入代码，先准备干净的 linked Git worktree；不要让实现任务指向 primary checkout。目录、prompt 和需要发送的代码上下文都应经过脱敏和授权检查，详见 [安全边界](SECURITY.md)。

## 推荐的任务包

任务越具体，结果越容易复查。可以使用下面的结构：

```text
Goal:
Scope (directories/files):
Constraints:
Acceptance criteria:
Evidence/output required:
```

五个字段的含义：

| 字段 | 应包含的内容 |
| --- | --- |
| `Goal` | 要回答或完成的单一目标 |
| `Scope` | 可以读取或修改的目录、文件和版本范围 |
| `Constraints` | 不可触碰的文件、兼容性、性能、安全和操作限制 |
| `Acceptance criteria` | 可观察、可测试、可复现的完成条件 |
| `Evidence/output required` | 来源 URL、`file:line`、diff、命令和测试结果等证据 |

不要把整段对话原样转发给 Grok；保留完成任务所需的目标、范围、约束和验收标准即可。

## 选择模式

| 模式 | MCP 工具 | sandbox | 适合的任务 | 是否修改文件 |
| --- | --- | --- | --- | --- |
| `research` | `spawn_readonly` | `read-only` | 外部资料、技术选型、事实核查 | 否 |
| `plan` | `spawn_readonly` | `read-only` | 实现拆解、迁移和测试计划 | 否 |
| `review` | `spawn_readonly` | `read-only` | 代码审查、风险识别和回归检查 | 否 |
| `implement` | `spawn_worker` | `workspace` | 在隔离 worktree 中实现已确认的变更 | 是，仅限 linked worktree |

研究默认启用 Grok 内置 web search；其他模式默认关闭。需要时可以在任务包中明确要求 `web_access: true` 或 `web_access: false`。

## 在 Codex 中使用

安装并重新加载插件后，新建一个 Codex task，使用自然语言即可：

```text
请使用 Grok Build 做一次只读 review。
目标：检查缓存失效逻辑是否可能返回过期数据。
范围：src/cache、tests/cache
要求：只报告有证据的 finding，按严重度排序，给出 file:line、影响、修复建议。
项目目录：/path/to/repository
Codex 负责复现 high/critical finding。
```

实现任务的示例：

```text
请让 Grok Build 在以下干净 linked worktree 中实现目标变更：……
只修改实现所需文件；不要运行测试、shell、interpreter、Git 或网络命令，也不要提交、
推送、合并、变基、cherry-pick、reset，或创建/删除 worktree。完成后由 Codex 运行相关测试。
工作目录：/path/to/repository-grok-worktree
完成后必须由 gpt-5.6-luna 使用 max reasoning 独立 review 实际 diff。
```

### 在 Codex subagent 中使用

当宿主已把本插件的 Skill/MCP tools 提供给 subagent 时，可以让一个 Codex subagent 直接拥有完整调用生命周期：

```text
调用 Grok Build 做一次只读计划。
你负责 setup、spawn、保存并轮询准确 job_id、读取 result，
再把收据、关键结论和独立核验证据回传给父任务。
不要创建第二个插件调用，也不要让父任务为相同目标重复 spawn。
项目目录：/path/to/repository
```

调用约束：

- 插件不检查调用者是主代理还是 subagent；是否可用取决于当前 Codex 宿主是否确实向该 subagent 暴露工具。
- 同一 MCP server 内的 job 列表和两个默认异步 job worker 槽位由所有 Codex 调用者共享；`setup` 不占用这些槽位。每个调用者只操作自己明确持有的 `job_id`，不要取消同级 subagent 的 job。
- 若宿主为不同 subagent 启动独立 MCP server，job、correction parent、并发计数和同 worktree 锁都不跨进程共享。subagent 必须在自己的连接中完成 `setup` → spawn → `status` → `result`，连接结束前取回收据；不能把 job ID 交给父任务或另一 subagent 接力。并发实现必须使用不同的 linked worktree。
- 同一 worktree 的实现互斥、correction 链和 Luna Max 独立 review 要求保持不变。实现调用者可以自己运行测试并安排独立 reviewer，或把 worktree diff、测试证据和收据交回父任务完成 review；未完成前保持 `unverified`。
- Codex subagent 可以调用插件，但不得再派生另一个插件调用者；Grok 的 `Agent` 和 subagents 仍在 CLI 层禁用。
- 如果 subagent 看不到本插件工具，它应返回任务包让主代理执行，而不是声称已经调用。

## MCP 工具契约

### `setup`

```text
setup({"cwd": "/path/to/repository", "timeout_seconds": 120})
```

刷新目标目录下的 Grok 模型目录，并进行一次无 prompt 的 ACP initialize attestation。`timeout_seconds` 可选，范围 `10..180`，默认 `120`；同一个截止时间覆盖 catalog probes 和 ACP runtime attestation。只有同时满足 `ready: true` 和 `runtime_attested: true`，且返回 `selected_model` 与 `selected_reasoning_effort` 时，才可以继续提交任务。

### `spawn_readonly`

```text
spawn_readonly({
  "mode": "research|plan|review",
  "task": "Goal/Scope/Constraints/Acceptance criteria/Evidence",
  "cwd": "/path/to/repository"
})
```

可选参数：

| 参数 | 范围与默认值 |
| --- | --- |
| `timeout_seconds` | `10..3600`，默认 `1800` |
| `max_output_chars` | `1000..200000`，默认 `120000` |
| `web_access` | 布尔值；默认仅 `research` 为 `true` |
| `max_turns` | `1..48`，默认 `24` |

工具立即返回 `job_id`，不会同步等待 Grok 完成。

### `spawn_worker`

```text
spawn_worker({
  "task": "Goal/Scope/Constraints/Acceptance criteria/Evidence",
  "cwd": "/path/to/repository-grok-worktree"
})
```

初次实现还需要满足：`cwd` 是 Git 根目录、是 linked worktree、不是 primary checkout，并且开始前没有变更。工具先立即返回 job，linked-worktree、干净状态和完整快照验证在 job 进入 `running` 后执行；验证失败会成为该 job 的 `failed` 终态。可选参数与 `spawn_readonly` 相同；修正轮次另有一个参数：

```text
"correction_of_job_id": "<immediately-previous-successful-worker-job-id>"
```

该参数只能用于 Luna review 后的有界修正，必须指向同一 worktree 中紧邻的成功实现 job。不要把它用于失败重试、分支修正或人工插入修改后的继续执行。

### `status`

```text
status({"job_id": "<job-id>"})
```

返回生命周期元数据，不返回完整答案。常见状态为 `queued`、`running`、`succeeded`、`failed`、`timed_out` 和 `cancelled`。通常每 10–30 秒检查一次即可，避免紧密轮询。

### `result`

```text
result({"job_id": "<job-id>"})
```

只在状态为 `succeeded` 时读取完整结果。`offset` 从 `0` 开始，`limit` 为 `1000..80000`，默认 `40000`；答案很大时按页读取。收据中的 `verification.verified` 在 Codex 完成独立核验前保持 `false`。

### `list`

```text
list({"limit": 20})
```

列出当前 MCP server 进程内最近的 job，`limit` 范围为 `1..100`。进程重启后，内存中的 job 不保证可恢复。

### `cancel`

```text
cancel({"job_id": "<exact-job-id>"})
```

只终止该 job 创建的 Grok 进程组。不要使用 `pkill`、`killall` 或模糊的进程匹配。取消写入任务后要检查 worktree；取消不会回滚已有文件变更。

## 等待与结果判读

标准生命周期如下：

1. 对目标目录调用 `setup`。
2. 构造最小但完整的任务包，调用对应的 spawn 工具。
3. 保存 `job_id`，以 10–30 秒间隔调用 `status`；由发起 spawn 的 Codex 调用者负责这个生命周期。
4. `succeeded` 时调用 `result`；其他终态先检查错误，不把部分答案当作成功。
5. 根据任务类型执行 Codex 核验，再报告结论。

job 的 `timeout_seconds` 从 `running` 开始，不含排队时间；它覆盖 cwd scope preflight、前后 Git/文件快照、CLI probe、模型 attest 和 ACP 执行。所有 ACP 任务显式禁用 `run_terminal_cmd` 和 `Agent`；implement 只能使用文件工具，所以测试必须在 Grok 返回后由 Codex 运行。

重点收据字段：

| 字段 | 判读方式 |
| --- | --- |
| `model_evidence` | 检查目录默认模型、ACP runtime default、请求参数和完成信息是否一致 |
| `memory_policy` | 记录 CLI 是否支持并使用 `--no-memory`；不要做超出该字段的隔离承诺 |
| `loop_guard` | 确认 prompt、turn、超时和 correction 限制 |
| `git` | 检查 tracked/untracked/ignored 证据、Git 管理区/object database、worktree 和 primary checkout；管理区变化应以 `E_GIT_ADMIN_CHANGED` 失败关闭 |
| `filesystem` | 非 Git 只读任务检查有界文件树前后是否一致；无法证明完整性时不要接受结果 |
| `verification` | `schema_valid` 仅说明格式；`verified` 需要 Codex 自己设置/判定 |
| `errors` | 失败或未验证时的稳定错误代码与说明 |

研究、计划和 review 的答案也需要事实核查；实现任务必须额外完成 Luna Max 的只读 review。没有独立验证，报告中应明确标注 `unverified`。正常取消或 MCP server 关闭会尝试清理该 job 的准确进程组，但宿主被 SIGKILL、崩溃或断电时仍可能留下孤儿 Grok 进程；异常恢复时只按准确 job 证据排查，不要用模糊进程匹配扩大范围。

调用 `setup` 或 spawn 工具时必须传入真实绝对 `cwd`；`status` 和 `result` 只接收 `job_id`。这些工具的公开响应中，`cwd` 固定显示为 `.`。Git 根路径、分支名和 worktree 位置也只返回哈希、计数或布尔证据；相对变更文件名仍可能识别项目，因此完整收据不应直接公开。

Grok 启动前的 exact-cwd scope preflight 最多检查 20,000 个条目，不读取内容预算；顶层 `.git` 只允许真实目录或 regular pointer，`.git` symlink、悬空/循环链接及指向 `cwd` 外的链接都会失败关闭。完整 non-Git、untracked、ignored 和普通 Git admin 扫描采用 20,000 条目/128,000,000 字节边界；tracked symlink 扫描采用 200,000 条目边界；Git object database 采用 200,000 条目/512,000,000 字节边界，并拒绝 alternates。Git diff 禁用 external diff/textconv；include/includeIf、外部 attributes/excludes/hooks/worktree path 或 clean/smudge/process/required filter 返回 `E_GIT_CONFIG_EXTERNAL`，非真实祖先的 Git root 返回 `E_GIT_SCOPE`。所有边界均为等于上限可接受、超过即拒绝。

## 实现修正流程

只有以下流程允许修正：

```text
初次 worker 成功
        │
        ▼
Codex 读取实际 diff + Luna Max review
        │
        ├─ pass ───────────────► 结束，仍由 Codex 决定是否提交
        │
        └─ needs_changes ──────► 用紧邻 parent job 发起 correction
                                   最多两轮，之后停止并请求决策
```

修正前不要在 worktree 中插入手工编辑；桥接层会比较 parent 收据与当前完整快照，任何中间变更都会拒绝。修正不是自动重试；失败、超时、取消或模型切换后的任务也不会自动重跑。产品流程只安排一次针对修复结果的 Grok 回归复审，再安排一次 Luna Max 独立终审；两者都不得递归调用本插件或自动重新委派。bridge 的两轮 correction 上限只是安全护栏，不是继续循环的许可。
