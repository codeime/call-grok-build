# 调用路径改进结论复核

本文是对「只看这个插件，有哪些可以改进」及其后续复核的落盘，供维护者对照。它不是 v2 契约，也不替代 USAGE / SECURITY / TROUBLESHOOTING。

审查日期：2026-09-14。下文的第一轮结论和 CLI 探测都是改前的历史观察：证据来自当时的仓库实现、fake 测试契约，以及本机 `grok` 1.0.30 的只读探测（`--version` / `--help` / `models`，以及无 `session/prompt` 的 ACP `initialize` / `authenticate` / `session/new`）。未跑真实 Grok 任务，也没有 Codex 宿主的调用轨迹；这些记录不应当作本轮新增验证。

## 本轮状态与边界

本轮已把运行中诊断字段纳入 status、await_result 超时/终态响应和终态 result 的契约；字段语义见文末“诊断字段契约”。本轮没有执行真实 CLI 任务、真实 ACP session/prompt 或联网探测；fake 只覆盖协议测试。profile、记忆、slash command、deny/tool ID 的真实 CLI 行为，以及 `--json-schema` 与 ACP stdio 的兼容性，仍需用户明确授权后再做受控验证。低收益缓存、paths 补齐和工具排序不在本轮实施范围。

以下“第一轮结论”“站得住”“夸大或证据用错了”均针对改前实现；本轮新增诊断契约后的行为以文末契约为准。

## 总评

站得住的核心是两件事：

1. 长任务阶段，调用方几乎看不到 Grok 在干什么。
2. fail-closed 的模型一致性校验、最高 advertised effort、discovery/task 双 ACP、按 ID 禁用的请求，不该为了「快」拆掉。

不该直接按第一轮排期开工。探测缓存、收据变胖、Skill「教错路」、worker 吃用户 Grok profile，这几条要么成本很低、要么证据不足、要么和现有反重派设计冲突。

没有真实任务的端到端耗时数据，不能认定最高 effort 是体感慢的主因。当时仅观察到四条 CLI 探测合计约 0.3 秒、ACP `initialize` + `authenticate` + `session/new` 合计约 0.3 秒；这只能说明握手探测开销较小，不能归因完整任务耗时。

## 第一轮结论（被复核对象）

第一轮把改进项排成：

1. 给 `await_result` / `status` 加 live 进度，默认等待改为 60 秒。
2. 收缩工具面和 Skill：三步 happy path，降级 `setup` / `spawn_readonly`。
3. worker 启动时隔离用户 Grok profile（system prompt、skills、hooks、plugins、memory）。
4. 验证并收敛 `--deny` / `--disallowed-tools` 到当前 CLI 的真实工具名。
5. 之后才讨论 effort 上限或合并 discovery/task ACP。

并主张：探测缓存对正式 job 基本是死代码；await 反复灌完整模型证明；Skill 仍在教错误调用顺序。

## 改前观察：站得住的判断

### 改前：运行中没有可用进度

`await_result` 只在终态、manager/server 关闭或等待超时时返回。`running` 和 model revision 不得结束等待，也不得触发新 job：

- `mcp/grok_build_server.py`：`await_result` 工具描述
- `scripts/grok_build_bridge.py`：`JobManager.await_result`

改前的 ACP `session/update` 会丢弃 `agent_thought_chunk`，其余类型只保留最后 20 个名字，且要等 job 结束才进入收据（`ACPClient._handle_message`）。改前 `Job.duration_ms` 只在 `_run_job` 的 `finally` 里赋值，运行中的 `status_view()` 一直是 `null`；本轮已补充运行中诊断字段。

「看起来卡住」的观察成立。修法是补充阶段、阶段累计时间、最近安全 ACP 事件及其年龄；不要改成事件一来就结束等待，后一种会打穿反重派契约。工具参数不要原样回传，避免路径进入公开收据。

### 改前：默认 30 秒等待会放大 Codex 往返

机制成立：`DEFAULT_AWAIT_SECONDS = 30`，`MAX_AWAIT_SECONDS = 60`。`docs/USAGE.md` 的示例也写了 `max_wait_seconds: 30`。

这解释的是「Codex 一直在转」，不是「Grok 算得慢」。把它改成默认 60 有风险：上限已经是 60，很像在给宿主 MCP tool timeout 留余量。只有宿主已确认支持且仍留有超时余量时，长任务才适合显式传 60；否则保留默认值。

### 改前：热路径强制刷新探测缓存

`PROBE_CACHE_SECONDS = 300` 存在，但正式 job 和 `setup` 都走 `probe_grok(force=True)`。每次跑 `grok --version`、`--help`、`agent --help`、`grok models`。

这是代码事实。本机这部分约 0.3 秒，不该进第一波。缓存 `--help` / `--version`、继续 live `grok models` + ACP 证明，和 v2 不冲突，只是低收益清理。

### 改前的其他观察

- `spawn_readonly` 没有 `paths`，和 `delegate_readonly` 行为不一致。它是兼容入口，严重度是 nit。
- 测试以 `tests/fake_grok.py` 为主，不能证明真实 CLI sandbox、deny 绑定或 skills/hooks 注入。`tests/real_smoke.py` 先 `setup` 再 spawn，和文档中的正常路径相反。
- 本轮不拆的不变量：live catalog + ACP 证明、禁止 fallback、固定最高 advertised effort、discovery ACP 不发送任务包、implement 的工具限制、MCP 重启不恢复旧 job。它们属于安全与可信度边界，不由低收益清理替代。

## 夸大或证据用错了

### 「Skill 还在教 Codex 走弯路」

不准确。分发 Skill 已经写明 setup 仅作诊断、不要先 setup、正常生命周期是 spawn 后 `await_result`（`skills/delegate-to-grok-build/SKILL.md`）。

真正偏的是 MCP `tools/list` 把 `setup` 放在第一位（`mcp/grok_build_server.py` 中 `TOOLS` 顺序）。这是工具发现顺序问题，不是 Skill 写反。没有 Codex 调用轨迹，不能断言调用方一定先 setup。

### 「await 每次灌完整证明材料」

过重。运行中的 `await_result` 复用 `Job.status_view()`，带的是精简后的 `model_selection`（模型 id、effort 列表、选择策略），不是终态 `result` 里的完整 `model_evidence` / ACP initialize。重复 `loop_guard` 和 `model_selection` 会轻度浪费上下文，不是主因。

### 「worker 没有从用户 Grok profile 里隔离」

第一轮用错了实验：`grok inspect` 看的是交互式 CLI，不是插件那条 `grok agent stdio` 命令。

按插件同款 flags（read-only sandbox、`--no-subagents`、`--disallowed-tools run_terminal_cmd,Agent`、`--deny MCPTool(**)`、`--always-approve`、`--no-leader`、`session/new` 传入空 `mcpServers`）做无 prompt 的 ACP 握手后：

- `mcpServers` 为 `[]`，与 bridge 传入的空列表一致；这只证明本次 session 没有传入 MCP server
- initialize 元数据里看不到项目 `AGENTS.md`、skills、hooks、plugin 名；字段缺失不等于 profile 已隔离，worker 仍继承必要的用户环境（包括 `HOME`）
- `--json-schema` 会暗示 `--output-format json`，那是 headless 文本输出，不能当成 ACP stdio 的现成能力

隔离问题没有被证明。需要另证的是：initialize 仍广告 `/deep-research`、`/workflow`、`/goal`；这些广告及其描述不等于命令已执行，后两者的并行行为以及 `--no-subagents` 是否挡得住它们，都没有 `session/prompt` 证据。当前 CLI help 里也没有 `--no-memory`；收据上的 `fresh_session_without_memory_opt_in` 只表示 bridge 没有传递关闭记忆的 flag，不表示 profile 或持久记忆已隔离。即使未来支持 `--no-memory`，传递 flag 也仍需运行时证据确认。

### 「用 `--json-schema` 结构化答案」

在当前 ACP 路径上证据不足，还可能把 stdio 协议打坏。未证明 `grok agent stdio` 接受该 flag 之前，不应进入待办。

### 把 `gpt-5.6-luna` 写成和「不写死 Grok 版本」同类问题

不成立。本插件面向 Codex，Luna Max 独立 review 是 v2 核验契约的一部分。Grok 版本动态选择，是因为 runtime catalog 会变。Codex reviewer 名字以后会过期，那是文档维护项，不是 bridge bug。

## 推断，不能当缺陷

| 说法 | 实际地位 |
| --- | --- |
| Codex 高频调用 `status` 或先 `setup` | 合理担忧，无调用日志 |
| `--deny Edit(**)` 等规则对当前 CLI 无效 | 假说。`--deny` 的 help 就是 permission rule；未做绑定实验 |
| `queued` 和 `running` 分不清 | 代码上 `JobManager(max_workers=2)` 时，第三个 job 会停在 `queued`；单 job 日常路径几乎碰不到 |
| Windows 没有稳定 cwd fd | 真边界（`E_CWD_FD_UNSUPPORTED`），不是当前 macOS 调用路径的问题 |
| `usage.estimated_codex_turns_avoided` 恒为 `null` | nit，占位字段 |

`--deny` 与 `--disallowed-tools` 是两套机制：前者是 permission rule（help 写明 compat alias `--disallowedTools`），后者是内置工具名列表。当前 bridge 只是向 CLI 请求按工具 ID 移除 `run_terminal_cmd,Agent`；fake 测试只能证明 argv，不能证明真实 CLI 已执行禁用。deny 列表是否按预期绑定，需要针对当前 CLI 另做实验，不能从规则长得像另一套产品语法直接推断无效。

人类可读的 `grok models` 解析（`_parse_model_catalog`）是 catalog 半边的脆弱点，但不是唯一证明面。每个 job 还要求 ACP initialize 的 runtime default 与 catalog default 一致。这是同一 CLI 的运行时一致性证据，不保证 provider 或答案正确性；completion model/effort 缺失时也不能声称完成端到端 attestation。CLI 改版式会 fail-closed，这是运维风险，不是「只有这一处解析」。

## 修正后的优先级

若要把结论变成改动，按这次复核而不是第一轮排期：

1. **保留并核验诊断载荷**（`phase`、已运行毫秒、阶段累计时间、最近安全 ACP 事件和事件年龄），保持「revision 不结束等待」。这是唯一既有代码证据、又对准「以为卡住」、还不碰 v2 不变量的项。
2. **Skill 与工具描述**：只有宿主已确认支持且留有超时余量时，长任务才传 `max_wait_seconds: 60`；不要改 `DEFAULT_AWAIT_SECONDS`。`setup` 可以保留，但不要排在 `tools/list` 第一位。
3. **隔离**：先验证 worker 在 `session/prompt` 下能否调用 `/deep-research` 和 `/workflow`；不要基于 `grok inspect` 去关 skills/hooks。
4. 探测缓存、await 收据裁剪、`spawn_readonly` 补 `paths`，本轮不实施；后续只能作为独立清理评估。
5. 调用方可选的 effort 上限、合并 discovery/task ACP，继续当作契约变更，不进这轮。

## 明确不在本轮

- 真实 Grok smoke test（会联网并消耗请求）
- 安装、提交、推送、发布
- 降低默认 reasoning effort 或写死模型版本
- 让 await 在内部 revision 变化时提前返回
- 把 `--json-schema` 接到 ACP stdio

## 诊断字段契约（本轮已实施）

status、await_result 的等待超时和终态响应，以及 result 的终态收据，按以下规则提供诊断字段：

- `phase` 为 `queued`、`probe`、`discovery`、`prompting` 或 `running`；终态保留最后执行阶段。
- `duration_ms` 在排队时为 `null`，运行中实时递增，终态冻结；只计算执行时间，不含排队。
- `phase_timings_ms` 是已进入阶段的累计毫秒，包含 `queued`；当前阶段实时更新，终态冻结。
- `last_update_type` 只允许安全白名单中的非 thought ACP 事件名；它不是实际工具进度。
- `last_activity_age_ms` 是距最近安全 ACP 事件的毫秒数；没有事件时为 `null`，终态冻结。
- 等待语义不变：仍只在终态、关闭或 `max_wait_seconds` 到期时返回。
- 公开载荷继续走现有脱敏；不回传工具参数、绝对路径或 thought。安全真实验证仍待明确授权，本轮不执行。
