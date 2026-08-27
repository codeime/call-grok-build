# 故障排查

先保留失败 job 的 `job_id`、状态和错误码。不要自动重试；先确认失败发生在模型探测、ACP 启动、任务执行、完整性快照还是结果核验阶段。

## 快速检查

```bash
python3 --version
grok --version
grok login
grok models
git -C /path/to/repository worktree list --porcelain
```

在 Codex 中：

1. 对实际目标目录重新调用 `setup`。
2. 确认返回 `ready: true`、`runtime_attested: true`、`selected_model` 和 `selected_reasoning_effort`。
3. 调用 `setup` 和任务工具时传入完全相同的绝对路径；公开响应中的 `cwd: "."` 是有意的脱敏标签，不是路径丢失。
4. 任务运行中每 10–30 秒查询一次 `status`；进入终态后再调用 `result`。
5. 写入任务失败、取消或超时后，检查 worktree、Git 管理区和 primary checkout，再决定下一步。

如果目标不是 Git 仓库，`research`、`plan` 和 `review` 仍然可用；它们会对非 Git 目录做有界文件树完整性检查。`implement` 仍必须使用干净的 linked Git worktree。

## 插件不可见或仍在使用旧版本

- 确认安装对象的根目录包含 `.codex-plugin/plugin.json`、`skills/` 和 `.mcp.json`。
- 在 Codex 插件管理界面重新加载或重新安装插件包；不要直接修改生成的 cache 目录。
- 更新后新建一个 Codex task。已经运行的 task 不一定会重新发现新增的 Skill/MCP server。
- 如果使用 marketplace，检查 source path 是相对于 marketplace 根目录的有效路径，而不是某台机器上的绝对路径。
- 可在新 task 中请求列出 `grok-build` MCP 工具，确认存在 `setup`、`spawn_readonly`、`spawn_worker`、`status`、`result`、`list` 和 `cancel`。

## `setup` 返回 `ready: false`

### CLI 找不到或能力不足

检查 `grok --version` 是否成功，并确认 `grok` 所在目录在启动 Codex 的 `PATH` 中。插件会探测 ACP 所需能力，包括 sandbox、cwd、禁止 subagents、deny、disallowed tools、model、reasoning effort、always approve 和 no leader 等参数；缺少必需能力会拒绝启动任务。

### 未登录

运行 `grok login`，完成登录后再运行 `grok models`。桥接层只使用 CLI 的缓存认证；没有 `cached_token` 时会返回 `E_AUTH`。

### 模型目录刷新失败或疑似缓存

`setup` 要求 `grok models` 的实时刷新成功。网络失败、DNS 错误、刷新超时、缓存来源不明或 stale catalog 都会 fail closed，即使命令偶尔返回了可解析的旧模型列表也不会继续。检查网络、CLI 登录状态和目标目录下的 Grok 配置，然后重新执行 `setup`。

### 模型或 effort 无法 attest

以下情况都应停止，不要自行指定一个看似合理的低档或旧模型：

- CLI catalog 没有唯一 provider default；
- catalog default 与 ACP `modelState.currentModelId` 不一致；
- ACP 没有 `modelState` 或没有当前模型的可用元数据；
- 当前模型没有可排序的 reasoning effort，或广告了未知档位。

插件只按 `xhigh > high > medium > low > none` 选择当前模型实际广告的最高档，并拒绝无法判断的情况。最高模型版本由实时 catalog 和 ACP runtime 共同决定，不能把某个版本号写死。检查 CLI 版本、项目级配置和模型服务状态，而不是修改文档或代码偷偷指定版本。

`setup` 的可选 `timeout_seconds` 范围为 `10..180`、默认 `120`；同一截止时间覆盖 catalog probes 与 ACP runtime attestation。`E_SETUP_TIMEOUT_VALUE` 表示参数越界；`E_PROBE_START`、`E_PROBE_TIMEOUT`、`E_PROBE_OUTPUT_LIMIT` 分别表示 probe 无法启动、超过截止时间或输出越界。不要用旧 catalog 绕过这些错误。

## 任务启动后失败

| 错误码/状态 | 常见原因 | 处理 |
| --- | --- | --- |
| `E_GROK_NOT_READY` | 任务执行前的实时 catalog/能力探测不通过 | 重新检查 `grok models`、登录和 `setup`；不要重试同一个 job |
| `E_CWD_SCOPE` | 目标是 home、账号父级、其他账号目录、整个临时目录或系统目录 | 改用明确的项目/资料子目录；临时任务先创建独立子目录 |
| `E_CWD_CHANGED`、`E_CWD_FD`、`E_CWD_FD_UNSUPPORTED`、`E_SCOPE_IDENTITY` | 稳定目录句柄或 filesystem identity 无法证明，可能发生路径替换、权限错误或 symlink loop | 停止任务，核对目标真实路径、权限和挂载；不要改成字符串比较或继续运行 |
| `E_CWD_SCOPE_READ`、`E_CWD_SCOPE_GIT_MARKER`、`E_CWD_SCOPE_SNAPSHOT_RACE`、`E_CWD_SCOPE_SYMLINK_SCOPE` | exact-cwd scope preflight 无法读取、`.git` 类型不合法、扫描中发生变化，或出现悬空/循环/越界 symlink | 只使用稳定的具体项目目录；`.git` 必须是真实目录或 regular pointer，不能是 symlink；移除越界链接或缩小范围 |
| `E_CWD_SCOPE_SNAPSHOT_LIMIT` | exact-cwd scope 超过 20,000 个条目 | 缩小目标目录；scope preflight 是 metadata-only，但条目上限仍失败关闭 |
| `E_TRACKED_PATH`、`E_TRACKED_SYMLINK_READ`、`E_TRACKED_SYMLINK_SCOPE`、`E_TRACKED_SYMLINK_SCAN_LIMIT` | tracked path 不安全、链接不可稳定读取/越界，或 tracked scan 超过 200,000 条目 | 修复链接或缩小仓库；不要绕过 tracked scope 检查 |
| `E_FILESYSTEM_READ`、`E_FILESYSTEM_SNAPSHOT_LIMIT`、`E_FILESYSTEM_SNAPSHOT_RACE`、`E_FILESYSTEM_SYMLINK_SCOPE` | non-Git 内容扫描读取失败、发生 race、链接越界或超过 20,000 条目/128,000,000 字节 | 停止并检查并发写入、权限、链接和范围；等于上限可接受，超过即拒绝 |
| `E_UNTRACKED_PATH`、`E_UNTRACKED_READ`、`E_UNTRACKED_SNAPSHOT_LIMIT`、`E_UNTRACKED_SNAPSHOT_RACE`、`E_UNTRACKED_SYMLINK_SCOPE` | untracked path/内容不安全、变化、越界或超过普通内容边界 | 检查路径、并发进程、权限和范围；不要跳过 untracked 证据 |
| `E_IGNORED_PATH`、`E_IGNORED_READ`、`E_IGNORED_SNAPSHOT_LIMIT`、`E_IGNORED_SNAPSHOT_RACE`、`E_IGNORED_SYMLINK_SCOPE` | ignored path/内容不安全、变化、越界或超过普通内容边界 | 检查 ignored 文件、链接、并发进程和范围；ignored 不是快照豁免区 |
| `E_GIT_SCOPE` | Git 报告的 root 不是 delegated `cwd` 的真实祖先，常见于外部 `core.worktree` | 修复仓库配置并使用真实仓库/子目录；不要允许 Git 把快照根导向无关目录 |
| `E_GIT_CONFIG`、`E_GIT_CONFIG_EXTERNAL` | 无法证明 Git config 安全，或仓库配置了 include/includeIf、外部 attributes/excludes/hooks/worktree path、clean/smudge/process/required filter | 移除/隔离该 local config 后再使用；bridge 不会继续执行仓库 helper |
| `E_GIT_OBJECT_ALTERNATES` | object database 声明了 alternates/http-alternates，或无法证明该边界 | 使用无 alternates 的独立仓库/worktree；bridge 不读取范围外对象库 |
| `E_GIT_OBJECTS_READ`、`E_GIT_OBJECTS_SNAPSHOT_LIMIT`、`E_GIT_OBJECTS_SNAPSHOT_RACE`、`E_GIT_OBJECTS_SYMLINK_SCOPE` | object database 不可安全读取、变化、链接越界，或超过 200,000 条目/512,000,000 字节 | 停止并检查对象库、并发 Git 操作和仓库规模；不能跳过对象内容证明 |
| `E_GIT_ADMIN_READ`、`E_GIT_ADMIN_RACE`、`E_GIT_ADMIN_SNAPSHOT_LIMIT`、`E_GIT_ADMIN_SNAPSHOT_RACE`、`E_GIT_ADMIN_SYMLINK_SCOPE` | `.git` pointer/common/worktree admin 无法稳定做内容快照、越界或超过普通 20,000/128,000,000 边界 | 检查 index/lock/hooks/config/refs/logs/worktrees、权限和并发 Git 进程 |
| `E_GROK_START`、`E_ACP_EXIT` | ACP 进程无法启动或提前退出 | 检查 CLI、`PATH`、权限和 stderr；单独运行 `grok --version`，再发起范围明确的新尝试 |
| `E_AUTH` | 没有 cached token | 重新 `grok login`，然后重新 setup |
| `E_ACP_PIPE`、`E_ACP_PROTOCOL`、`E_ACP_REMOTE` | stdin/stdout 管道或 ACP 响应异常 | 保留错误和 stderr，确认 CLI 与插件支持的 ACP 能力一致 |
| `E_ACP_SESSION` | ACP 没有返回 session ID | 检查 CLI 版本、登录状态和目标目录配置 |
| `E_EMPTY_RESULT` | ACP 完成但没有公开答案 | 任务包要求明确的输出格式；不要把空结果当作通过 |
| `E_OUTPUT_LIMIT` | 公开答案超过 `max_output_chars`，只得到截断内容 | 缩小任务范围或在明确的新任务中合理提高限制；不要使用部分答案 |
| `E_STDERR_LIMIT` | Grok stderr 超过收据边界，完整诊断无法证明 | 保持结果未验证，缩小任务并检查 CLI；fallback 警告即使出现在保留上限之后仍优先返回 `E_MODEL_FALLBACK` |
| `E_READONLY_CHANGED` | 只读任务前后 Git 工作区、ignored 内容或非 Git 文件树发生变化 | 检查并发进程、编辑器和构建工具；确认来源前不要使用结果 |
| `E_GIT_ADMIN_CHANGED` | `.git` 指针或 Git common/admin 管理区发生变化 | 检查 hooks、config、refs、logs、worktrees、锁文件和相关进程；不要把结果当作只读成功 |
| `E_TIMEOUT` / `timed_out` | 达到 `timeout_seconds` | 检查任务范围；写任务先检查残留变更，不要自动重试 |
| `E_JOB_DEADLINE` | 内部 snapshot/Git 边界观察到 job deadline；正常 job 会统一映射为 `E_TIMEOUT` | 按超时处理；执行时限从 `running` 开始，覆盖前后快照、probe、attest 与 ACP，不含排队时间 |
| `E_CANCELLED` / `cancelled` | 对准确 job ID 的取消已生效 | 写任务检查实际 diff；终止不会回滚文件，也不要自动重试 |
| `E_TURN_LIMIT` | 达到 `max_turns` | 缩小任务或明确验收条件；该 job 不会自动继续 |
| `E_MODEL_MISMATCH`、`E_EFFORT_MISMATCH` | 运行中实际模型/effort 与 preflight 不一致 | 结果保持未验证；检查模型服务与项目配置，不要接受 fallback |
| `E_MODEL_FALLBACK`、`E_MODEL_SWITCHED` | CLI 警告 fallback 或 ACP 报告切换模型 | 停止使用该结果，确认模型服务稳定后再做有明确范围的新尝试 |

## 只读任务改变了文件

`E_READONLY_CHANGED` 表示任务前后完整性快照不一致，可能来自 Grok，也可能来自并发进程。Git 目标先检查：

```bash
git -C /path/to/repository status --short --ignored
git -C /path/to/repository diff --stat
git -C /path/to/repository diff --cached --stat
git -C /path/to/repository worktree list --porcelain
```

非 Git 目标则检查任务前后的文件树、目录和符号链接变化。不要只看 tracked diff：ignored 文件、非 Git 文件和目录元数据也在保护范围内。Git `.git` 指针或 common/admin 管理区变化通常会单独返回 `E_GIT_ADMIN_CHANGED`；先确认 hooks、config、refs、logs、worktrees 和锁文件是否被其他程序修改。

不要假定变更一定来自 Grok；也可能是编辑器、构建工具或其他自动化造成的。确认变更来源前不要继续使用结果，也不要把该 job 当作只读成功。

## 实现任务被拒绝

| 错误码 | 原因与修复 |
| --- | --- |
| `E_WORKTREE`、`E_WORKTREE_ROOT` | 目标不是 Git linked worktree 的根目录；用 `git worktree list --porcelain` 核实并传入正确根目录 |
| `E_PRIMARY_CHECKOUT` | 目标是 primary checkout，或 Git 根目录不是 linked worktree；创建/选择 linked worktree，不能绕过检查。普通非 Git 目录对应 `E_WORKTREE` |
| `E_DIRTY_WORKTREE` | 初次实现前已有 staged、unstaged 或 untracked 变更；清理或另建干净 worktree |
| `E_PRIMARY_SNAPSHOT` | 无法读取 primary checkout 快照；检查 Git 状态和权限 |
| `E_WORKTREE_BUSY` | 同一 worktree 已有活动实现 job；等待其终态或对准确 job ID 调用 `cancel` |
| `E_PRIMARY_CHANGED` | 实现期间 primary checkout 发生变化；停止并检查两个目录，避免继续混用结果 |
| `E_GIT_ADMIN_CHANGED` | 实现期间 Git 管理区发生变化；检查 `.git` 指针、common/admin 目录和锁文件，结果保持未验证 |
| `E_COMMIT_DETECTED`、`E_HEAD_CHANGED` | Grok 改变了 HEAD 或分支；结果未验证，检查是否有提交或切换分支 |
| `E_WORKTREE_SNAPSHOT`、`E_GIT`、`E_GIT_OUTPUT_LIMIT` | Git 快照命令失败或输出超过独立边界；检查仓库可读性、Git 输出和并发操作 |

bridge 不创建、删除或修复 worktree。写入任务取消或超时后，先人工检查实际 diff、管理区和 primary checkout；终止进程不会回滚文件。

Git diff 明确使用 `--no-ext-diff --no-textconv`。index、index.lock、config.worktree、hooks、refs、logs、worktrees、其他锁文件和 object 内容都在管理证据中；发现 filter 或 alternates 时会在执行外部 helper/读取外部对象前停止。

## 修正被拒绝

修正只能引用同一 MCP server 进程中、同一 worktree 的紧邻成功实现 job：

- `E_CORRECTION_PARENT`：parent 不存在、不是成功的 implement job，或路径不同；不要猜 job ID。
- `E_CORRECTION_STATE`：parent 完成后 worktree 有中间变更；保存当前状态，重新建立干净流程。
- `E_CORRECTION_ALREADY_USED`：同一个 parent 已经有 correction child；不允许分支或重复使用。
- `E_CORRECTION_LIMIT`：已达到两轮安全上限；停止并让 Codex/调用方决定如何处理。

Correction 不是失败重试。一次实现修复流程最多安排一次 Grok 修复回归复审，再安排一次 Luna Max 独立终审；不允许递归调用插件或自动重新委派。Luna 仍返回 `needs_changes` 时，达到 correction 上限后应报告 `unverified/needs_user_decision`。

## 任务似乎卡住、结果拿不到

- 只用 `status` 查看生命周期，避免高频轮询；默认超时为 30 分钟，硬上限为 60 分钟。
- 检查 `list` 了解当前 MCP server 进程中的 job 数量；默认最多两个并发 Grok 进程。
- `E_JOB_CAPACITY` 表示内存中的 job 已达到上限；等待终态并让旧终态记录按生命周期清理，或取消仍在运行的准确 job。
- MCP server 重启后，内存中的 job 不保证存在；不要假称可以用旧 job ID 恢复。
- 如果结果被分页，使用 `result` 的 `offset` 和 `limit` 读取下一页；不要无限增大单页输出。

## 取消后或异常退出后仍有 Grok 进程

正常 `cancel` 或 MCP server 正常关闭会尝试清理准确 job 的进程组。若宿主遭遇 SIGKILL、崩溃或断电，清理代码无法运行，仍可能残留孤儿 Grok 进程，这是已知的残余风险。

先保留准确 `job_id` 和收据中的进程证据，再用系统工具核对对应 PID；不要使用 `pkill`、`killall` 或模糊匹配。确认进程确实属于该 job 后，按本机进程管理规范处理；不要自动重试或重新委派。

## 需要报告给维护者的最小信息

只提供不含秘密的信息：插件版本、Python/CLI 版本、目标模式、错误码、终态、是否为 Git linked worktree，以及脱敏后的 stderr 片段。不要附上 API key、完整环境变量、`.env`、私有源码或未脱敏收据。
