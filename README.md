# Call Grok Build

一个可分发的 Codex 插件：供 Codex 主代理或具备插件工具访问权的 subagent，把边界明确的研究、计划、代码审查和实现任务交给本机已安装的 Grok Build，再由 Codex 独立核验结果。Grok 的回答和改动始终只是候选结果，不会自动成为最终结论。

## 概览

- Skill 会把自然语言请求整理成范围、约束、验收标准和证据要求明确的任务包。
- MCP server 负责模型探测、异步任务、状态、结果、取消和收据。
- `research`、`plan`、`review` 使用只读 sandbox；`implement` 只允许写入干净的 linked Git worktree。
- 每个 job 使用独立的运行时 attest ACP 进程和全新的任务 ACP 进程；任务 ACP 只有一个新会话和单条 prompt。实现结果必须经过 Codex 独立发起的 `gpt-5.6-luna`、`max` reasoning review。
- 收据记录模型证据、目录完整性快照、限制条件和验证状态，便于复查。
- Codex subagent 可直接拥有一次完整调用生命周期；Grok 自己的 subagent/`Agent` 仍被禁用，避免递归委派。

## 要求

- 支持插件的 Codex 版本。
- Python 3.9 或更高版本；插件运行时只使用 Python 标准库。
- 能在 `PATH` 中调用的 Grok Build CLI（命令名为 `grok`）。
- 已完成 Grok CLI 登录，并允许 CLI 刷新模型目录及建立 ACP 连接。
- 只有 `implement` 需要 Git linked worktree；其他模式也可以用于非 Git 目录，但 Git 仓库能提供更完整的变更收据。

先确认 CLI 和登录状态：

```bash
grok --version
grok login
grok models
```

## 安装与更新

1. 获取本插件目录或发布压缩包。插件根目录必须包含 `.codex-plugin/plugin.json`。
2. 在 Codex 的插件管理界面选择本地目录或压缩包安装。若使用 marketplace，请按宿主的标准流程添加该 marketplace；插件源码路径应相对于 marketplace 根目录，不要把某台机器的绝对路径写进分发配置。
3. 按 Codex 提示重新加载插件。安装或更新后新建一个 Codex task，确保新 task 加载当前版本。
4. 更新时替换插件源码或安装新的发布包，不要直接编辑 Codex 生成的缓存目录。

插件本身不会替使用者创建 Git worktree、提交、推送或合并代码；这些操作保留给正常的 Git/Codex 工作流。

## 快速开始

在新建的 Codex task 中直接描述委托意图，并给出目标目录的绝对路径：

```text
使用 Grok Build 只读分析这个项目的架构、主要风险和改进方向。
项目目录：/path/to/repository
关键结论由 Codex 独立核验，并区分事实与推断。
```

Skill 会依次完成模型 setup、任务提交、状态等待和结果读取。通常不需要手动输入 MCP 工具名；需要了解工具参数或编排方式时，参阅 [使用指南](docs/USAGE.md)。

## Codex subagent 调用

当 Codex 宿主把本插件的 Skill/MCP tools 暴露给 subagent 时，主代理可以把一个边界明确的任务交给 Codex subagent，由该 subagent 直接完成 `setup`、spawn、等待、取回结果和初步核验，再把收据回传给父任务。插件本身不区分主代理和 subagent 调用者。

```text
让一个 Codex subagent 调用 Grok Build，只读 review 指定范围。
该 subagent 负责保存并轮询自己的 job_id，完成后回传结果收据和复现证据；
父任务不要为同一目标重复提交 Grok job。
项目目录：/path/to/repository
```

支持边界：宿主必须确实向该 subagent 提供本插件工具；否则 subagent 只能把任务包交回主代理。连接到同一个 MCP server 的 Codex 调用者共享两个默认异步 job 执行槽位、job 列表和同 worktree 实现锁；`setup` 不占用这些 worker 槽位。若宿主为不同 subagent 启动独立 MCP server，这些进程不会共享 job、correction 链、并发计数或 worktree 锁；subagent 必须在自己的连接中完成整个生命周期，不能把 job ID 交给另一进程继续，并发实现必须使用不同的 linked worktree。每个调用者只操作自己明确持有的 job ID。这里允许的是 Codex subagent 调用，绝不重新启用 Grok 的 `Agent`，也不允许任何一层递归调用本插件。

## 四类任务

### 研究

```text
让 Grok Build 研究“目标主题”，只使用可核验来源并返回来源 URL。
请区分已证实事实、推断和待确认事项；Codex 复核关键来源。
项目目录：/path/to/repository
```

研究默认允许 Grok 的内置 web search；如果不需要联网，可明确要求关闭 web access。

### 计划

```text
让 Grok Build 为这个仓库制定实现计划，只读，不修改文件。
计划必须包含范围、假设、步骤、测试、回滚、风险和验收标准。
项目目录：/path/to/repository
```

### 代码审查

```text
让 Grok Build 只读 review 当前代码，只报告有证据的 actionable findings。
按严重度排序，并提供 file:line、影响和修复建议；Codex 复现 high/critical 结论。
项目目录：/path/to/repository
```

### 实现

先准备一个干净的 linked worktree，再委托写入：

```bash
git -C /path/to/repository worktree add \
  /path/to/repository-grok-worktree \
  -b grok/feature-name
```

然后在 Codex 中说明：

```text
在这个干净的 linked worktree 中，让 Grok Build 实现“目标变更”。
只修改实现所需文件，不要 commit、push、merge、rebase、cherry-pick、reset，
也不要创建或删除 worktree。完成后由 gpt-5.6-luna 使用 max reasoning 独立 review 实际 diff。
工作目录：/path/to/repository-grok-worktree
```

实现模式会禁用 Grok 的 `run_terminal_cmd` 和 `Agent` 工具，因此 Grok 不能运行 shell、解释器、Git 命令或递归代理。实现完成不等于通过：Codex 必须在 Grok 返回后自行运行相关测试，再检查 Luna 的结论、实际 diff 和测试证据；在通过前，结果保持 `unverified`。

## 架构

```text
Codex task（主代理或具备工具访问权的 subagent）
   │ Skill：整理范围与验收标准
   ▼
Call Grok Build MCP server（内部配置键：grok-build）
   ├─ setup：刷新目录并进行 ACP 模型 attest
   ├─ spawn_readonly：research / plan / review
   └─ spawn_worker：linked-worktree implement
              │
              ▼
       Grok Build CLI / ACP stdio
              │
              ▼
       有界收据 + 公开回答
              │
              ▼
       Codex 独立核验（实现还需 Luna Max）
```

MCP 工具包括 `setup`、`spawn_readonly`、`spawn_worker`、`status`、`result`、`list` 和 `cancel`。任务状态只存在于当前 MCP server 进程中；进程重启后不会假称恢复进行中的任务。

## 动态模型选择

插件不会把某个 Grok 版本写死，也不会根据版本字符串猜测“最强模型”。`setup` 和每个实际任务都会在目标 `cwd` 刷新 `grok models`，再读取 ACP `initialize._meta.modelState`，使用 provider/runtime default model，并从该模型实际广告的 reasoning effort 中选择最高可排序档位：`xhigh > high > medium > low > none`。当前最高版本由实时目录和 ACP 运行时共同决定，文档和代码都不预设某个版本号。

目录默认值与 ACP runtime default 不一致、目录刷新失败或疑似使用缓存、ACP 缺少可验证的模型/effort 元数据、出现未知 effort，都会 fail closed。任务启动时会再次 attest，并核对完成元数据和 model-switch 事件；不会静默 fallback。最高 reasoning effort 优先质量，不代表最低延迟；CLI 若没有可验证的独立 speed/service-tier 控制，插件不会臆造 `fast` 参数。

## 交叉验证与可信度

- 研究：Codex 打开并核对决策关键来源；未经支持的说法标为未验证。
- 计划：Codex 检查范围、假设、迁移/回滚、测试和破坏性风险；高风险计划可再做独立 Luna Max 检查。
- 代码审查：Codex 从源码和测试复现 high/critical finding；Grok 的 finding 只有在复现后才算确认。
- 实现：Luna 以只读方式检查原始需求、验收标准、实际 worktree diff 和测试证据；不要只把 Grok 的叙述转交给 reviewer，以降低确认偏差。
- 一次实现修复流程最多安排一次针对修复结果的 Grok 回归复审，再安排一次 Luna Max 独立终审；两者都不允许递归调用本插件或再次自动委派。

`schema_valid: true` 只表示收据格式正确，不表示事实正确。桥接层返回 `verified: false`，最终判定由 Codex 完成。

## 循环与并发限制

- 一个 job 的运行时 attest 与任务执行使用独立 ACP 进程；任务 ACP 只有一个全新的会话和 prompt。
- 默认 24 turns，硬上限 48 turns；默认超时 30 分钟，硬上限 60 分钟。
- job 的执行超时从进入 `running` 开始，覆盖前置/后置快照、CLI 探测、模型 attest 和 ACP 任务；队列等待时间不计入该执行超时。`setup` 使用独立的 120 秒默认截止时间，最大 180 秒。
- 不自动重试、不自动重新委派；Codex subagent 只可拥有一次明确的调用生命周期，不允许它递归派生插件调用，也不允许 Grok 调用本插件或再创建 Grok/Codex 任务。
- 实现收到 Luna 的修改意见后，最多允许两轮有明确 parent 的 correction；这是桥接层安全上限，不代表工作流会自动循环。默认流程只安排一次修复复审和一次 Luna Max 终审。
- 默认最多两个异步 job 同时执行；`setup` 不计入该 worker 上限，同一个 worktree 同时只能有一个实现任务。

达到 turn/output 限制、超时、取消或模型切换时，任务停止并报告状态；不会以部分结果冒充成功。正常取消或 MCP 关闭会清理对应的子进程组；如果宿主遭遇 SIGKILL 或崩溃，清理代码无法运行，仍可能残留孤儿 Grok 进程，不能把本插件描述成绝对的父子进程存活保证。实现任务即使取消或超时，也应检查 worktree，因为进程终止不会自动回滚文件。

## 数据边界

Grok Build 在本机执行工具，但 prompt 和选中的代码上下文会发送到 xAI。不要委托密钥、cookie、token、`.env`、SSH 私钥、证书、生产凭据、无关个人目录或未经授权的第三方私有代码。使用窄范围的目标目录；桥接层拒绝文件系统根目录和 home 目录作为 `cwd`。

桥接层不会把目标目录的绝对路径重复写进任务 prompt，公开的 setup、status 和 result 收据也只使用 `.` 作为目标标签，并对 Git 分支和 worktree 位置保留哈希或计数。所有公开字符串和嵌套字段会在序列化前统一经过凭据形状脱敏，覆盖常见 URL query token、带日志前缀的认证头、JSON 字符串/数组/对象凭据、URL userinfo、账号路径和邮箱；这仍只是纵深防御。不过，Grok CLI 为了在目标目录执行，仍会在本机进程参数和 ACP session 中接收真实 `cwd`；被选择的文件名、代码和命令输出也可能暴露仓库身份。不要把输出脱敏误解为内容匿名化。

`cwd` 必须是具体项目或资料子目录。除文件系统根目录和当前 home 外，桥接层还拒绝账号目录父级、其他账号的 home、整个临时目录以及常见系统配置/程序目录；需要临时测试时应创建一个独立子目录，而不是把 `/tmp` 或等价目录本身作为目标。

只读任务会比较 Git 仓库的 HEAD/ref、worktree、tracked diff 以及 untracked/ignored 内容；非 Git 目录则执行不跟随符号链接的有界文件树快照，覆盖内容、元数据和目录结构。每次任务还会先对准确 `cwd` 做 20,000 条目的 scope scan：顶层 `.git` 仅允许真实目录或 regular pointer，符号链接、悬空链接和指向 `cwd` 外部的链接都会失败关闭。完整的 non-Git、untracked、ignored 与普通 Git 管理区内容扫描采用 20,000 条目/128,000,000 字节边界；tracked symlink 扫描另有 200,000 条目上限；Git object database 会完整哈希最多 200,000 条目/512,000,000 字节，并拒绝 alternates。

Git diff 明确禁用 external diff 和 textconv；仓库只要配置了 include/includeIf、外部 attributes/excludes/hooks/worktree 路径或 clean/smudge/process filter，就以 `E_GIT_CONFIG_EXTERNAL` 停止，避免快照阶段使用仓库外 helper。Git 报告的根目录也必须是 `cwd` 的真实祖先，否则返回 `E_GIT_SCOPE`。`.git` 指针及 common/admin 管理区（包括 index、lock、hooks、config、refs、logs、worktrees 和对象内容）单独纳入完整性证据；管理区变化会以 `E_GIT_ADMIN_CHANGED` 失败关闭。上述边界采用“等于上限可接受、超过即拒绝”的规则。

详细的读取、写入、凭据、prompt injection 和失败处理规则见 [安全边界](docs/SECURITY.md)。

## 故障排查与开发

- setup、登录、模型目录、ACP 或任务状态问题：参阅 [故障排查](docs/TROUBLESHOOTING.md)。
- 本地测试、真实 smoke test、MCP stdio 调试和发布检查：参阅 [开发与测试](docs/DEVELOPMENT.md)。
- 工具参数、任务包模板和收据解释：参阅 [使用指南](docs/USAGE.md)。

最小单元测试入口：

```bash
python3 -Wd -m unittest discover -s tests -v
```
