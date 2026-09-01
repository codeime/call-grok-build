# 安全边界参考

在委托私有代码、实现变更或处理可能外发的上下文前阅读本文。Call Grok Build 的边界是纵深防御；最终授权和结果判断由 Codex 负责。

## 外发授权

当前用户明确点名 Grok Build，且目标是当前 Codex workspace 时，该请求足以授权一次有界流程把完成任务所需的代码和上下文发送给 xAI。不要仅因仓库私有、内容会发送到 xAI、任务会写文件或调用者是 subagent 而再次询问。

这条授权覆盖初次 job，以及同一 cwd 内最多一次 correction、一次 Grok 回归复审和一次 Luna Max 终审。它不覆盖密钥、客户/第三方数据、任务外目录、额外外部操作、自动重试或重新委派。

## cwd 与数据

- Codex 宿主将当前 workspace 的绝对 cwd 传给插件；Grok 使用同一个 cwd 启动。
- bridge 不复制项目、不创建临时副本、不切换辅助目录，也不做 Git 或全目录内容扫描。
- paths 只作为 prompt 关注范围提示，不是访问控制；它不能隐藏敏感文件，不能改变 cwd。
- 不要把 API key、密码、token、cookie、session、.env、SSH 私钥、证书、生产凭据、客户数据、第三方私有代码或无关个人资料放入任务包。
- 根目录、当前账号 home、账号父级、其他账号目录和明显系统配置目录不能作为 cwd。

Grok 任务的 prompt、被选择的代码上下文、文件名、工具输出和模型请求可能发送到 xAI。文件内容即使没有绝对路径，也可能暴露项目身份；收据脱敏不是内容匿名化。

## Sandbox 与工具

- research、plan、review 使用 read-only sandbox；
- implement 使用 workspace sandbox，直接作用于当前 workspace；
- 真实工具 ID run_terminal_cmd 和 Agent 始终禁用；
- Grok 不能运行 shell、解释器、Git、测试，不能调用本插件或再创建代理；
- 测试、实际 diff 检查和最终审查由 Codex 在 Grok 返回后完成。

## 环境与凭据

bridge 使用最小 worker 环境。认证由 Grok CLI 自己管理，不把 XAI_API_KEY、代理凭据、USER、LOGNAME 或其他无关身份变量转发给 worker。公开结果、错误和诊断会递归脱敏账号路径、邮箱、URL userinfo、认证头和常见凭据形状。

每个任务使用全新的 ACP session。若 CLI 支持 no-memory 选项，bridge 会关闭会话记忆；否则只报告 fresh session without memory opt-in，不作超出收据的记忆隔离承诺。

## 模型

每个 job 刷新实时模型目录，并通过 ACP initialize 确认 provider/runtime default model。只从该模型实际广告的 effort 中选最高档位：

~~~text
xhigh > high > medium > low > none
~~~

不得写死版本、根据名字猜测强弱、静默接受 fallback 或忽略 model-switch。catalog、ACP、完成信息或 model-switch 不一致时，结果保持未验证并失败关闭。

## 写入与归因

implement 允许 primary checkout、已有 staged/unstaged/untracked 修改和非 Git 目录。bridge 不扫描 workspace 内容，结果中的 workspace.integrity_snapshot 固定为 not_collected；因此它不声称能自动区分任务前已有修改与 Grok 新增修改。

Codex 必须在任务前记录当前状态，任务后检查实际 diff、运行测试，并让只读 gpt-5.6-luna 使用 max reasoning 独立 review 原始需求、验收标准、实际 diff 和测试证据。验证通过前，收据中的 verified 保持 false。

## 生命周期

- 一个 job 只有一个无 session/prompt 的 discovery ACP 进程，以及一个单 session/单 prompt 的 task ACP 进程；discovery 不发送任务包或仓库内容；
- 同一 cwd 同时只允许一个 implement job；
- 默认 24 turns、30 分钟，硬上限 48 turns、60 分钟，输出也有界；
- 失败、超时、取消、空回答、模型切换或达到限制后不会自动重试或重新委派；
- Luna 的 needs_changes 最多触发一次 correction 和一次回归复审；
- cancel 只终止准确 job 创建的进程组，不使用模糊进程匹配。

正常关闭会尝试清理进程组；宿主强制终止、崩溃或断电时可能留下孤儿进程，这是残余风险。
