#!/usr/bin/env python3
"""
智能编程代理 - 完整版

功能：工具调用、子代理、团队成员协作、后台任务、上下文压缩、任务管理等
所有提示词和工具配置均在 config.yaml 中管理。
REPL 命令：/compact /tasks /team /inbox
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional

import requests
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

# ==================== 加载环境变量 ====================
load_dotenv(override=True)

# ==================== 加载配置文件 ====================
WORKDIR = Path.cwd()
CONFIG_PATH = WORKDIR / "config.yaml"


def load_config() -> dict:
    """加载 YAML 配置文件。"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在：{CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()

# ==================== 从配置读取设置 ====================
_SETTINGS = CONFIG.get("settings", {})
MAX_TOKENS = _SETTINGS.get("max_tokens", 8000)
TOKEN_THRESHOLD = _SETTINGS.get("token_threshold", 100000)
POLL_INTERVAL = _SETTINGS.get("poll_interval", 5)
IDLE_TIMEOUT = _SETTINGS.get("idle_timeout", 60)
BASH_TIMEOUT = _SETTINGS.get("bash_timeout", 120)
MAX_OUTPUT = _SETTINGS.get("max_output_length", 50000)
MAX_SUBAGENT_ROUNDS = _SETTINGS.get("max_subagent_rounds", 30)
MAX_TEAMMATE_ROUNDS = _SETTINGS.get("max_teammate_rounds", 50)
REPL_PROMPT = _SETTINGS.get("repl_prompt", "agent >> ")
API_RETRY_COUNT = _SETTINGS.get("api_retry_count", 3)
API_RETRY_DELAY = _SETTINGS.get("api_retry_delay", 2)
DANGEROUS_COMMANDS = CONFIG.get("dangerous_commands", ["rm -rf /", "sudo", "shutdown", "reboot"])
PROMPTS = CONFIG.get("prompts", {})

# HTTP 配置
_HTTP_CONFIG = CONFIG.get("http", {})
HTTP_TIMEOUT = _HTTP_CONFIG.get("timeout", 30)
HTTP_MAX_RESPONSE = _HTTP_CONFIG.get("max_response_size", 100000)
HTTP_DEFAULT_HEADERS = _HTTP_CONFIG.get("default_headers", {})

# MCP 服务器配置
MCP_SERVERS_CONFIG = CONFIG.get("mcp_servers", {})

# HTTP 接口配置
HTTP_ENDPOINTS = CONFIG.get("http_endpoints", {})

# ==================== 初始化 API 客户端 ====================
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ==================== 目录常量 ====================
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

# 合法消息类型
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response", "plan_request"}

# ==================== 工具 Schema 定义 ====================
# Schema 结构定义在代码中（属于 API 协议），描述和启用状态在 config.yaml 中配置
TOOL_SCHEMAS = {
    "bash": {"type": "object",
             "properties": {"command": {"type": "string"}},
             "required": ["command"]},
    "read_file": {"type": "object",
                  "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                  "required": ["path"]},
    "write_file": {"type": "object",
                   "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]},
    "edit_file": {"type": "object",
                  "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                  "required": ["path", "old_text", "new_text"]},
    "TodoWrite": {"type": "object",
                  "properties": {"items": {"type": "array", "items": {"type": "object",
                    "properties": {"content": {"type": "string"},
                                   "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                   "activeForm": {"type": "string"}},
                    "required": ["content", "status", "activeForm"]}}},
                  "required": ["items"]},
    "task": {"type": "object",
             "properties": {"prompt": {"type": "string"},
                            "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}},
             "required": ["prompt"]},
    "load_skill": {"type": "object",
                   "properties": {"name": {"type": "string"}},
                   "required": ["name"]},
    "compress": {"type": "object", "properties": {}},
    "background_run": {"type": "object",
                       "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
                       "required": ["command"]},
    "check_background": {"type": "object",
                         "properties": {"task_id": {"type": "string"}}},
    "task_create": {"type": "object",
                    "properties": {"subject": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["subject"]},
    "task_get": {"type": "object",
                 "properties": {"task_id": {"type": "integer"}},
                 "required": ["task_id"]},
    "task_update": {"type": "object",
                    "properties": {"task_id": {"type": "integer"},
                                   "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                                   "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                                   "add_blocks": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["task_id"]},
    "task_list": {"type": "object", "properties": {}},
    "claim_task": {"type": "object",
                   "properties": {"task_id": {"type": "integer"}},
                   "required": ["task_id"]},
    "spawn_teammate": {"type": "object",
                       "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}},
                       "required": ["name", "role", "prompt"]},
    "list_teammates": {"type": "object", "properties": {}},
    "send_message": {"type": "object",
                     "properties": {"to": {"type": "string"}, "content": {"type": "string"},
                                    "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}},
                     "required": ["to", "content"]},
    "read_inbox": {"type": "object", "properties": {}},
    "broadcast": {"type": "object",
                  "properties": {"content": {"type": "string"}},
                  "required": ["content"]},
    "shutdown_request": {"type": "object",
                         "properties": {"teammate": {"type": "string"}},
                         "required": ["teammate"]},
    "plan_approval": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]},
    "idle": {"type": "object", "properties": {}},
    "http_request": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"], "default": "GET"},
            "url": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"oneOf": [{"type": "string"}, {"type": "object"}]},
            "params": {"type": "object", "additionalProperties": {"type": "string"}},
            "timeout": {"type": "integer"}
        },
        "required": ["url"]
    },
    "mcp_call": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "MCP 工具全名，格式：mcp_<服务器名>_<工具名>"},
            "arguments": {"type": "object", "description": "工具参数"}
        },
        "required": ["tool_name"]
    },
}

# 基础工具：可通过配置启用/禁用
OPTIONAL_TOOLS = {"bash", "read_file", "write_file", "edit_file"}

# 启用的工具名称集合 = 所有非基础工具（始终启用）+ 配置中启用的基础工具
_all_tools_in_config = {name for name in CONFIG.get("tools", {}).keys()}
_all_non_optional = {name for name in TOOL_SCHEMAS if name not in OPTIONAL_TOOLS}
ENABLED_TOOLS = _all_non_optional | {
    name for name, cfg in CONFIG.get("tools", {}).items()
    if name in OPTIONAL_TOOLS and cfg.get("enabled", True)
}


def build_tools() -> list:
    """根据配置构建发送给模型的工具列表（基础工具按配置，其他工具始终包含）。"""
    tools = []
    tool_config = CONFIG.get("tools", {})
    # 添加配置中启用的基础工具
    for name in OPTIONAL_TOOLS:
        if name not in ENABLED_TOOLS:
            continue
        schema = TOOL_SCHEMAS.get(name)
        if schema:
            cfg = tool_config.get(name, {})
            tools.append({
                "name": name,
                "description": cfg.get("description", ""),
                "input_schema": schema,
            })
    # 添加所有非基础工具（始终启用）
    for name, schema in TOOL_SCHEMAS.items():
        if name in OPTIONAL_TOOLS:
            continue  # 基础工具已单独处理
        cfg = tool_config.get(name, {})
        tools.append({
            "name": name,
            "description": cfg.get("description", ""),
            "input_schema": schema,
        })
    # 添加预配置的 HTTP 接口工具
    http_tools = build_http_tools()
    tools.extend(http_tools)
    # 添加 MCP 工具
    mcp_tools = MCP_MANAGER.get_all_tools()
    for mcp_tool in mcp_tools:
        tools.append({
            "name": mcp_tool["name"],
            "description": f"[MCP/{mcp_tool['mcp_server']}] {mcp_tool.get('description', '')}",
            "input_schema": mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return tools


def build_http_tools() -> list:
    """从配置构建 HTTP 接口工具列表。"""
    tools = []
    for api_name, api_config in HTTP_ENDPOINTS.items():
        base_url = api_config.get("base_url", "")
        api_headers = api_config.get("headers", {})
        endpoints = api_config.get("endpoints", [])

        for endpoint in endpoints:
            tool_name = f"http_{api_name}_{endpoint['name']}"
            method = endpoint.get("method", "GET")
            path = endpoint.get("path", "")
            endpoint_desc = endpoint.get("description", "")
            params = endpoint.get("params", {})
            endpoint_headers = endpoint.get("headers", {})

            # 构建 JSON Schema properties
            properties = {}
            required = []
            for param_name, param_config in params.items():
                prop = {"type": "string"}
                if param_config.get("description"):
                    prop["description"] = param_config["description"]
                if param_config.get("required"):
                    required.append(param_name)
                properties[param_name] = prop

            tools.append({
                "name": tool_name,
                "description": f"[{api_name}] {endpoint_desc}",
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                },
                # 存储元数据用于调用时使用
                "_meta": {
                    "api_name": api_name,
                    "base_url": base_url,
                    "method": method,
                    "path": path,
                    "headers": {**api_headers, **endpoint_headers},
                    "params": params
                }
            })
    return tools


# ==================== 基础工具函数 ====================


def safe_path(p: str) -> Path:
    """安全路径解析，防止路径逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作目录范围：{p}")
    return path


def run_bash(command: str) -> str:
    """安全地执行 shell 命令。"""
    if any(d in command for d in DANGEROUS_COMMANDS):
        return "错误：危险命令已被拦截"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=BASH_TIMEOUT)
        out = (r.stdout + r.stderr).strip()
        return out[:MAX_OUTPUT] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return f"错误：命令超时（{BASH_TIMEOUT}秒）"


def run_read(path: str, limit: int = None) -> str:
    """读取文件内容。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:MAX_OUTPUT]
    except Exception as e:
        return f"错误：{e}"


def run_write(path: str, content: str) -> str:
    """写入文件。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误：{e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件（精确替换）。"""
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"错误：在 {path} 中未找到要替换的文本"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


# ==================== HTTP 请求工具 ====================


def call_http_endpoint(tool_name: str, arguments: dict) -> str:
    """调用预配置的 HTTP 接口。"""
    # 查找工具元数据
    tool_meta = None
    for tool in TOOLS:
        if tool.get("name") == tool_name and "_meta" in tool:
            tool_meta = tool["_meta"]
            break

    if not tool_meta:
        return f"错误：未找到 HTTP 接口配置 '{tool_name}'"

    # 构建请求 URL
    path = tool_meta["path"]
    params_config = tool_meta.get("params", {})

    # 替换路径参数 (如 {owner}/{repo})
    for param_name in params_config:
        placeholder = "{" + param_name + "}"
        if placeholder in path and param_name in arguments:
            path = path.replace(placeholder, str(arguments[param_name]))

    url = tool_meta["base_url"] + path

    # 构建查询参数（不在路径中的参数）
    query_params = {}
    for param_name, param_config in params_config.items():
        placeholder = "{" + param_name + "}"
        if placeholder not in tool_meta["path"] and param_name in arguments:
            query_params[param_name] = arguments[param_name]

    # 合并 headers
    headers = {**HTTP_DEFAULT_HEADERS, **tool_meta.get("headers", {})}

    try:
        response = requests.request(
            method=tool_meta["method"],
            url=url,
            params=query_params,
            headers=headers,
            timeout=HTTP_TIMEOUT
        )

        result = {
            "status_code": response.status_code,
            "url": response.url,
        }

        # 尝试解析 JSON
        try:
            result["body"] = response.json()
        except:
            body_text = response.text
            if len(body_text) > HTTP_MAX_RESPONSE:
                body_text = body_text[:HTTP_MAX_RESPONSE] + f"\n... (已截断，共 {len(body_text)} 字节)"
            result["body"] = body_text

        return json.dumps(result, ensure_ascii=False, indent=2)

    except requests.Timeout:
        return f"错误：请求超时（超过 {HTTP_TIMEOUT} 秒）"
    except requests.ConnectionError as e:
        return f"错误：连接失败 - {e}"
    except Exception as e:
        return f"错误：{e}"


def run_http_request(method: str, url: str, headers: dict = None,
                     body: Any = None, params: dict = None, timeout: int = None) -> str:
    """发送 HTTP 请求。"""
    try:
        # 合并默认 headers
        req_headers = {**HTTP_DEFAULT_HEADERS}
        if headers:
            req_headers.update(headers)

        # 处理 body
        req_body = None
        if body is not None:
            if isinstance(body, dict):
                req_body = json.dumps(body)
                req_headers.setdefault("Content-Type", "application/json")
            else:
                req_body = str(body)

        # 发送请求
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=req_headers,
            data=req_body,
            params=params,
            timeout=timeout or HTTP_TIMEOUT,
            allow_redirects=True
        )

        # 构建结果
        result = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "url": response.url,
        }

        # 尝试解析 JSON
        try:
            result["body"] = response.json()
        except:
            # 限制文本响应大小
            body_text = response.text
            if len(body_text) > HTTP_MAX_RESPONSE:
                body_text = body_text[:HTTP_MAX_RESPONSE] + f"\n... (已截断，共 {len(body_text)} 字节)"
            result["body"] = body_text

        return json.dumps(result, ensure_ascii=False, indent=2)

    except requests.Timeout:
        return f"错误：请求超时（超过 {timeout or HTTP_TIMEOUT} 秒）"
    except requests.ConnectionError as e:
        return f"错误：连接失败 - {e}"
    except Exception as e:
        return f"错误：{e}"


# ==================== MCP 客户端 ====================


class MCPServer:
    """MCP 服务器客户端基类。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.tools: List[Dict] = []
        self.resources: List[Dict] = []
        self._initialized = False

    def initialize(self) -> bool:
        """初始化连接。"""
        raise NotImplementedError

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用工具。"""
        raise NotImplementedError

    def list_tools(self) -> List[Dict]:
        """列出可用工具。"""
        return self.tools

    def cleanup(self):
        """清理资源。"""
        pass


class StdioMCPServer(MCPServer):
    """通过 stdio 连接的 MCP 服务器。"""

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.process: Optional[subprocess.Popen] = None

    def initialize(self) -> bool:
        """启动子进程并初始化。"""
        try:
            cmd = self.config.get("command")
            args = self.config.get("args", [])
            env = self.config.get("env", {})

            if not cmd:
                return False

            # 合并环境变量
            full_env = {**os.environ, **env}

            # 启动进程
            self.process = subprocess.Popen(
                [cmd] + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=full_env,
                bufsize=0
            )

            # 发送初始化请求
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "vibe-coding-agent",
                        "version": "1.0.0"
                    }
                }
            }

            self._send_message(init_request)
            response = self._read_message()

            if response and response.get("result"):
                self._initialized = True
                # 发送 initialized 通知
                self._send_message({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                })
                # 获取工具列表
                self._fetch_tools()
                return True

            return False

        except Exception as e:
            print(f"[MCP] 初始化 {self.name} 失败: {e}")
            return False

    def _send_message(self, msg: dict):
        """发送 JSON-RPC 消息。"""
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(msg) + "\n"
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def _read_message(self) -> Optional[dict]:
        """读取 JSON-RPC 消息。"""
        if not self.process or not self.process.stdout:
            return None
        line = self.process.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line.strip())
        except:
            return None

    def _fetch_tools(self):
        """获取工具列表。"""
        self._send_message({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        })
        response = self._read_message()
        if response and "result" in response:
            self.tools = response["result"].get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用工具。"""
        if not self._initialized:
            return f"错误：MCP 服务器 {self.name} 未初始化"

        try:
            self._send_message({
                "jsonrpc": "2.0",
                "id": int(time.time()),
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments or {}
                }
            })

            response = self._read_message()
            if response and "result" in response:
                result = response["result"]
                # 处理不同类型的结果
                if isinstance(result, list):
                    for item in result:
                        if item.get("type") == "text":
                            return item.get("text", "")
                    return str(result)
                return str(result)
            elif response and "error" in response:
                return f"MCP 错误：{response['error']}"

            return "工具调用完成，但无返回内容"

        except Exception as e:
            return f"错误：{e}"

    def cleanup(self):
        """清理进程。"""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except:
                self.process.kill()


class SSEMCPServer(MCPServer):
    """通过 SSE 连接的 MCP 服务器。"""

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.url = config.get("url")
        self.headers = config.get("headers", {})
        self.session: Optional[requests.Session] = None

    def initialize(self) -> bool:
        """初始化 SSE 连接。"""
        try:
            self.session = requests.Session()
            self.session.headers.update(self.headers)

            # 发送初始化请求
            response = self.session.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "vibe-coding-agent", "version": "1.0.0"}
                    }
                },
                timeout=HTTP_TIMEOUT
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("result"):
                    self._initialized = True
                    self._fetch_tools()
                    return True

            return False

        except Exception as e:
            print(f"[MCP] SSE 初始化 {self.name} 失败: {e}")
            return False

    def _fetch_tools(self):
        """获取工具列表。"""
        if not self.session:
            return
        try:
            response = self.session.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list"
                },
                timeout=HTTP_TIMEOUT
            )
            if response.status_code == 200:
                result = response.json()
                if "result" in result:
                    self.tools = result["result"].get("tools", [])
        except Exception as e:
            print(f"[MCP] 获取工具列表失败: {e}")

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用工具。"""
        if not self._initialized or not self.session:
            return f"错误：MCP 服务器 {self.name} 未初始化"

        try:
            response = self.session.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": int(time.time()),
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments or {}
                    }
                },
                timeout=HTTP_TIMEOUT
            )

            if response.status_code == 200:
                result = response.json()
                if "result" in result:
                    return str(result["result"])
                elif "error" in result:
                    return f"MCP 错误：{result['error']}"

            return f"错误：HTTP {response.status_code}"

        except Exception as e:
            return f"错误：{e}"

    def cleanup(self):
        """清理会话。"""
        if self.session:
            self.session.close()


class MCPManager:
    """管理所有 MCP 服务器连接。"""

    def __init__(self):
        self.servers: Dict[str, MCPServer] = {}
        self._initialize_servers()

    def _initialize_servers(self):
        """从配置初始化所有 MCP 服务器。"""
        for name, config in MCP_SERVERS_CONFIG.items():
            if not config:
                continue

            server: MCPServer
            if "url" in config:
                server = SSEMCPServer(name, config)
            else:
                server = StdioMCPServer(name, config)

            if server.initialize():
                self.servers[name] = server
                print(f"[MCP] 已连接服务器: {name} ({len(server.tools)} 个工具)")
            else:
                print(f"[MCP] 连接失败: {name}")

    def get_all_tools(self) -> List[Dict]:
        """获取所有服务器的工具。"""
        all_tools = []
        for server_name, server in self.servers.items():
            for tool in server.list_tools():
                all_tools.append({
                    **tool,
                    "mcp_server": server_name,
                    "name": f"mcp_{server_name}_{tool['name']}"
                })
        return all_tools

    def call_tool(self, full_tool_name: str, arguments: dict) -> str:
        """调用 MCP 工具。"""
        # 解析工具名：mcp_serverName_toolName
        if not full_tool_name.startswith("mcp_"):
            return "错误：无效的 MCP 工具名"

        parts = full_tool_name.split("_", 2)
        if len(parts) < 3:
            return "错误：无效的 MCP 工具名格式"

        server_name = parts[1]
        tool_name = parts[2]

        server = self.servers.get(server_name)
        if not server:
            return f"错误：未找到 MCP 服务器 '{server_name}'"

        return server.call_tool(tool_name, arguments)

    def list_servers(self) -> str:
        """列出所有服务器状态。"""
        if not self.servers:
            return "未连接任何 MCP 服务器。"

        lines = []
        for name, server in self.servers.items():
            tool_count = len(server.tools)
            status = "已连接" if server._initialized else "未初始化"
            lines.append(f"- {name}: {status}, {tool_count} 个工具")
        return "\n".join(lines)

    def cleanup(self):
        """清理所有连接。"""
        for server in self.servers.values():
            server.cleanup()


# ==================== 待办管理器 ====================


class TodoManager:
    """管理代理的短期待办事项列表。"""

    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        validated, in_progress_count = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            active_form = str(item.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"第 {i} 项：content 为必填")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"第 {i} 项：无效状态 '{status}'")
            if not active_form:
                raise ValueError(f"第 {i} 项：activeForm 为必填")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"content": content, "status": status, "activeForm": active_form})
        if len(validated) > 20:
            raise ValueError("最多 20 条待办")
        if in_progress_count > 1:
            raise ValueError("只能有一条 in_progress 状态的待办")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无待办。"
        lines = []
        status_icon = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        for item in self.items:
            icon = status_icon.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{icon} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n(已完成 {done}/{len(self.items)})")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item["status"] != "completed" for item in self.items)


# ==================== 模型调用（带重试）====================


def call_model_with_retry(**kwargs):
    """调用模型 API，失败时自动重试。"""
    for attempt in range(API_RETRY_COUNT):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            is_last = attempt == API_RETRY_COUNT - 1
            if is_last:
                print(f"[API] 调用失败（已重试 {API_RETRY_COUNT} 次）: {e}")
                raise
            print(f"[API] 调用失败（{e}），{API_RETRY_DELAY} 秒后重试 ({attempt + 1}/{API_RETRY_COUNT})...")
            time.sleep(API_RETRY_DELAY)


# ==================== 子代理 ====================


def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """启动子代理执行独立任务，返回结果摘要。"""
    sub_tools = [
        {"name": "bash", "description": "执行命令。",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "读取文件。",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    # Explore 类型只读，其他类型可写
    if agent_type != "Explore":
        sub_tools += [
            {"name": "write_file", "description": "写入文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "编辑文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]
    handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    messages = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(MAX_SUBAGENT_ROUNDS):
        resp = call_model_with_retry(model=MODEL, messages=messages, tools=sub_tools, max_tokens=MAX_TOKENS)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                handler = handlers.get(b.name, lambda **kw: "未知工具")
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": str(handler(**b.input))[:MAX_OUTPUT]})
        messages.append({"role": "user", "content": results})
    if resp:
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or PROMPTS.get("subagent_ack", "(子代理执行完毕)")
    return "(子代理执行失败)"


# ==================== 技能加载器 ====================


class SkillLoader:
    """从 skills/ 目录加载 SKILL.md 文件中的技能知识。"""

    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills:
            return "(无可用技能)"
        return "\n".join(f"  - {n}：{s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            return f"错误：未知技能 '{name}'。可用技能：{', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# ==================== 上下文压缩 ====================


def estimate_tokens(messages: list) -> int:
    """粗略估算消息列表的 token 数。"""
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: list):
    """微型压缩：清除旧的工具输出，只保留最近 3 条。"""
    tool_results = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append(part)
    if len(tool_results) <= 3:
        return
    for part in tool_results[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[已清除]"


def auto_compact(messages: list) -> list:
    """自动压缩：保存完整记录并让模型生成摘要。"""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    conv_text = json.dumps(messages, default=str)[:80000]
    compress_prompt = PROMPTS.get("compress", "总结以下对话：\n{text}").format(text=conv_text)
    resp = call_model_with_retry(
        model=MODEL,
        messages=[{"role": "user", "content": compress_prompt}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    return [
        {"role": "user", "content": f"[已压缩。完整记录：{path}]\n{summary}"},
        {"role": "assistant", "content": PROMPTS.get("compress_ack", "收到，基于摘要继续工作。")},
    ]


# ==================== 持久化任务管理器 ====================


class TaskManager:
    """基于文件系统的持久化任务管理（.tasks/ 目录）。"""

    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"任务 {tid} 不存在")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": [], "blocks": []}
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            # 完成时清除其他任务对该任务的阻塞依赖
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
                # 同时清除当前任务的 blocks 依赖
                if task.get("blocks"):
                    for blocker_id in task["blocks"]:
                        blocker = self._load(blocker_id) if isinstance(blocker_id, int) else None
                        if blocker:
                            blocker["blockedBy"] = [bid for bid in blocker.get("blockedBy", []) if bid != tid]
                            self._save(blocker)
                    task["blocks"] = []
            if status == "deleted":
                # 删除时清除其他任务对该任务的所有依赖
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
                    if tid in t.get("blocks", []):
                        t["blocks"].remove(tid)
                        self._save(t)
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"任务 {tid} 已删除"
        if add_blocked_by:
            # 双向更新：添加到当前任务的 blockedBy，同时更新被依赖任务的 blocks
            for blocker_id in add_blocked_by:
                try:
                    blocker = self._load(blocker_id)
                    if tid not in blocker.get("blocks", []):
                        blocker["blocks"] = blocker.get("blocks", []) + [tid]
                        self._save(blocker)
                except ValueError:
                    pass  # 被依赖任务不存在，忽略
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            # 双向更新：添加到当前任务的 blocks，同时更新被阻塞任务的 blockedBy
            for blocked_id in add_blocks:
                try:
                    blocked_task = self._load(blocked_id)
                    if tid not in blocked_task.get("blockedBy", []):
                        blocked_task["blockedBy"] = blocked_task.get("blockedBy", []) + [tid]
                        self._save(blocked_task)
                except ValueError:
                    pass  # 被阻塞任务不存在，忽略
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        # 按任务 ID 数值排序，而非字符串排序
        task_files = list(TASKS_DIR.glob("task_*.json"))
        task_files.sort(key=lambda f: int(f.stem.split("_")[1]))
        tasks = [json.loads(f.read_text()) for f in task_files]
        if not tasks:
            return "暂无任务。"
        lines = []
        for t in tasks:
            icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (阻塞于：{t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{icon} #{t['id']}：{t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        current_status = task.get("status", "pending")
        # 校验状态：已完成或已删除的任务不能被认领
        if current_status == "completed":
            return f"任务 #{tid} 已完成，无需认领。"
        if current_status == "deleted":
            return f"任务 #{tid} 已删除，无法认领。"
        # 如果已被他人认领且进行中，提示
        current_owner = task.get("owner")
        if current_status == "in_progress" and current_owner and current_owner != owner:
            return f"任务 #{tid} 已被 {current_owner} 认领，请勿重复认领。"
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return PROMPTS.get("task_claimed", "已为 {owner} 认领任务 #{task_id}").format(owner=owner, task_id=tid)


# ==================== 后台任务管理器 ====================


class BackgroundManager:
    """在后台线程中执行命令，不阻塞主循环。"""

    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = None) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout or BASH_TIMEOUT), daemon=True).start()
        tpl = PROMPTS.get("background_started", "后台任务 {task_id} 已启动：{command}")
        return tpl.format(task_id=tid, command=command[:80])

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:MAX_OUTPUT]
            self.tasks[tid].update({"status": "completed", "result": output or "(无输出)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})

    def check(self, tid: str = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(运行中)')}" if t else f"未知任务：{tid}"
        return "\n".join(f"{k}：[{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "无后台任务。"

    def drain(self) -> list:
        """取出所有待处理的后台通知。"""
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# ==================== 消息总线 ====================


class MessageBus:
    """基于文件系统的进程间消息通信（.team/inbox/ 目录）。"""

    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        msg = {"type": msg_type, "from": sender, "content": content, "timestamp": time.time()}
        if extra:
            msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"已发送 {msg_type} 给 {to}"

    def read_inbox(self, name: str) -> list:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return []
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")  # 读取后清空
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"已向 {count} 位团队成员广播"


# ==================== 关闭与审批协议 ====================

shutdown_requests = {}
plan_requests = {}


def handle_shutdown_request(teammate: str) -> str:
    """向团队成员发送关闭请求。"""
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "请关闭。", "shutdown_request", {"request_id": req_id})
    return f"已向 '{teammate}' 发送关闭请求 {req_id}"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """审批团队成员的计划。"""
    req = plan_requests.get(request_id)
    if not req:
        return f"错误：未知的计划请求 '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    status = "已批准" if approve else "已拒绝"
    return f"来自 '{req['from']}' 的计划{status}"


# ==================== 团队成员管理器 ====================


class TeammateManager:
    """管理自主团队成员的创建、工作和空闲生命周期。"""

    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        member = self._find(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """创建并启动一个团队成员。"""
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误：'{name}' 当前状态为 {member['status']}，无法重新启动"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"已启动 '{name}'（角色：{role}）"

    def _loop(self, name: str, role: str, prompt: str):
        """团队成员的主循环：工作阶段 -> 空闲阶段 -> 循环。"""
        team_name = self.config["team_name"]
        sys_prompt = PROMPTS.get("teammate", "").format(
            name=name, role=role, team=team_name, workdir=WORKDIR)
        messages = [{"role": "user", "content": prompt}]
        tools = [
            {"name": "bash", "description": "执行命令。",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "读取文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "写入文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "编辑文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "发送消息。",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "表示当前工作完成。",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "按 ID 认领任务。",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            {"name": "request_approval", "description": "向 Lead 请求计划审批。需要审批时会生成 request_id，等待 Lead 调用 plan_approval 工具处理。",
             "input_schema": {"type": "object",
                 "properties": {"plan": {"type": "string", "description": "计划内容"},
                               "reason": {"type": "string", "description": "需要审批的原因"}},
                 "required": ["plan", "reason"]}},
        ]
        while True:
            # ---- 工作阶段 ----
            for _ in range(MAX_TEAMMATE_ROUNDS):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    response = call_model_with_retry(
                        model=MODEL, system=sys_prompt, messages=messages,
                        tools=tools, max_tokens=MAX_TOKENS)
                except Exception:
                    self._set_status(name, "shutdown")
                    return
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            idle_requested = True
                            output = "进入空闲阶段。"
                        elif block.name == "claim_task":
                            output = self.task_mgr.claim(block.input["task_id"], name)
                        elif block.name == "send_message":
                            output = self.bus.send(name, block.input["to"], block.input["content"])
                        elif block.name == "request_approval":
                            # 创建审批请求并存入全局 plan_requests
                            req_id = str(uuid.uuid4())[:8]
                            plan_requests[req_id] = {
                                "from": name,
                                "plan": block.input["plan"],
                                "reason": block.input.get("reason", ""),
                                "status": "pending"
                            }
                            # 向 Lead 发送审批请求通知
                            output = self.bus.send(name, "lead",
                                f"审批请求 [{req_id}]：{block.input.get('reason', '')}\n计划：{block.input['plan']}",
                                "plan_request",
                                {"request_id": req_id})
                        else:
                            dispatch = {
                                "bash": lambda **kw: run_bash(kw["command"]),
                                "read_file": lambda **kw: run_read(kw["path"]),
                                "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
                            }
                            output = dispatch.get(block.name, lambda **kw: "未知工具")(**block.input)
                        print(f"  [{name}] {block.name}：{str(output)[:120]}")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break

            # ---- 空闲阶段：轮询消息和未认领任务 ----
            self._set_status(name, "idle")
            resume = False
            for _ in range(max(IDLE_TIMEOUT // max(POLL_INTERVAL, 1), 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        # 处理审批响应：从 plan_requests 中移除已处理的请求
                        if msg.get("type") == "plan_approval_response":
                            req_id = msg.get("request_id")
                            if req_id in plan_requests:
                                del plan_requests[req_id]
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                # 自动认领未分配的任务
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    # 压缩后重新注入身份信息
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>你是 '{name}'，角色：{role}，团队：{team_name}。</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"我是 {name}，继续工作。"})
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>任务 #{task['id']}：{task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"已认领任务 #{task['id']}，正在处理。"})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]:
            return "暂无团队成员。"
        lines = [f"团队：{self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']}（{m['role']}）：{m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# ==================== 全局实例 ====================

TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)
MCP_MANAGER = MCPManager()

# ==================== 系统提示词 ====================

def build_system_prompt() -> str:
    """构建系统提示词，包含 MCP 服务器信息。"""
    mcp_info = MCP_MANAGER.list_servers()
    base_prompt = PROMPTS.get("system", "").format(workdir=WORKDIR, skills=SKILLS.descriptions())
    if mcp_info and "未连接" not in mcp_info:
        base_prompt += f"\n\n已连接的 MCP 服务器：\n{mcp_info}\n"
        base_prompt += "MCP 工具名格式：mcp_<服务器名>_<工具名>，例如 mcp_fetch_read_url"
    return base_prompt

SYSTEM = build_system_prompt()

# ==================== 工具构建 ====================

TOOLS = build_tools()

# 所有可用的工具处理器
_ALL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    "compress":         lambda **kw: "正在压缩...",
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", BASH_TIMEOUT)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"),
                                                      kw.get("add_blocked_by"), kw.get("add_blocks")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":             lambda **kw: "主代理不会进入空闲状态。",
    "http_request":     lambda **kw: run_http_request(
                            kw.get("method", "GET"),
                            kw["url"],
                            kw.get("headers"),
                            kw.get("body"),
                            kw.get("params"),
                            kw.get("timeout")
                        ),
    "mcp_call":         lambda **kw: MCP_MANAGER.call_tool(kw.get("tool_name", ""), kw.get("arguments", {})),
}

# 仅保留启用的工具处理器
TOOL_HANDLERS = {name: handler for name, handler in _ALL_HANDLERS.items() if name in ENABLED_TOOLS}


# ==================== 主循环 ====================


def agent_loop(messages: list):
    """核心循环：调用模型 -> 执行工具 -> 循环直到模型给出最终回答。"""
    rounds_without_todo = 0
    while True:
        # 上下文压缩
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[自动压缩触发]")
            messages[:] = auto_compact(messages)

        # 收集后台任务通知
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[后台:{n['task_id']}] {n['status']}：{n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            messages.append({"role": "assistant", "content": PROMPTS.get("background_ack", "已记录后台任务结果。")})

        # 检查主代理收件箱
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
            messages.append({"role": "assistant", "content": PROMPTS.get("inbox_ack", "已记录收件箱消息。")})

        # 调用模型
        response = call_model_with_retry(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=MAX_TOKENS,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 模型没有调用工具，输出并返回最终回答
        if response.stop_reason != "tool_use":
            # 提取并输出文本内容
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text += block.text
            if final_text:
                print(final_text)
            return

        # 执行工具调用
        results = []
        used_todo = False
        manual_compress = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compress":
                    manual_compress = True
                # 处理 MCP 工具调用
                if block.name.startswith("mcp_"):
                    handler = TOOL_HANDLERS.get("mcp_call")
                    output = handler(tool_name=block.name, arguments=block.input)
                # 处理预配置 HTTP 接口调用
                elif block.name.startswith("http_"):
                    output = call_http_endpoint(block.name, block.input)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"未知工具：{block.name}"
                    except Exception as e:
                        output = f"错误：{e}"
                print(f"> {block.name}：{str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "TodoWrite":
                    used_todo = True

        # 待办提醒：连续 3 轮未更新待办时提醒模型
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            results.insert(0, {"type": "text", "text": PROMPTS.get("todo_reminder", "<reminder>请更新待办。</reminder>")})

        messages.append({"role": "user", "content": results})

        # 手动压缩
        if manual_compress:
            print("[手动压缩]")
            messages[:] = auto_compact(messages)


# ==================== REPL 入口 ====================

if __name__ == "__main__":
    history = []
    prompt_str = f"\033[36m{REPL_PROMPT}\033[0m"
    while True:
        try:
            query = input(prompt_str)
        except (EOFError, KeyboardInterrupt):
            break
        query = query.strip()
        if query.lower() in ("q", "exit", ""):
            break
        # REPL 快捷命令
        if query == "/compact":
            if history:
                print("[手动压缩]")
                history[:] = auto_compact(history)
            continue
        if query == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if query == "/team":
            print(TEAM.list_all())
            continue
        if query == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
