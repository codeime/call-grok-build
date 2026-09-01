# 开发与测试

本文面向维护插件、更新 Skill/MCP 或发布新版本的场景。命令示例假定当前目录是插件根目录；示例路径使用可移植占位符。

## 目录结构

| 路径 | 责任 |
| --- | --- |
| AGENTS.md | 仓库级 Agent 协作、授权、运行时不变量和验证入口 |
| .agents/skills/ | 面向仓库维护者的 bridge review 与分发隐私审计 Skill |
| .codex-plugin/plugin.json | 插件元数据、Skill/MCP 入口和图标资源声明 |
| .mcp.json | MCP server 启动命令及最小环境变量清单 |
| skills/delegate-to-grok-build/SKILL.md | Codex 侧委托、验证、写入和循环规则 |
| skills/delegate-to-grok-build/references/ | 工具契约和安全边界的详细说明 |
| mcp/grok_build_server.py | JSON-RPC stdio server、工具 schema 和调用分发 |
| scripts/grok_build_bridge.py | CLI 探测、动态模型选择、ACP client 和 job manager |
| tests/test_bridge.py | 单元测试、协议测试和安全/循环回归覆盖 |
| tests/fake_grok.py | 不联网的确定性 ACP fake |
| tests/real_smoke.py | 可选的真实 Grok ACP 端到端 smoke test |
| docs/ | 面向分发的使用、安全、排障和开发文档 |

## v2 运行时约束

- Codex 传入当前 workspace 的绝对 cwd，Grok 在同一个 cwd 原生启动；所有任务都走 direct。
- bridge 不复制项目、不创建临时副本、不切换辅助目录，不做 Git 或全目录内容扫描。
- paths 仅作为 prompt 关注范围提示，不是访问控制；不会改变 cwd。
- research、plan、review 使用 read-only sandbox；implement 使用 workspace sandbox，允许主工作目录、已有修改和非 Git 目录。
- 任务前后的工作区状态由 Codex 记录和检查；bridge 不声称能区分任务前已有修改与 Grok 新增修改。
- 结果必须是 grok.codex.result.v2，workspace.integrity_snapshot 为 not_collected。
- 模型每次从实时 catalog 和 ACP runtime default 证明，使用当前模型实际广告的最高 reasoning effort；禁止写死版本、静默 fallback 或接受 model switch。
- 真实工具 ID run_terminal_cmd 和 Agent 必须禁用；不得递归调用插件。
- 单 prompt、turn/time/output ceiling、准确 cancel、同 cwd implement 互斥和 correction 上限不可移除。

## Agent 协作资产

维护任务先读取根 AGENTS.md，再按责任面使用 .agents/skills/。插件调用方读取 skills/delegate-to-grok-build/。修改其中一侧后检查 README、docs、Skill 和 manifest 的 v2 契约是否一致。

## 本地检查

### 语法检查

~~~text
python3 -m py_compile mcp/grok_build_server.py scripts/grok_build_bridge.py tests/*.py
~~~

### 单元与协议测试

~~~text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
~~~

测试使用 fake Grok，不消耗真实请求。应覆盖：

- research、plan、review 保持 read-only，implement 使用 workspace sandbox；
- primary checkout、已有修改和非 Git 目录都能按契约处理；
- paths 只影响 prompt 关注范围，不改变 cwd 或访问方式；
- Grok 不能使用 terminal、Agent、外部 MCP 或递归调用插件；
- 任务前后由 Codex 记录的状态、实际 diff、测试和独立 review 能正确决定验证状态；
- 当前 cwd 的稳定目录句柄、权限和路径替换检测失败关闭；
- 动态 catalog、ACP runtime default、最高 effort、fallback 和 model switch 证明一致；
- await_result 的有界等待、终态幂等、准确 cancel、输出/turn/时间限制和进程组清理；
- 同 cwd implement 互斥、不同 cwd 并行，以及 correction 仅允许一次且无自动重试；
- 最小环境、敏感文件拒绝和公开收据的账号路径/邮箱/凭据脱敏；
- grok.codex.result.v2 envelope、workspace.integrity_snapshot=not_collected 和 verification 状态。

fake Grok 只能证明参数、协议和收据行为，不能证明真实 Grok CLI 完全遵守 OS sandbox。测试通过也不代表 Grok 的代码或结论正确。

### 真实 smoke test

真实 smoke test 需要已登录的 Grok CLI、实时模型目录和网络连接，会消耗一次 Grok 请求：

~~~text
python3 tests/real_smoke.py --cwd /path/to/project --timeout 300
~~~

只有用户明确授权联网和消耗请求时才运行。它只提供当前安装版本的运行证据，不能替代 Codex 的测试、diff 检查或 Luna Max review。

## MCP stdio 调试

MCP server 通过 stdin/stdout 处理一行一个 JSON-RPC 消息。通常由 Codex 通过 .mcp.json 启动；手动启动只用于协议调试：

~~~text
python3 mcp/grok_build_server.py
~~~

stdout 只能输出 JSON-RPC，诊断写 stderr，且不得输出凭据、完整 prompt 或私有代码。推荐顺序是 initialize、tools/list、delegate_readonly 或 spawn_worker、await_result，必要时 result。不要因 revision 变化重新发起 job。

## 插件校验

发布前使用宿主提供的官方插件校验脚本检查插件根目录：

~~~text
python3 /path/to/plugin-validator/validate_plugin.py /path/to/call-grok-build
~~~

至少检查 manifest JSON、相对资源路径、Skill front matter、MCP 配置、图标文件和必需目录。若宿主提供独立 Skill validator，也应运行。

## 分发检查

发布包应满足：

- 顶层目录能直接发现 .codex-plugin/plugin.json；
- manifest、MCP 配置、文档和 Skill 不含本机绝对路径、真实用户名、邮箱、机器名、会话信息或凭据；
- .mcp.json 只声明运行所需的最小环境变量；
- README 链接的文档和资源均存在；
- 版本号按发布策略递增，安装后在新 Codex task 中验证 Skill/MCP 加载。

不要为了让发布包看起来干净而修改生成 bundle、source map 或图标二进制内容；只核验它们是否应被包含。

## 修改后的验证顺序

1. 运行语法检查和相关 fake 测试。
2. 检查 direct cwd、sandbox、模型证明、敏感文件边界、任务前后状态和收据。
3. 只有明确授权联网时才运行真实 smoke test。
4. 运行官方插件/Skill validator。
5. implement 完成后由 Codex 运行测试并检查实际 diff，再安排一次 Luna Max 独立终审；如发现问题，最多做一次 correction 和一次回归复审。
6. 在干净安装中创建新 Codex task，确认工具列表和 v2 收据。
7. 只有明确授权后执行提交、推送或发布。

## 进程生命周期

准确 cancel 或 MCP server 正常关闭时，bridge 会尝试清理该 job 的进程组。宿主遭遇强制终止、崩溃或断电时可能留下孤儿 Grok 进程；恢复时只依据准确 job_id 和进程证据处理，不使用 pkill、killall 或模糊匹配，也不自动重试。
