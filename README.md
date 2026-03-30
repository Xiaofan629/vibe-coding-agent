# Vibe Coding Agent

基于 Anthropic Claude API 的智能编程代理，支持工具调用、子代理、团队协作、后台任务、上下文压缩、任务管理等完整功能。

> 本项目学习自 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，在此基础上进行了配置文件化改造，并新增了 MCP 工具调用和 HTTP 工具调用能力，同时修复了若干问题。

## 架构总览

![Agent 架构流程图](https://raw.githubusercontent.com/Xiaofan629/my-image-host/refs/heads/main/agent.png)

## 全工具详细工作流

![Blog 项目全 Tool 详细流程图](https://raw.githubusercontent.com/Xiaofan629/my-image-host/refs/heads/main/example.png)

## 核心特性

### 1. 配置文件驱动

所有行为通过 `config.yaml` 控制，无需改动代码即可自定义：

- **提示词管理**：system prompt、压缩提示词、团队提示词、各类确认回复等
- **工具启用/禁用**：基础工具（bash、read_file、write_file、edit_file）可通过配置开关
- **运行参数**：max_tokens、token_threshold、超时时间、重试策略等
- **危险命令黑名单**：可配置拦截的 shell 命令
- **变量插值**：提示词支持 `{workdir}`、`{skills}`、`{name}`、`{role}`、`{team}` 等占位符

### 2. MCP 工具调用

支持通过 [Model Context Protocol](https://modelcontextprotocol.io/) 连接外部工具服务器：

- **Stdio 模式**：通过子进程 stdin/stdout 通信（如 `uvx mcp-atlassian`）
- **SSE 模式**：通过 Server-Sent Events 连接远程 MCP 服务器
- **自动注册**：连接后 MCP 服务器提供的工具自动注册为可用工具
- 配置方式与 Claude Desktop 一致，降低迁移成本

### 3. HTTP 工具调用

支持两种 HTTP 工具模式：

- **通用 `http_request`**：发送任意 HTTP 请求（GET/POST/PUT/DELETE 等），支持自定义 headers、JSON body、query params
- **预配置接口**：在 `config.yaml` 的 `http_endpoints` 中定义 API 接口，自动注册为独立工具（如 `tianji_build_ai_input`、`github_repo_get_repo`）

### 4. 完整的工具体系

| 分类 | 工具 | 说明 |
|------|------|------|
| 基础 | `bash`、`read_file`、`write_file`、`edit_file` | 可通过配置启用/禁用 |
| 任务追踪 | `TodoWrite` | 短期待办清单，限 20 项 |
| 子代理 | `task` | 派生独立子代理执行任务，最多 30 轮 |
| 技能加载 | `load_skill` | 从 YAML frontmatter 加载专业知识 |
| 上下文压缩 | `compress`、`auto_compact`、`microcompact` | 手动/自动压缩对话上下文 |
| 后台任务 | `background_run`、`check_background` | 线程池异步执行 shell 命令 |
| 持久化任务 | `task_create`、`task_get`、`task_update`、`task_list`、`claim_task` | 文件级任务管理，支持依赖关系 |
| 团队协作 | `spawn_teammate`、`list_teammates`、`send_message`、`read_inbox`、`broadcast` | 多代理协作，基于 JSONL 文件 IPC |
| 管理 | `shutdown_request`、`plan_approval`、`idle` | 团队成员生命周期管理 |
| HTTP | `http_request` | 通用 HTTP 请求 |
| MCP | `mcp_call` | MCP 服务器工具调用 |

### 5. 团队协作

- 基于 JSONL 文件的进程间通信（inbox 收件箱机制）
- 支持点对点消息、广播、计划审批
- 团队成员自动认领待处理任务
- 空闲轮询与超时自动关闭

## 相比原项目的改进

### 新增功能

1. **配置文件化**：将硬编码的提示词、工具配置、运行参数全部迁移至 `config.yaml`，支持变量插值
2. **MCP 工具调用**：新增 `mcp_call` 工具，支持 Stdio 和 SSE 两种 MCP 服务器连接方式
3. **HTTP 工具调用**：新增 `http_request` 通用工具和 `http_endpoints` 预配置接口，支持将任意 REST API 注册为工具

### Bug 修复

1. **`task_update` 双向依赖更新**：更新任务的 `blockedBy`/`blocks` 依赖时，原版只更新单侧关系。现已改为双向同步 —— 当任务 A 标记为被 B 阻塞时，B 的 `blocks` 列表会同步添加 A；反之亦然。任务完成或删除时也会正确清理双向依赖。

2. **`task_list` 排序导致字典序错乱**：原版按文件名字符串排序（`task_1.json`、`task_10.json`、`task_2.json`），导致任务列表顺序混乱。现已改为按任务 ID 数值排序。

3. **`claim_task` 缺少状态校验**：原版认领任务时未检查原状态，已完成或已删除的任务也能被认领。现已增加校验，拒绝认领非 `pending` 状态的任务。

## 快速开始

### 环境要求

- Python 3.12+
- Anthropic API Key

### 安装

```bash
git clone https://github.com/your-username/vibe-coding-agent.git
cd vibe-coding-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置

1. 创建 `.env` 文件：

```env
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL_ID=claude-sonnet-4-5-20250929
```

2. 根据需要修改 `config.yaml`，包括工具启用状态、提示词、MCP 服务器、HTTP 接口等。

### 运行

```bash
python agent.py
```

启动后进入 REPL 交互模式，支持以下命令：

- `/compact` — 手动压缩上下文
- `/tasks` — 查看任务板
- `/team` — 查看团队成员
- `/inbox` — 读取收件箱消息

## 项目结构

```
vibe-coding-agent/
├── agent.py              # 主程序（单文件，约 1600 行）
├── config.yaml           # 配置文件
├── requirements.txt      # Python 依赖
├── .env                  # 环境变量（API Key 等）
├── skills/               # 技能文件目录
│   └── blog-feature-delivery/
│       └── SKILL.md
├── .tasks/               # 持久化任务存储（JSON 文件）
├── .team/                # 团队成员状态
│   └── inbox/            # 消息收件箱（JSONL 文件）
└── .transcripts/         # 对话记录
```

## 依赖

| 包 | 用途 |
|----|------|
| `anthropic>=0.40.0` | Anthropic Claude API SDK |
| `pyyaml>=6.0` | YAML 配置文件解析 |
| `python-dotenv>=1.0.0` | `.env` 环境变量加载 |
| `requests>=2.31.0` | HTTP 请求工具 |

## 致谢

- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 原始项目，提供了基础的 Agent 架构设计
