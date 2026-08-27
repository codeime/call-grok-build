# 开发与测试

本文面向维护插件、更新 Skill/MCP 或发布新版本的场景。所有命令都假定当前目录是插件根目录；将示例中的 `/path/to/call-grok-build` 和 `/path/to/repository` 替换为实际路径。

## 目录结构

| 路径 | 责任 |
| --- | --- |
| `.codex-plugin/plugin.json` | 插件元数据、Skill/MCP 入口和图标资源声明 |
| `.mcp.json` | MCP server 启动命令及最小环境变量清单 |
| `skills/delegate-to-grok-build/SKILL.md` | Codex 侧的委托、验证、写入和循环规则 |
| `skills/delegate-to-grok-build/references/` | 工具契约和安全边界的详细说明 |
| `mcp/grok_build_server.py` | JSON-RPC stdio server、工具 schema 和调用分发 |
| `scripts/grok_build_bridge.py` | CLI 探测、动态模型/effort 选择、ACP client、目录完整性快照和 job manager |
| `tests/test_bridge.py` | 单元测试、协议测试和安全/循环回归覆盖 |
| `tests/fake_grok.py` | 不联网的确定性 ACP fake，用于测试错误路径和边界条件 |
| `tests/real_smoke.py` | 可选的真实 Grok ACP 端到端 smoke test |
| `docs/` | 面向分发的使用、安全、排障和开发文档 |

## 运行时约束

- 运行时只依赖 Python 标准库；Python 3.9 或更高版本是最低要求。
- Grok Build CLI 必须可从启动 MCP server 的 `PATH` 找到；测试可以通过 `GROK_BUILD_BIN` 指向 fake CLI。
- 模型选择必须继续来自目标 `cwd` 的实时 `grok models` 和 ACP `initialize._meta.modelState`，不要按版本字符串写死模型；始终使用当前默认模型实际广告的最高 reasoning effort。
- 读模式保持 `read-only`，实现模式保持 linked worktree、快照和独立 Luna review 的边界。
- 所有模式必须继续用真实工具 ID 禁用 `run_terminal_cmd,Agent`。implement 只使用文件工具；测试由 Grok 返回后的 Codex 运行，不能在 prompt 或文档中假称 Grok 已执行测试。
- 只读快照必须覆盖准确 `cwd` 的 symlink scope、Git tracked/untracked/ignored 内容、Git `.git` pointer、common/worktree admin 和 object database，并覆盖非 Git 目录的有界文件树；完整性无法证明时要失败关闭。
- Git diff 必须保持 `--no-ext-diff --no-textconv`；Git root 必须是 `cwd` 的真实祖先；config include、外部 path、filter 和 alternate object database 必须在执行 helper 或工作区快照前失败关闭。
- 不要移除单 prompt、turn/time ceiling、output 上限、correction 上限、并发锁或 fail-closed 检查来追求表面吞吐。
- 主代理和具备插件工具访问权的 Codex subagent 使用同一 MCP 契约；不要引入依赖主代理身份的隐藏检查。调用者必须在同一 MCP 连接内按准确 job ID 管理完整生命周期。单个 server 共享 job manager、两个异步 job worker 槽位和同 worktree 实现锁；`setup` 不走该 executor。独立 server 进程之间不共享这些状态，因此并发 implement 调用者必须使用不同的 linked worktree。
- 不要让 Grok worker 调用本插件、其他 Grok worker 或 Codex 委托；不要添加自动重试、自动重新委派或递归修复。
- 一次实现修复流程最多做一次 Grok 修复回归复审，再做一次 Luna Max 独立终审；bridge 的 correction 上限是安全护栏，不是循环许可。

## 本地检查

### 语法检查

```bash
python3 -m py_compile mcp/grok_build_server.py scripts/grok_build_bridge.py tests/*.py
```

### 单元与协议测试

```bash
python3 -Wd -m unittest discover -s tests -v
```

测试使用 `tests/fake_grok.py`，不会消耗真实 Grok 请求。修改模型解析、ACP 事件、目录快照、Git 管理区、非 Git 目录、cwd 稳定性、错误码、worktree 或循环策略后，应先运行完整测试集，再补充覆盖新边界的回归测试。至少应覆盖：

- `research`、`plan`、`review` 修改 ignored 文件时失败关闭；
- 非 Git 目录发生创建、删除、重命名或内容/元数据变化时失败关闭；
- `.git` 指针、common/admin 管理区、hooks、refs、logs、worktrees 或锁文件变化时返回 `E_GIT_ADMIN_CHANGED`；
- exact cwd 的外部、悬空、循环或不可读 symlink 被拒绝；顶层 `.git` symlink 被拒绝，而嵌套 `.git` 仍参与 scope scan；
- macOS case-folding 卷上的 `/USERS`、`/LIBRARY`、`/SYSTEM` 及 home alias 仍被 filesystem identity 检查拒绝；PermissionError/ELOOP 等无法证明身份的情况失败关闭；
- `--no-textconv` 不执行 repository textconv；include/includeIf、外部 path 或有效的 clean/smudge/process filter 返回 `E_GIT_CONFIG_EXTERNAL` 且不执行 helper；外部 `core.worktree` 返回 `E_GIT_SCOPE`，object alternates 被拒绝；
- 参数化边界测试证明等于限制可接受、`+1` 被拒绝，并断言实际常量：普通 scan 为 20,000 条目/128,000,000 字节、tracked scan 为 200,000 条目、object scan 为 200,000 条目/512,000,000 字节；
- `spawn_readonly` 在运行时拒绝 `implement`，不能只依赖 MCP schema enum；
- 两个独立 Codex 调用者风格的 job 获得不同 ID；取消其中一个不会终止另一个，证明共享 manager 的准确 job-ID 隔离；
- 独立 manager/MCP 进程不能读取彼此 job ID，避免文档误导调用者做跨进程生命周期接力；
- 带日志前缀的 Authorization、JSON 字符串/数组/对象凭据、无密码/FTP userinfo 会在序列化前的完整收据边界递归脱敏，输出仍是合法 JSON；
- 任务排队后替换路径时，稳定目录句柄不会把 Grok 导向另一个目录；
- stderr/answer output 超限、turn limit、超时、取消、模型 fallback/switch 和 correction 上限不会被标记为成功；fallback 即使出现在 stderr 保留上限之后也必须被识别；
- job deadline/cancel 覆盖 pre/post snapshot、Git、probe、ACP attest 与任务进程；setup 的单一 deadline 覆盖 catalog probe 与 ACP initialize；
- ACP stdin 在管道背压时仍响应绝对 deadline/cancel，异常写入会清理 pending waiter；进程组 leader 先退出时仍会清理同一 bridge-owned group；
- 公开 setup/status/result/error 的递归脱敏不会泄露凭据、账号路径、邮箱或认证头；文档明确区分“Codex subagent 可作为调用者”和“Grok subagents 始终禁用”。

fake Grok 用于验证参数、协议、收据和前后快照，不证明真实 Grok CLI 一定遵守 OS sandbox 或 deny 规则。真实 smoke test 只能提供当前安装版本的运行证据，不能替代快照和失败关闭。

### 真实 smoke test

真实 smoke test 需要已登录的 Grok CLI、实时模型目录和网络连接，会消耗一次 Grok Build 请求：

```bash
python3 tests/real_smoke.py \
  --cwd /path/to/repository \
  --timeout 300
```

该脚本只提交一个短的 `plan` 任务，关闭 web access，不应修改文件。它会输出动态选择的模型、reasoning effort、sandbox、session 和答案是否存在；退出码非零时不要把结果当作通过。测试结果中的模型版本只能作为当次运行证据，不能据此把版本号写死进代码或文档。

## MCP stdio 调试

MCP server 通过 stdin/stdout 处理一行一个 JSON-RPC 消息。通常应让 Codex 通过 `.mcp.json` 启动；手动启动只用于协议调试：

```bash
python3 mcp/grok_build_server.py
```

不要向 stdout 写入调试日志，否则会破坏 JSON-RPC 协议；调试信息应走 stderr，并避免输出凭据、完整 prompt 或私有代码。优先验证以下顺序：`initialize` → `tools/list` → `tools/call`（先 `setup`，再只读 spawn）→ `status` → `result`。

## 插件校验

发布前使用宿主提供的官方插件校验脚本检查插件根目录，例如：

```bash
python3 /path/to/plugin-validator/validate_plugin.py /path/to/call-grok-build
```

校验内容至少应覆盖：manifest JSON、相对资源路径、Skill front matter、MCP 配置、图标文件和必需目录。若宿主提供单独的 Skill validator，也应运行它；不要用手工猜测替代校验脚本。

## 文档与分发检查

发布包应满足：

- 压缩包只包含一个以插件 ID 命名的顶层目录；进入该目录即可直接发现 `.codex-plugin/plugin.json`，不要再嵌套额外目录；
- manifest、MCP 配置和 Skill 中不包含本机绝对路径、个人姓名、账号、公司/项目专名、凭据或测试机信息；
- 分发文档只使用 `/path/to/...` 之类占位符，并说明需要按宿主标准流程安装；
- `.mcp.json` 只声明运行所需的最小环境变量；
- README 链接的文档和路径在压缩包内均存在；
- 新增或修改的行为都有对应测试，尤其是模型 fallback、模型切换、tracked/untracked/ignored 快照、Git 管理区/对象库、非 Git 文件树、cwd/symlink 稳定性、外部 Git helper、取消、超时、output limit 和 correction 上限；
- 版本号按发布策略递增，更新后的插件通过重新安装和新 Codex task 验证加载。

不要为了让发布包“看起来干净”而 diff 或格式化生成的 bundle、source map 或其他构建产物；除非发布规则明确要求，否则只核验其是否被正确包含或排除。

## 修改实现时的验证顺序

1. 先运行语法检查和完整 fake 测试。
2. 对只读任务确认 exact-cwd scope、Git tracked/untracked/ignored 内容、Git 管理区/对象库和非 Git 文件树前后 snapshot 不变；对实现任务确认 linked worktree、管理区、primary checkout 和 HEAD/ref 不变。
3. 若有可用的已授权登录环境，运行真实 smoke test；不要把失败或 output 截断的结果当作成功。
4. 使用宿主官方 validator 校验插件目录和 Skill。
5. 对一次实现变更，先按范围做一次 Grok 修复回归复审（如确有修复），再让 `gpt-5.6-luna` 以 `max` reasoning 做一次独立终审。reviewer 必须读取原始需求、实际 diff 和测试证据，不得只接收 Grok 的结论。
6. 在干净安装中创建新 Codex task，确认 MCP 工具列表、setup 动态模型证据、结果收据和验证门槛均正常。
7. 只在明确授权后执行提交、推送或其他发布动作；插件本身不自动执行这些动作。

## 进程生命周期与异常恢复

- 正常 `cancel` 或 MCP server 关闭时，只清理准确 job 对应的进程组；不得使用 `pkill`、`killall` 或模糊匹配。
- 宿主收到 SIGKILL、崩溃或断电时，清理代码无法运行，仍可能残留孤儿 Grok 进程。这是已知残余风险，不能宣称绝对的父子进程存活保证。
- 异常退出后先依据准确 job ID 和收据检查进程、worktree、Git 管理区及 primary checkout，再决定人工处理；不要自动重试或重新委派。

## 变更设计提示

- 模型目录和 ACP runtime default 是两份必须一致的证据；遇到歧义应失败关闭，不能偷偷选一个旧模型。
- Grok 的答案和 finding 都是不可信候选；实现结果必须保留 `review_required`，并交由只读 Luna Max 检查实际 diff。
- correction 是有界的新 job，不是递归重试；必须携带紧邻 parent，最多两轮安全上限，但正常流程只执行一次修复回归复审和一次 Luna Max 终审。
- 任务状态只在 MCP server 进程内存中保存；不要引入没有持久化格式和恢复语义的“自动续跑”。
- 对日志、stderr、路径和收据字段做边界审查，避免为了调试将秘密或完整上下文写入输出。

更多使用和故障处理见 [使用指南](USAGE.md)、[安全边界](SECURITY.md) 和 [故障排查](TROUBLESHOOTING.md)。
