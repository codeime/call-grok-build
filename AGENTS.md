# Call Grok Build 协作说明

本文件适用于整个仓库。若子目录以后增加更具体的 AGENTS.md，以离目标文件最近的规则为准。

## 项目定位

本仓库提供一个 Codex 插件，通过当前 Codex workspace 中的本机 Grok Build CLI 执行有边界的研究、计划、代码审查和实现。Grok 的回答和修改都是待核验的候选结果；范围控制、测试、独立审查和最终交付由 Codex 负责。

## 事实源与项目 Skill

开始修改前按任务读取相关文档：

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)：开发、目录和测试
- [docs/SECURITY.md](docs/SECURITY.md)：数据、文件和进程边界
- [docs/USAGE.md](docs/USAGE.md)：调用协议
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)：错误和恢复
- [skills/delegate-to-grok-build/SKILL.md](skills/delegate-to-grok-build/SKILL.md)：插件调用方 Skill

维护任务可使用：

- [bridge-change-review](.agents/skills/bridge-change-review/SKILL.md)：审查 MCP、bridge、生命周期、模型证明和直接 cwd 边界
- [distribution-privacy-audit](.agents/skills/distribution-privacy-audit/SKILL.md)：审查硬路径、用户名、邮箱、凭据形状、会话信息和分发资源元数据

.agents/skills/ 面向仓库维护者；skills/delegate-to-grok-build/ 随插件分发，面向调用插件的 Codex。不要把两套职责混在一起。

## 协作与授权

- “review”“审查”“检查”默认只读；只有用户明确要求修复或实现时才修改。
- 保留用户已有变更，不还原、不覆盖无关内容。没有 Git 元数据时，不要声称检查了提交、分支或历史。
- 当前用户明确点名 Grok Build，且目标就是当前 workspace 时，这条请求已授权同一有界流程将完成任务所需的代码和上下文发送给 xAI。不要仅因仓库是私有仓库、内容会发送到 xAI 或任务会修改当前目录而再次询问。
- 该授权覆盖初次 job；如果 Luna 发现问题，也覆盖同一 cwd 内最多一次 correction、一次 Grok 回归复审和一次 Luna Max 终审。它不覆盖密钥、客户/第三方数据、任务外目录、额外外部操作、自动重试或重新委派。
- 安装、更新缓存、提交、推送、发布和真实 Grok smoke test 仍需要明确授权。普通委托不会替用户执行这些动作。
- 一个 Grok job 只有一个生命周期负责人。不要自动重试、自动重新委派、高频轮询或递归调用本插件。
- Codex subagent 只有在宿主确实暴露本插件工具时才能直接调用；Grok 的 Agent/subagent 始终禁用。

## v2 运行时契约

- Codex 宿主把当前 workspace 的绝对 cwd 传给插件。Grok 在同一个 cwd 原生启动，所有路由都是 direct。
- 插件不创建或要求额外目录，不复制项目，不切换辅助目录，也不扫描 Git 或整个目录内容。
- paths 仅是 prompt 关注范围提示，不是访问控制；不能把它当作敏感文件过滤或变更隔离。
- research、plan、review 使用 read-only sandbox；implement 使用 workspace sandbox，可以作用于 primary checkout、已有修改和非 Git 目录。
- bridge 不声称能区分任务前已存在的修改与 Grok 新增的修改。Codex 任务前记录状态，任务后运行测试并检查实际 diff，再安排独立 Luna Max review。
- 结果必须使用 grok.codex.result.v2；workspace.integrity_snapshot 为 not_collected。
- 模型来自每次 job 的实时 catalog 与 ACP runtime default 共同证明；选择当前模型实际广告的最高 effort，不写死某个版本，不接受 fallback 或静默 model switch。
- read-only/workspace sandbox、真实工具 ID run_terminal_cmd,Agent 禁用、最小环境、敏感文件拒绝、turn/time/output ceiling、准确 cancel、同 cwd implement 互斥和 fail-closed 模型检查不可移除。
- 单 job 允许一个无 session/prompt 的 discovery ACP 进程，以及一个只有单 session/单 prompt 的 task ACP 进程；失败、取消、超时、空结果、模型切换或限制达到后不得自动再试。
- Luna 的修改意见最多触发一次 correction 和一次回归复审；第二次仍需修改时停止并报告 unverified。

## 文件边界与隐私

- 不把 API key、密码、token、cookie、.env、SSH 私钥、证书、生产凭据、客户数据或无关个人资料放入 Grok 任务。
- worker 只接收完成任务所需的最小环境；认证缓存由 Grok CLI 自己管理，bridge 不转发常见认证变量。
- 公开结果、错误和诊断递归脱敏账号路径、邮箱、URL userinfo、认证头和常见凭据形状，但脱敏不等于代码内容匿名化。
- 任务 prompt 不应重复输出本机绝对路径；公开收据使用 cwd: "." 和稳定的有界字段。

## 开发验证

修改 MCP、bridge、Skill 或文档后，先运行与改动直接相关的检查，再运行：

~~~text
python3 -m py_compile mcp/grok_build_server.py scripts/grok_build_bridge.py tests/*.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
~~~

fake Grok 只能证明参数、协议和收据行为，不能证明真实 Grok CLI 的 sandbox。真实 smoke test 会联网并消耗请求，只有用户明确授权时才运行。

提交前检查：

- manifest 的相对资源路径、Skill front matter 和 MCP 配置有效；
- 文档、Skill 和 manifest 使用 v2 direct cwd 契约；
- 不含真实用户名、邮箱、机器名、会话路径、凭据或本机绝对路径；
- 变更保留现有用户修改；不要顺手提交、推送或发布。
