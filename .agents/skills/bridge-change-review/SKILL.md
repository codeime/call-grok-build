---
name: bridge-change-review
description: 修改或审查 Call Grok Build 的 MCP server、bridge、任务生命周期、模型证明、当前 cwd 运行边界或相关测试。用于本仓库的实现、修复和安全 review；不用于纯文案翻译、图标调整或仅安装分发。
---

# Bridge 变更与审查

先读取仓库根 AGENTS.md、docs/DEVELOPMENT.md 和 docs/SECURITY.md，再按变更范围读取 docs/USAGE.md、docs/TROUBLESHOOTING.md 和 skills/delegate-to-grok-build/。以实际代码、测试和 diff 为证据，不用旧文档主张代替实现检查。

## 判定任务模式

- 用户只要求 review、审查或检查时保持只读，按严重度报告 file:line、复现证据和最小修复建议。
- 用户明确要求实现或修复时才编辑；保留无关变更，并为实际失败模式增加回归测试。
- 安装、提交、推送、发布和真实 Grok smoke test 不属于普通代码修改授权。

## 当前 v2 不变量

- Codex 把当前 workspace 的绝对 cwd 传入 bridge；Grok 在同一个 cwd 原生启动，所有路由均为 direct。
- bridge 不复制项目、不创建辅助目录、不扫描 Git 或整个目录内容；paths 只能作为 prompt focus。
- implement 允许 primary checkout、已有 dirty 状态和非 Git 目录。bridge 不声称能区分任务前已有修改与 Grok 新增修改，Codex 负责任务前记录、任务后 diff/测试和独立 review。
- 结果 envelope 必须是 grok.codex.result.v2，workspace.integrity_snapshot 必须为 not_collected。
- 动态模型来自实时 catalog 与 ACP runtime default 的共同证明，并选择当前模型实际广告的最高 effort；禁止写死版本、fallback 或静默 model switch。
- research、plan、review 必须使用 read-only sandbox；implement 必须使用 workspace sandbox。真实工具 ID run_terminal_cmd 和 Agent 始终禁用。
- 一个 job 只有一个无 session/prompt 的 discovery ACP 进程，以及一个单 session/单 prompt 的 task ACP 进程；不得增加自动重试、自动重新委派或递归代理。
- 保留 turn/time/output ceiling、准确 cancel、同 cwd implement 互斥和最多一次 correction/回归复审。

## MCP server

修改 mcp/grok_build_server.py 时确认：

- stdout 只承载 JSON-RPC；日志和诊断只走有界 stderr；
- setup/await_result 等待期间，stdin reader 仍能响应 cancel、status 和 ping；
- stdout writer 在真实 pipe 背压时有硬退出边界；
- 工具 schema 与运行时校验一致，尤其是 cwd、paths、mode、job_id 和 await 参数；
- subagent 调用与主代理使用同一工具契约，不依赖调用者身份；
- result、status、错误和诊断都经过递归凭据/账号路径脱敏。

## Bridge 与生命周期

修改 scripts/grok_build_bridge.py 时确认：

- job 使用稳定 cwd 句柄，排队后路径替换不会把 Grok 导向另一个目录；
- 每个 job 都实时证明 provider/runtime default model 和最高 advertised effort；
- 单 job 只有一次 live catalog preflight、一个无 session/prompt 的 discovery ACP 进程，以及一个单 session/单 prompt 的 task ACP 进程；
- read-only/workspace sandbox、run_terminal_cmd/Agent 禁用、最小环境和敏感文件拒绝持续生效；
- 内容、答案和 stderr 有界，deadline、turn limit、cancel 和进程组清理不会被标记为成功；
- 同一 cwd 的 implement 互斥只按稳定 filesystem identity 判断；
- correction 只能引用紧邻成功 parent，最多一次，不得分支、递归或自动重试；
- MCP server 重启后不假称能恢复旧 job。

## 调用方 Skill 与文档

修改 skills/delegate-to-grok-build/ 或工具契约时，同步检查：

- README.md
- docs/USAGE.md
- docs/SECURITY.md
- docs/TROUBLESHOOTING.md
- docs/DEVELOPMENT.md

文档必须说明：当前 workspace 原目录直连；paths 只是 prompt focus；明确点名 Grok Build 的用户请求不需要重复授权询问；Codex subagent 可以作为调用者；Grok Agent/subagent 永远禁用；implement 允许 primary、dirty、非 Git；Codex 负责任务前后状态、测试和 Luna Max review。

## 验证

先跑与改动直接相关的检查，再运行：

~~~text
python3 -m py_compile mcp/grok_build_server.py scripts/grok_build_bridge.py tests/*.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
~~~

测试使用 tests/fake_grok.py，不能证明真实 Grok sandbox。只有用户明确授权联网和消耗请求时才运行 tests/real_smoke.py。

涉及 Grok 生成代码或安全关键实现时，Codex 必须独立复现关键结论；实现还需只读 gpt-5.6-luna、max reasoning reviewer 检查原始需求、实际 diff 和测试证据。

## 交付

说明改了什么、验证命令及结果、独立 review 结论和残余边界。未执行的真实 smoke、安装或发布必须明确标注，不能用 fake 测试推断为已通过。
