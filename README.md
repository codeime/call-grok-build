# Call Grok Build

Call Grok Build 是一个 Codex 插件。它让 Codex 主代理或具备插件工具访问权的 Codex subagent，在当前 Codex workspace 中直接启动本机 Grok Build，处理研究、计划、代码审查和实现任务，再由 Codex 独立核验结果。

插件的定位很简单：Grok 在你当前打开的项目目录工作，Codex 负责组织任务、等待结果、运行验证和做最终判断。不会为了委托任务复制项目、创建临时目录或要求用户准备额外 Git 目录。

## 主要行为

- Codex 宿主把当前 workspace 的绝对路径传给插件；Grok 使用同一个目录作为进程 cwd。
- 所有路由都是 direct。可选的 paths 只用于告诉 Grok 关注哪些相对路径，不是访问控制，也不会把代码复制到另一目录。
- read-only 任务使用 read-only sandbox；implement 使用 workspace sandbox。Grok 的 terminal 和 Agent 工具始终禁用。
- bridge 不扫描 Git 或整个目录内容；结果中的 workspace.integrity_snapshot 固定为 not_collected。
- implement 可以在当前目录直接工作，包括 primary checkout、已有修改和非 Git 目录。Codex 会在任务前记录工作区现状，任务后运行测试，并交给 gpt-5.6-luna 的 max reasoning 做独立 review；bridge 不声称能自动区分哪些修改来自 Grok。
- 用户明确点名 Grok Build 后，该请求覆盖同一当前 workspace 中完成这次有界流程所需的代码和上下文，不会仅因仓库是私有仓库或内容会发送到 xAI 再次询问。

## 要求

- 支持插件的 Codex 版本。
- Python 3.9 或更高版本；运行时只使用 Python 标准库。
- PATH 中可调用 Grok Build CLI，命令名为 grok。
- Grok CLI 已登录，并能刷新模型目录及建立 ACP 连接。

首次使用可以检查：

~~~text
grok --version
grok login
grok models
~~~

## 安装与更新

1. 获取本插件目录或发布压缩包。插件根目录必须包含 .codex-plugin/plugin.json。
2. 在 Codex 的插件管理界面安装本地目录或压缩包。分发配置只使用相对资源路径，不要把某台机器的绝对路径写进 manifest。
3. 按 Codex 提示重新加载插件；更新后新建一个 Codex task，确保新 task 加载当前版本。
4. 不要直接编辑 Codex 生成的缓存目录。

插件不会替用户提交、推送、合并或发布代码。代码是否提交以及如何交付，仍由正常的 Codex/Git 流程决定。

## 快速开始

在需要委托的 Codex task 中直接描述目标即可。通常不需要手写 cwd，插件会使用当前 workspace：

~~~text
使用 Grok Build review 当前项目的缓存失效逻辑。
只报告有证据的 finding，给出 file:line、影响和修复建议。
Codex 随后复现 high/critical 结论。
~~~

只读正常流程是 delegate_readonly 返回 job_id，再由同一个调用者使用 await_result 等待结果。不需要先 setup，也不要因为状态变化反复创建 job。setup 仅用于诊断 CLI 和模型环境。

## Codex subagent 调用

如果宿主把本插件的 Skill/MCP tools 暴露给 Codex subagent，subagent 可以直接持有一次完整调用生命周期：

~~~text
让一个 Codex subagent 使用 Grok Build review 当前 workspace 的认证边界。
它负责发起任务、保存准确 job_id、等待终态并回传结果和独立核验证据。
父任务不要为同一目标重复提交。
~~~

主代理和 subagent 使用同一套工具契约。发起调用的代理负责同一 MCP 连接中的 job_id、等待、结果读取和取消；连接结束后不要让其他进程接力旧 job。Grok 自己的 Agent/subagent 始终禁用，任何层都不能递归调用本插件。

## 四类任务

| 模式 | 用途 | 是否修改当前 workspace |
| --- | --- | --- |
| research | 资料研究、事实核查和方案比较 | 否 |
| plan | 实现拆解、迁移、测试和风险计划 | 否 |
| review | 代码审查、风险识别和回归检查 | 否 |
| implement | 在当前 workspace 实现明确变更 | 是 |

研究示例：

~~~text
让 Grok Build 研究目标主题，返回可核验来源 URL。
区分事实、推断和待确认事项，Codex 复核关键来源。
~~~

计划示例：

~~~text
让 Grok Build 为当前项目制定实现计划，不修改文件。
计划包含范围、假设、步骤、测试、回滚、风险和验收标准。
~~~

审查示例：

~~~text
让 Grok Build review 当前代码，只报告有证据的 actionable findings。
按严重度排序，并给出 file:line、影响和修复建议。
~~~

实现示例：

~~~text
让 Grok Build 在当前 workspace 实现这个变更。
只修改完成目标所需的文件，不要提交、推送、合并、变基或调用 Agent。
完成后 Codex 运行相关测试，并让 gpt-5.6-luna 使用 max reasoning 独立 review 实际 diff。
~~~

implement 允许当前目录已有 staged、unstaged 或 untracked 修改，也允许非 Git 目录。开始前和结束后应由 Codex 记录并比较工作区状态；如果 Luna 返回 needs_changes，最多安排一次有明确 parent 的 correction，再做一次 Grok 回归复审和一次 Luna Max 终审。失败、取消、超时或空结果不会自动重试或重新委派。

## 调用结构

~~~text
Codex task（主代理或具备插件工具访问权的 subagent）
    │ 当前 workspace 的绝对 cwd
    ▼
Call Grok Build MCP server
    ├─ delegate_readonly：research / plan / review
    ├─ spawn_worker：implement
    ├─ await_result：有界等待
    ├─ result：按需读取完整收据
    └─ cancel：只取消准确 job
    ▼
Grok Build CLI / ACP stdio
    ▼
当前 workspace 中的 Grok 结果或修改
    ▼
Codex 独立核验；实现还需 Luna Max
~~~

每个 job 先启动一次无 session、无 prompt 的 discovery ACP 进程，用于证明 runtime model/effort；随后只启动一个 task ACP 进程，其中只有一个 session 和一条 prompt。discovery 不发送任务包或仓库内容。Codex 可以等待同一 job 的结果，但内部 revision 变化不代表需要重新调用 Grok。正常回答默认限制为 16,000 字符，await_result 默认只带 12,000 字符答案页；需要更长原文时才显式分页读取，避免把大段结果反复塞回 Codex 上下文。

## 动态模型选择

插件不写死 Grok 版本，也不根据版本号猜测强弱。每个 job 都刷新 grok models，并通过 ACP initialize 元数据确认 provider/runtime default model，然后从该模型实际广告的 reasoning effort 中选最高档位：

~~~text
xhigh > high > medium > low > none
~~~

目录默认模型、ACP runtime default、完成信息或 model-switch 事件不一致时，结果保持未验证并失败关闭。插件不会静默 fallback，也不会臆造 fast 参数；实际模型和 effort 以本次 job 收据为准。

## 交叉验证

- 研究：Codex 打开并核对关键来源，区分事实、推断和未验证说法。
- 计划：Codex 检查范围、假设、迁移、回滚、测试和破坏性风险。
- review：Codex 从源码和测试复现 high/critical finding。
- implement：Codex 运行测试，检查任务前后实际 diff，再由只读 gpt-5.6-luna、max reasoning 独立 review 原始需求、验收标准、实际 diff 和测试证据。

Grok 的答案、finding 和修改都只是候选结果。收据格式正确不等于内容正确；结果在 Codex 完成独立核验前保持 verified: false。

## 循环、并发和停止

- 同一个 workspace 同时只允许一个活动 implement job；read-only job 可以并行。
- 每个 job 默认 24 turns、默认 30 分钟；硬上限分别为 48 turns 和 60 分钟。输出有独立上限。
- 单 job 只有一个 ACP prompt；自动重试和自动重新委派为零。
- Luna 发现问题后最多一次 correction 和一次回归复审；这不是循环许可。
- cancel 只终止准确 job 创建的进程组，不使用模糊进程匹配。
- 取消、超时、模型切换、turn/output limit、空回答或 MCP 进程关闭都不会被报告为成功。

结果 envelope 为 grok.codex.result.v2。workspace.integrity_snapshot 为 not_collected；这表示 bridge 不冒充变更归因，implement 的变更核验由 Codex 依据任务前后状态、测试和独立 review 完成。

## 数据边界

Grok CLI 在本机运行，但 prompt、被选择的代码上下文和模型服务请求会发送到 xAI。不要把密钥、cookie、token、.env、SSH 私钥、证书、生产凭据、客户数据或无关个人资料放进任务范围。bridge 使用最小环境变量，拒绝向 worker 转发常见认证变量，并在公开收据、错误和输出中做凭据与账号路径脱敏。

文件名和内容仍可能暴露项目身份；脱敏不是内容匿名化。任务应使用当前 workspace 中与目标直接相关的范围。paths 只能提示 Grok 关注范围，不能替代敏感文件判断。

## 故障排查与开发

- CLI、登录、模型目录、ACP 或任务状态问题：参阅 [故障排查](docs/TROUBLESHOOTING.md)。
- 工具参数、任务包和收据解释：参阅 [使用指南](docs/USAGE.md)。
- 本地测试、MCP stdio 调试和发布检查：参阅 [开发与测试](docs/DEVELOPMENT.md)。
- 数据、文件和进程边界：参阅 [安全边界](docs/SECURITY.md)。

最小单元测试入口：

~~~text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
~~~
