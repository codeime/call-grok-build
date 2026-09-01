---
name: distribution-privacy-audit
description: 审计 Call Grok Build 分发内容中的硬编码绝对路径、用户名、邮箱、凭据形状、会话目录和图片元数据。用于隐私检查、发布准备、安装更新前审计；不替代 bridge 运行时安全 review。
---

# 分发隐私与硬路径审计

默认只读。用户只要求检查或 review 时不要顺手修复；明确要求修复后，才把真实身份或本机路径替换为可移植占位符，并运行相关测试。

## 审计范围

从仓库根开始先列文件名，再扫描内容。覆盖隐藏文本文件、manifest、MCP 配置、Markdown、Python、Skill、测试和图片元数据；排除 Python cache 等生成物，但不要只扫 README。

重点检查：

- /Users/name、/home/name、Windows 用户目录等个人路径；
- Desktop、Documents、Downloads、/var/folders、IDE、会话和临时剪贴板路径；
- 真实姓名、邮箱、Git author/remote、账号名和机器名；
- token、API key、Authorization、URL userinfo、cookie、.env 和证书路径；
- manifest、.mcp.json、Markdown 链接或启动参数中的本机绝对路径；
- bridge/probe/子进程错误是否直接拼接自定义可执行文件、配置文件或 cwd 绝对路径；
- direct 模式的文件工具是否越过任务边界读取敏感 Git 元数据或账号目录；
- 公开收据中的 commit OID、remote、author、分支名和相对文件清单等可关联私有项目指纹；
- PNG 等分发资源的文本块、profile、注释或生成器元数据。

可使用以下有界检查作为起点，并根据结果人工分类：

~~~text
rg --files --hidden -g '!__pycache__/**' -g '!*.pyc'
rg -n --hidden -i -g '!assets/*.png' -g '!__pycache__/**' -g '!*.pyc' '(/Users/[^/[:space:]]+|/home/[^/[:space:]]+|[A-Za-z]:\\Users\\[^\\[:space:]]+|/var/folders/|Documents/|Desktop/|Downloads/|[[:alnum:]._%+-]+@[[:alnum:].-]+\.[A-Za-z]{2,})' .
file assets/*.png
~~~

如果目标有 Git 元数据，还要检查 remote 和最近提交作者，但不要在报告中无必要地复制真实个人值。没有 Git 元数据时，明确说明无法审计历史，不要虚构 clean status。

## 分类规则

以下命中通常是有意的安全边界，不应机械删除：

- scripts/grok_build_bridge.py 中账号路径脱敏和系统目录拒绝规则；
- /tmp、/private/tmp、/etc、/Library、/System 等 scope 测试；
- /opt/grok、loopback 地址、example.invalid 和 SYNTHETIC_* 测试夹具；
- 文档中的 /path/to/... 明确占位符。

分别标记：

1. 真实泄露：能识别个人、机器、会话或私有 workspace，必须在分发前移除；
2. 可移植性硬编码：没有个人信息，但运行依赖某台机器的绝对路径，应改为相对路径、参数或运行时解析；
3. 安全边界样本：用于拒绝、脱敏或回归测试，保留并确认使用合成值；
4. 误报：二进制字节、通用协议字段或保留域，记录判定依据。

账号路径已经脱敏，不代表其他绝对路径也安全。对外错误优先使用命令 basename、稳定错误码或显式加入脱敏上下文；不要回显自定义安装目录。Git 元数据和相对文件结构可能形成可关联的项目指纹，应单独标为条件性隐私风险。

## v2 运行边界

- Codex 传入当前 workspace 的绝对 cwd，Grok 在同一 cwd direct 运行；
- paths 只作 prompt focus，不是访问控制；
- bridge 不复制项目、不建立辅助目录、不扫描 Git 或整个目录内容；
- implement 允许 primary、dirty 和非 Git workspace；Codex 负责任务前后状态、测试和 Luna Max review；
- 结果使用 grok.codex.result.v2，workspace.integrity_snapshot 为 not_collected；
- 公开结果、错误和诊断必须经过递归凭据、邮箱和账号路径脱敏。

审计不得要求用户重复确认已经明确点名的 Grok Build 外发授权。它也不能把敏感文件扫描结果当成可以自动扩大任务范围的许可。

## 分发门槛

修复后运行完整测试和宿主提供的官方 plugin/Skill validator。若在 Git checkout 中，再运行 git diff --check 并只审查本次变更。安装、版本更新、推送和发布仍需明确授权。

报告应列出检查范围、真实 finding、允许保留的命中、direct 的 Git 元数据边界、项目指纹、历史和图片元数据的验证边界，以及实际运行的命令。没有 finding 时也要说明哪些范围已经检查、哪些因环境缺失未检查。
