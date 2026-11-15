# MCP (Model Context Protocol) 集成文档

## 功能概述

NanoClaw 已集成 MCP 协议，可以加载外部 MCP Server 提供的工具，扩展 Agent 能力。

## 已完成的工作

### 1. MCP 工具模块 (`agent/tools/mcp.py`)
- `MCPClientManager`: 管理 MCP Server 连接生命周期
- `MCPTool`: 将 MCP Server 工具包装为 NanoClaw Tool 接口
- 工具命名格式: `{server_name}__{tool_name}`（避免跨 server 冲突）

### 2. 配置支持 (`config.py`)
- 新增 `mcp_servers` 字段，格式同 Claude Desktop
- 自动加载 MCP Server 配置

### 3. 主程序集成 (`main.py`)
- 启动时自动连接所有 MCP Server（超时 30s）
- 注册 MCP 工具到 ToolRegistry
- 退出时清理所有连接

### 4. 示例 MCP Server (`mcp_servers/poetry_server.py`)
- 古诗词查询服务（12 首经典诗词）
- 提供三个工具：
  - `search_poetry(keyword)` - 搜索包含关键词的诗词
  - `random_poetry()` - 随机推荐一首诗
  - `list_poets()` - 获取诗人列表

## 使用步骤

### 1. 安装 MCP SDK
```bash
pip install mcp
```

### 2. 配置 MCP Server
在 `config.json` 中添加 `mcp_servers` 配置：
```json
{
    "mcp_servers": {
        "poetry": {
            "command": "python",
            "args": ["mcp_servers/poetry_server.py"],
            "description": "古诗词查询服务"
        }
    }
}
```

### 3. 启动 NanoClaw
```bash
python main.py
```

启动时会看到：
```
[MCP] 开始连接 1 个 MCP Server...
[MCP] 正在连接 poetry...
[MCP]   - 加载工具: poetry__search_poetry (搜索包含指定关键词的古诗词...)
[MCP]   - 加载工具: poetry__random_poetry (随机返回一首古诗词...)
[MCP]   - 加载工具: poetry__list_poets (返回所有诗人列表...)
[MCP] poetry 连接成功，加载 3 个工具
[cli:local] 已注册 MCP 工具: 3 个
```

### 4. 使用 MCP 工具
在对话中可以直接调用：
```
你: 搜索包含"明月"的诗词

NanoClaw: [调用工具 poetry__search_poetry]
找到以下诗词：
《静夜思》—— 李白（唐）
床前明月光，疑是地上霜。举头望明月，低头思故乡。

《水调歌头·明月几时有》—— 苏轼（宋）
明月几时有？把酒问青天...
```

## 关键技术要点

### 1. ClientSession 必须手动 `__aenter__()`
```python
# 错误写法（会卡死）
session = ClientSession(read, write)
await session.initialize()  # 消息循环未启动，永远等待

# 正确写法
session = ClientSession(read, write)
await session.__aenter__()  # 启动内部消息循环
await session.initialize()  # 现在可以正常工作
```

### 2. 工具命名避免冲突
多个 MCP Server 可能提供同名工具，因此使用 `{server}__{tool}` 格式：
```
poetry__search_poetry
weather__search_location  # 同名但不同 server
```

### 3. 所有 print 不使用 emoji
Windows GBK 编码不支持 emoji，使用 `[MCP]`、`[!]` 等文本标记代替。

## 支持的传输协议

支持三种，配置里不写 `type` 也能按配置形状自动推断：

| 传输 | 配置形状 | 说明 |
|---|---|---|
| `stdio` | `command` + `args` | 本地子进程，最常见 |
| `http` | `url`（+ `headers` / `bearer_token`） | Streamable HTTP，远程服务 |
| `sse` | `url`（+ `headers` / `bearer_token`） | 老一代远程传输，仍有 Server 在用 |

推断规则：有 `url` 按 `http` 处理，有 `command` 按 `stdio` 处理；显式写 `"type"` 优先。

```jsonc
// 本地子进程
"poetry": { "command": "python", "args": ["mcp_servers/poetry_server.py"], "risk_level": "auto" }

// 远程服务（带鉴权）
"remote_kb": {
  "type": "http",
  "url": "https://example.com/mcp",
  "bearer_token": "${KB_TOKEN}",
  "risk_level": "confirm"
}
```

## 自愈能力

| 能力 | 行为 |
|---|---|
| 调用超时 | 单次 `call_tool` 有超时（默认 60s），不会把会话挂死 |
| 断线重连 | 调用失败时自动重建连接并重试一次（配置从 `mcp_config` 取，不从已清空的状态取） |
| 周期探活 | 启动后台健康检查（默认 60s），发现 down 自动重连 |
| 幂等关闭 | `shutdown()` 可重复调用，不会二次 `__aexit__` 报错 |
| 启动降级 | 单个 Server 连不上只跳过并打警告，不阻塞主链路 |

## 调试 MCP Server

使用 MCP Inspector 测试单独的 server：
```bash
npx @modelcontextprotocol/inspector python mcp_servers/poetry_server.py
```

## 注意事项

1. **超时处理**: 单个 server 连接超时 30s，失败会跳过并打印警告
2. **清理顺序**: 退出时必须**逆序**——先关 ClientSession，再关 transport 上下文；顺序反了会让 session 的消息循环对着已关闭的流报错
3. **工具去重**: 如果多个 server 提供同名工具，会以不同前缀注册
4. **错误处理**: 工具调用失败返回错误字符串，不会中断对话
5. **相对路径**: stdio 的 `args` 是相对路径时，必须给子进程指定 `cwd`（本项目用 config.json 所在目录），否则会按"启动时的当前目录"解析

## 参考文档

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [FastMCP 示例](https://github.com/modelcontextprotocol/python-sdk/tree/main/examples)