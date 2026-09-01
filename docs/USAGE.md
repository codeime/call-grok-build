# 使用指南

本文说明如何把任务交给 Grok Build、如何等待结果，以及如何让 Codex 独立判断。日常使用只需要在当前 Codex workspace 中描述目标，插件会把这个 workspace 作为 Grok 的工作目录。

## 开始前

Codex 宿主会自动传入当前 workspace 的绝对 cwd。不要为了调用 Grok 复制项目、准备额外目录或改变当前 Git 状态。直接使用当前打开的项目目录即可。

确认 Grok CLI 已安装并登录：

~~~text
grok --version
grok login
grok models
~~~

用户在当前 task 中明确点名 Grok Build 后，该请求已经授权本次有界流程将完成任务所需的当前 workspace 代码和上下文发送给 xAI。不会仅因仓库是私有仓库、任务会修改当前目录或当前内容来自 subagent 而再次询问。授权不包含密钥、客户数据、任务外目录和额外外部操作。

## 任务包

建议把任务写成以下五段：

~~~text
Goal:
Scope:
Constraints:
Acceptance criteria:
Evidence/output required:
~~~

Scope 可以列出相对文件或目录，帮助 Grok 聚焦；它只是 prompt 提示，不是文件访问控制。不要把秘密、无关个人资料或完整历史对话塞进任务包。

## 选择模式

| 模式 | 用途 | sandbox | 是否修改当前 workspace |
| --- | --- | --- | --- |
| research | 外部资料、事实核查、方案比较 | read-only | 否 |
| plan | 实现拆解、迁移、测试和风险计划 | read-only | 否 |
| review | 代码审查、风险识别和回归检查 | read-only | 否 |
| implement | 在当前目录实现明确变更 | workspace | 是 |

所有模式都在同一个当前 cwd 启动 Grok。bridge 不复制代码，也不通过目录内容扫描来决定能否启动。

## 只读任务

推荐自然语言：

~~~text
使用 Grok Build review 当前 workspace 的缓存失效逻辑。
只报告有证据的 finding，按严重度排序，给出 file:line、影响和修复建议。
Codex 负责复现 high/critical 结论。
~~~

高阶入口是 delegate_readonly，支持 research、plan、review，并立即返回 job_id。paths 省略或传入非空相对列表都保持 direct；传入 paths 只改变 prompt 中的关注范围，不改变 cwd，不复制文件，也不改变访问边界。

spawn_readonly 是兼容入口，适合需要显式传入参数的调用。新调用优先使用 delegate_readonly。

## 实现任务

~~~text
让 Grok Build 在当前 workspace 实现这个变更。
只修改完成目标所需的文件，不要提交、推送、合并、变基或调用 Agent。
完成后由 Codex 运行相关测试，并让 gpt-5.6-luna 使用 max reasoning 独立 review 实际 diff。
~~~

implement 使用 workspace sandbox，可以作用于 primary checkout、已有 staged/unstaged/untracked 修改和非 Git 目录。bridge 不创建额外目录，也不声称能自动区分任务前已有修改与 Grok 新增修改。Codex 应在任务前记录状态，任务后检查实际 diff 并运行测试。

如果 Luna 返回 needs_changes，Codex 最多发起一次带明确 parent 的 correction，并完成一次 Grok 回归复审和一次 Luna Max 终审。失败、取消、超时或模型切换不会自动重试或重新委派。

## MCP 工具契约

正常只读流程：

~~~text
delegate_readonly({"mode":"review","task":"...","cwd":"当前 workspace"})
await_result({"job_id":"<job-id>","after_revision":0,"max_wait_seconds":30})
~~~

正常实现流程是 spawn_worker 后使用 await_result。发起调用的代理必须保存准确 job_id，并在同一个 MCP 连接中等待、读取结果或取消。

### delegate_readonly

立即返回 job_id，不要求先 setup。mode 只能是 research、plan 或 review。可选参数包括 timeout_seconds、max_output_chars、web_access、max_turns 和 paths。默认公开回答上限为 16,000 字符；research 默认开启 web search，其他模式默认关闭；需要时在任务包中明确说明。

### await_result

一次等待最多 60 秒，正常结果页默认最多返回 12,000 字符。短任务直接等待终态；长任务只在本次等待超时后，对同一个 job 再等待。running 或 model revision 变化不代表要创建新 job。终态调用幂等；需要更长原文时再显式调用 result，并用 offset/limit 分页。

### setup

setup 是可选诊断，用于刷新模型目录并做无 prompt 的 ACP 初始化证明。每个实际 job 仍会自己刷新目录，并先运行一个无 session/prompt 的 discovery ACP 进程，再运行一个只有单 session/单 prompt 的 task ACP 进程，因此不需要为了正常调用提前 setup。discovery 不发送任务包或仓库内容。

### spawn_worker

spawn_worker 启动 implement 任务，使用当前 workspace 的 workspace sandbox。可选 correction_of_job_id 只用于 Luna 指出的那一次有界修复，必须指向紧邻的成功 implement job。bridge 最多接受一次 correction，不接受失败重试、分支或递归委派。

### status、result、list、cancel

- status 返回一个 job 的 compact 状态，适合诊断，不应高频轮询。
- result 返回完整结果收据；答案较大时按 offset/limit 分页。
- list 只列出当前 MCP server 进程内的 job。
- cancel 只取消准确 job 创建的进程组；不要使用模糊进程匹配。

## 结果收据

结果使用 grok.codex.result.v2。重要字段包括：

| 字段 | 含义 |
| --- | --- |
| model、reasoning_effort | 本次实际选择并锁定的模型和最高可用 effort |
| model_evidence | catalog、ACP runtime 和完成信息的证明 |
| route | 固定为 direct |
| cwd | 公开结果中的脱敏标签，通常为 . |
| sandbox | read-only 或 workspace |
| workspace.integrity_snapshot | 固定为 not_collected |
| loop_guard | 单 prompt、turn、时间、输出和 correction 护栏 |
| verification | schema_valid、review_required 和 verified 状态 |

schema_valid 为 true 只表示格式正确。verified 在 Codex 完成独立核验前保持 false。

## Codex subagent

如果宿主向 Codex subagent 暴露本插件工具，subagent 可以直接成为一次 job 的生命周期负责人；否则它应把任务包交回主代理，不要声称已经调用。

一个父任务只能指定一个调用者负责同一目标。job_id、等待和结果读取不在不同 MCP 进程间接力。Grok 的 Agent/subagent 始终禁用，Codex subagent 也不得递归调用本插件。

## 停止条件

遇到失败、超时、取消、空回答、输出或 turn 上限、模型 fallback/switch、ACP 异常或 MCP 连接关闭时，停止使用该结果。不要自动重试、改换路径、重新委派或把部分回答当作成功。

实现任务结束后，Codex 必须检查实际 diff、运行相关测试，并由 gpt-5.6-luna 以 max reasoning 独立 review。没有这些证据时，结果保持 unverified。
