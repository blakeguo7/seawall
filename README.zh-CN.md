# Seawall

[English](README.md) | 简体中文

Seawall 是一个用 Python 写的精简版编码 Agent 运行框架（Agent Harness）。它包含一个编码 Agent 需要的各个部件，以及围绕这些部件的一套控制手段：Agent 循环、工具层、权限与 Hooks、审批和可发现篡改的审计日志、成本与运行限制、评测框架、HTTP 服务、上下文压缩、记忆、MCP、子 Agent、可选的沙箱和终端界面。所有功能都通过一个 `seawall` 命令使用。

Seawall 的意思是海堤。海堤立在港口和大海之间，它不能让海平静下来，但能决定海水最远到哪里。审批、限制、审计日志和中断对编码 Agent 起的就是这个作用：它们限定一步错误操作能造成多大影响，并留下记录。这些是对 Agent 行为的控制，不是把 Agent 和它所在的机器隔离开。它们能保证什么、不能保证什么，见 [docs/permissions-and-audit.md](docs/permissions-and-audit.md)。

项目有意只做编码 Agent 的核心部分，这样整个代码库都能读完、都能掌握。包含什么、不包含什么，见[范围](#范围)。

`docs/` 目录下的详细文档目前只有英文版。

## 状态

核心部分已经完成并通过测试，包括前端类型检查、一次针对模拟模型接口的端到端运行，以及约 1,400 个单元测试和集成测试。审批、审计日志、成本统计、运行限制、循环守卫、评测框架和 HTTP 服务都已完成。还没做的部分列在[计划](#计划)里。

## 快速开始

环境要求：Python 3.10 及以上；交互式终端界面还需要 Node.js 18 及以上。

```bash
git clone https://github.com/blakeguo7/seawall.git
cd seawall
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # 或者：uv sync --extra dev
(cd frontend/terminal && npm ci)                              # 安装终端界面的依赖

.venv/bin/seawall setup        # 选择模型提供方并保存 API key
.venv/bin/seawall              # 进入交互式会话
```

非交互式用法：

```bash
seawall -p "Summarize what src/seawall/engine does"                 # 单次提问，输出文本
seawall -p "List the permission modes" --output-format json             # 输出结构化结果
seawall -p "Fix the failing test" --output-format stream-json           # 边运行边输出事件
seawall -p "Fix the failing test" --allowed-tools bash,edit_file        # 无人值守时无法询问，需要列出允许运行的工具
seawall -p "Fix the failing test" --permission-mode full_auto --max-budget-usd 2   # 花费达到 2 美元后，在下一次调用前停止
seawall serve --workspace ~/projects                                   # 把 Agent 作为 HTTP 服务运行（令牌来自 SEAWALL_SERVER_TOKEN）
seawall eval run --agent oracle --expect-all-pass                       # 用脚本化 Agent 跑编码评测集（不花钱）
seawall eval run --agent live --model my-model --max-budget-usd 0.5     # 用真实模型跑同一批任务
seawall -p "Refactor utils" --permission-mode plan --dry-run            # 只预览配置，不实际运行
```

`seawall` 和 `sw` 是同一个命令。Python 包名是 `seawall`，状态保存在 `~/.seawall`，环境变量都以 `SEAWALL_` 开头。

## 模型提供方

Seawall 支持两类 API：Anthropic 的 Messages API，以及兼容 OpenAI 的聊天 API。认证方式只有 API key 一种。

内置的配置有：`claude-api`（Anthropic 及兼容 Anthropic 的接口）、`openai-compatible` 和 `qwen`（阿里云 DashScope）。`seawall setup` 里还提供 DeepSeek 的预设（兼容 Anthropic 的接口）。其他提供方用自定义配置接入：

```bash
seawall provider add my-gateway --label "My gateway" --provider openai --api-format openai \
  --auth-source openai_api_key --base-url https://example.com/v1 --model my-model \
  --api-key "$MY_GATEWAY_KEY"
seawall provider use my-gateway
seawall auth status
```

API key 也可以从环境变量读取（`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY`，或者带 `SEAWALL_` 前缀的同名变量）。保存下来的 key 放在 `~/.seawall/credentials.json`（权限 600），系统钥匙串可用时则放在钥匙串里。设置保存在 `~/.seawall/settings.json`；想换位置，可以设置 `SEAWALL_CONFIG_DIR` 和 `SEAWALL_DATA_DIR`。

## 架构

```
seawall (cli.py)
 ├─ ui/            运行时组装；为 React 终端界面提供 JSON-lines 后端，以及无界面的打印模式
 ├─ engine/        QueryEngine 和 Agent 循环（query.py）：调用模型 → 执行工具调用 → 回填结果 → 重复
 ├─ api/           Anthropic 客户端和兼容 OpenAI 的客户端，带退避的重试，提供方识别
 ├─ tools/         39 个工具：文件、shell、搜索、网页、任务、定时任务、计划模式、子 Agent、MCP、Skills
 ├─ permissions/   权限模式（default、plan、full_auto），允许与拒绝名单，凭据路径拒绝名单，风险标签，审批
 ├─ audit/         按会话记录、可发现篡改的审计日志，敏感信息脱敏，`seawall audit`
 ├─ evals/         `seawall eval`：编码任务、安全场景、脚本化 Agent、报告、回归门禁
 ├─ server/        `seawall serve`：HTTP + SSE 服务，用 SQLite 保存会话和事件，租约，通过 API 审批
 ├─ hooks/         10 个生命周期事件（工具调用前后、会话开始与结束、压缩等）；支持命令、提示词、HTTP 和 Agent 四种 Hook
 ├─ services/      上下文压缩、记忆整理（"dream"）、定时调度、会话存储
 ├─ memory/        项目记忆文件，按相关性选取，并记录使用情况
 ├─ mcp/           MCP 客户端（支持 stdio 和 HTTP 两种服务器）
 ├─ sandbox/       可选的 shell 命令隔离：sandbox-runtime 或 Docker
 ├─ tasks/ swarm/ coordinator/   后台任务；子 Agent 以子进程方式运行；协调者模式
 ├─ skills/ plugins/             Markdown 写的 Skills（内置和用户自定义）以及插件包
 └─ commands/      60 多个斜杠命令（/compact、/resume、/permissions、/mcp、/provider、/diff 等）
frontend/terminal  React + Ink 终端界面，通过标准输入输出与 Python 后端通信
```

读代码建议从 [`engine/query.py`](src/seawall/engine/query.py) 里的循环开始。每一轮先把对话发给模型，再执行模型返回的工具调用（每次调用前后都会检查权限并运行 Hooks），然后把结果追加到对话里；上下文太长时进行压缩；模型不再请求工具、直接给出回答时，循环结束。

服务：`seawall serve` 把同一套运行时放在 HTTP 后面。客户端可以创建会话、发送消息、以可续传的事件流跟踪运行过程（SSE，事件带编号并保存在 SQLite 中，支持 `Last-Event-ID`）、答复审批请求、中断运行、读取审计日志。多个会话可以同时运行，并发数有上限，超出的进入队列，队列也满了就返回 `429`。会话在服务重启后仍然保留。客户端只能申请运营方允许的范围（工作区根目录、权限模式、各项限制），不能自带凭据。服务没有沙箱，也不支持多租户：只有一个令牌，工具以服务进程的用户身份运行。细节、API 和限制见 [docs/server.md](docs/server.md)。

评测：`seawall eval` 通过真实的命令运行 21 个编码任务和 19 个安全场景，并报告通过率、轮数、token 数、成本和耗时。编码任务带有隐藏测试和参考解法，并且会检查每个任务在初始状态下确实失败、解决后确实通过。安全场景模拟一个被攻破的模型去冲击各层安全防护。脚本化 Agent 让两套评测不花钱，而且结果足够稳定，可以作为 CI 的门禁；`--agent live` 则在每次试验的预算内测量真实模型。失败的试验会根据 Agent 的运行轨迹归因（改错了、没有尝试、篡改测试、触发限制、模型出错等），`seawall eval explain` 会展示证据和轨迹树。细节，以及这些分数能说明什么、不能说明什么，见 [docs/evals.md](docs/evals.md)。

成本与限制：引擎发出的每一次模型调用都会计量，`/cost` 显示估算的花费，一次运行可以按金额、token、时间或轮数设上限。Agent 反复发出同样的调用却没有进展时，会先收到警告，然后被停止。`seawall -p` 会在输出里报告停止原因、用量和成本；如果是限制导致提前结束，退出码为 2。价格属于配置项（内置的价格表只是近似值，覆盖的模型也不多）；给一个不知道价格的模型设金额预算时，Seawall 会直接拒绝，而不是忽略这个预算。细节见 [docs/limits-and-cost.md](docs/limits-and-cost.md)。

追踪：每个会话都会为每次运行、每一轮、每次模型调用、每次工具调用、每次等待审批和每次压缩记录一个带时间的 span。`seawall trace show <session>` 把一次运行打印成树，并说明时间和钱花在了哪里（模型调用、工具、等待审批、压缩）。span 里只有耗时、次数和成本，不包含消息内容，除非你主动开启。细节见 [docs/tracing.md](docs/tracing.md)。

权限：`default` 模式下，任何写入或执行命令的操作都要先询问；`plan` 模式禁止一切写入；`full_auto` 模式放行所有没有被明确拒绝的操作。凭据文件（`~/.ssh`、`~/.aws`、钥匙串等）和 `critical` 级别的命令（`rm -rf ~`、反弹 shell 等）在任何模式下都会被拒绝。需要审批但没有人可以询问的调用（比如在 `seawall -p` 或子 Agent 里），会被拒绝而不是放行。每一次决策都会写入哈希链审计日志。细节、保证和局限见 [docs/permissions-and-audit.md](docs/permissions-and-audit.md)。

## 范围

Seawall 只覆盖编码 Agent 的核心，其余部分是有意不做的：

- 不做聊天应用网关（Slack、Telegram、Discord、飞书等），也不做个人助理型 Agent。
- 不做把 GitHub issue 自动变成 pull request 的机器人。
- 只支持 API key：不支持用 Claude Code、Codex 或 GitHub Copilot 的订阅账号登录。
- 内置 Anthropic、OpenAI、DeepSeek 和 DashScope 的提供方配置，其他提供方用自定义配置。
- 子 Agent 以子进程方式运行；没有进程内的队友、终端分屏窗格、信箱，也不管理 worktree。
- 只有一个终端界面（React 和 Ink），没有主题、输出样式、vim 模式和语音模式。

Python 代码约 36,000 行，共 203 个文件；另有约 22,000 行测试代码，以及 3,400 行 TypeScript 写的终端界面。

## 计划

以下功能还没有实现，按大致的开发顺序排列：

- 每个会话一个 Docker 沙箱，并使用一次性的工作区。容器本身已经做了加固（无网络，限制 CPU、内存和进程数，去掉全部 capabilities，根文件系统只读；见 [docs/sandbox.md](docs/sandbox.md)），但目前是每个进程一个容器，所以启用沙箱时 HTTP 服务仍然会拒绝启动；另外，容器会以可读写的方式挂载你真实的项目目录。

## 开发

```bash
.venv/bin/python -m pytest -q                      # 单元测试和集成测试，不需要联网
.venv/bin/ruff check src tests scripts
(cd frontend/terminal && npx tsc --noEmit)         # 前端类型检查
```

需要和模型交互的测试都使用模拟客户端，`tests/` 里没有任何测试需要 API key。

## 许可证

MIT，见 [LICENSE](LICENSE)。
