# 安全边界

本文说明 Call Grok Build 在当前 Codex workspace 中直接运行 Grok 时的安全边界。它用于控制任务范围、工具能力、凭据和进程生命周期；最终是否接受答案或修改，仍由 Codex 判断。

## 数据流

~~~text
Codex task（主代理或具备插件工具访问权的 subagent）
    │ 当前 workspace、任务包和必要上下文
    ▼
MCP server / bridge
    │ 同一个 cwd 启动 Grok Build CLI
    ▼
Grok Build CLI / ACP ──────► xAI 模型服务
    │ 在当前 workspace 中读取或修改
    ▼
有界回答与 v2 收据
    ▼
Codex 独立核验
~~~

Grok CLI 在本机运行，但 prompt、被选择的代码上下文、工具输出和模型请求可能发送到 xAI。任何放入任务包或被 Grok 读取的内容都应按可能离开本机来评估。

## 授权

当前用户明确点名 Grok Build，且目标为当前 Codex workspace 时，这条请求已经授权本次有界流程发送完成任务所需的代码与上下文。不要仅因为仓库是私有仓库、内容会发送到 xAI、任务是 implement 或调用者是 subagent 而再次询问。

同一请求的授权覆盖初次 job；如果 Luna 发现问题，也覆盖同一 cwd 内最多一次 correction、一次 Grok 回归复审和一次 Luna Max 终审。授权不包含密钥、客户或第三方数据、任务外目录、额外外部操作、自动重试或重新委派。用户没有明确点名 Grok Build 时，不应擅自发起外发请求。

## 当前目录

- Codex 宿主负责传入当前 workspace 的绝对 cwd；Grok 使用相同的 cwd 启动。
- bridge 保留稳定目录句柄，避免排队期间路径被替换到另一个目录。
- bridge 不复制项目，不创建临时副本，不切换到辅助目录，也不通过 Git 或全目录内容扫描决定是否可启动。
- implement 可以作用于主工作目录、已有 staged/unstaged/untracked 修改和非 Git 目录。bridge 不声称能自动区分任务前已有修改与 Grok 新增修改。
- paths 只用于 prompt 关注范围，不是访问控制。不要把它当作敏感文件过滤或变更隔离。
- 根目录、当前账号 home、账号父级、其他账号目录和明显的系统配置目录仍会被 cwd 边界拒绝；目标应是具体项目或资料目录。

## Grok 工具能力

research、plan 和 review 使用 Grok CLI 的 read-only sandbox；implement 使用 workspace sandbox。三个任务入口的 `allow_subagents` 默认是 `false`，只有显式为 `true` 时才允许 Grok 在同一 job/cwd 内使用内置 Agent。bridge 仍向 CLI 请求禁用 `run_terminal_cmd`、外部 MCP 和顶层递归调用本插件；fake 测试不能证明真实 CLI 的子 agent 约束继承、清理或事件来源：

- 任务禁止 Grok 通过本插件递归调用另一个 Grok job；
- 任务禁止 Grok 创建新的顶层 Codex/Grok 任务或调用外部 Agent；显式开启时只能在当前 parent job/cwd 内使用内置 Agent；
- implement 的测试、Git 查询和最终 diff 检查由 Codex 在 Grok 返回后执行；
- 任务 prompt 只描述目标、范围、约束和验收标准，不转发整段对话。

CLI sandbox 是纵深防御，不能代替 Codex 对任务包和结果的独立判断。bridge 也会拒绝常见的外部编辑、远程操作和敏感文件读取请求；如果 CLI 能力不足，任务失败关闭。

## 不应发送的内容

任务范围内不要放入：

- API key、密码、token、cookie、session 文件和 .env 内容；
- SSH 私钥、签名密钥、证书、生产凭据和云平台私密配置；
- 浏览器配置、个人文档、与任务无关的账号目录内容；
- 未获授权的第三方私有代码、客户数据、生产数据和受监管数据；
- 能绕过访问控制、扩大 cwd 范围或执行额外外部操作的指令。

bridge 会按文件名和内容形状拒绝或脱敏常见凭据，但主动缩小任务范围仍是调用者责任。

## 环境与认证

- 使用已登录的 Grok CLI 缓存认证；bridge 不把 XAI_API_KEY、代理凭据或其他认证变量转发给 worker。
- worker 只接收完成任务所需的最小环境变量，包括必要的 HOME、PATH、TMPDIR、locale/terminal 和 TLS 证书设置。
- 每个任务使用全新的 ACP session；如果 CLI 支持 no-memory 选项，bridge 只会传递该 flag，不能据此保证用户 profile、hooks、skills、plugins 或持久记忆已隔离；否则收据只记录 `fresh_session_without_memory_opt_in`，不作记忆隔离承诺。
- 公开 result、status、错误和诊断会递归处理账号路径、邮箱、URL userinfo、认证头和常见凭据形状。
- 脱敏只保护收据和输出中的已知形状；代码内容、文件名、模型上下文和本机进程参数仍可能暴露项目身份。

profile、记忆、slash command、deny/tool ID 的真实 CLI 行为，以及 `--json-schema` 与 ACP stdio 的兼容性，需用户明确授权后再做受控验证；本轮不执行真实任务或联网探测。

## 模型证明

插件不写死某个 Grok 版本，也不根据版本字符串猜测最强模型。每个 job 都刷新实时模型目录，并通过 ACP initialize 元数据确认 provider/runtime default model，再从该模型实际广告的 effort 中选择最高档位：

~~~text
xhigh > high > medium > low > none
~~~

catalog、ACP runtime、可用的完成信息或 model-switch 事件不一致时，任务失败关闭或保持 unverified；不得静默 fallback。这是同一 CLI 的运行时一致性证据，不是独立或加密的 provider 证明。实际 model 和 reasoning_effort 以 v2 收据为准。

## 变更与核验

bridge 不扫描 Git 或整个目录内容，v2 收据中的 workspace.integrity_snapshot 固定为 not_collected。这样可以保持与用户在当前目录直接启动 Grok 的行为一致，也不假装能把每一行修改归因给 Grok。

implement 的安全流程由 Codex 负责：

1. 任务前记录当前目录的 status、diff 和必要的文件状态；
2. Grok 在同一 cwd 修改文件；
3. 任务后检查实际 diff 并运行相关测试；
4. 使用只读 gpt-5.6-luna、max reasoning，基于原始需求、验收标准、实际 diff 和测试证据独立 review；
5. 只有独立核验完成后，才决定是否人工提交或交付。

任务前已有变更、编辑器自动保存、构建工具和并发进程都可能影响结果。Codex 不能把所有 diff 自动归因给 Grok。

## 循环、并发与取消

- bridge 最多同时执行 4 个独立顶层 job；一个 job 先使用一个无 session/prompt 的 discovery ACP 进程做模型一致性证明，再使用一个只有单 session/单 prompt 的 parent task ACP 进程；discovery 不接收任务包或仓库内容。
- `allow_subagents=true` 时，内置 Agent 在 parent job/cwd 内共享 workspace。parent sandbox、范围、显式 timeout、turn/output 约束仍是 bridge 契约，但 bridge 不宣称 child runtime 已继承这些约束；bridge 也不设置或观测子 agent 数量，不把它们当作顶层 job。
- 同一个 cwd 同时只允许一个活动 implement job；read-only job 可以并行。
- `timeout_seconds` 可省略或为 null，表示 job 不设执行截止；显式整数必须至少为 10，且没有 bridge 额外的最大值。`await_result` 的 1–60 秒只是单次等待窗口，不会终止 job；setup/probe 仍有各自本地边界。
- 无限期 job 若 CLI 一直不完成，会持续占用一个执行槽位；四个槽位都被占用时，新 job 只能排队，bridge 不会替调用方设置 watchdog 或自动取消。生命周期负责人应保存准确 job_id，并在需要停止时使用 `cancel`；正常关闭也会尝试清理当前进程组。
- 失败、超时、取消、模型切换、空结果和达到限制后不会自动重试或重新委派。
- Luna 的 needs_changes 最多触发一次 correction 和一次回归复审；第二次仍需修改时停止并保持 unverified。
- cancel 只终止准确 job 创建的进程组，不使用 pkill、killall 或模糊进程匹配。
- MCP server 正常关闭会尝试清理对应进程组；宿主被强制终止、崩溃或断电时可能留下孤儿进程。启用内置 Agent 后，fake 测试不能证明子 agent 的清理或事件来源，这一残余风险不能被描述成绝对隔离。

## 结果收据

结果 envelope 是 grok.codex.result.v2。公开字段会使用 cwd: "."，避免把本机绝对路径写进普通响应。workspace.integrity_snapshot 为 not_collected；verification.verified 在 Codex 完成独立核验前为 false。

收据中的 model_evidence、sandbox、loop_guard、usage 和 errors 用于审计本次运行，不是对 Grok 结论正确性的保证。答案、finding、测试和修复必须由 Codex 独立验证。

`status`、`await_result` 的等待超时和终态响应，以及终态 `result` 会提供 `phase`、`duration_ms`、`phase_timings_ms`、`last_update_type` 和 `last_activity_age_ms`。这些字段只描述 bridge/ACP 活动，不是实际工具进度或安全隔离证明。
