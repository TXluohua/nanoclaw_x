# NanoClaw

**一个能真正干活的 Agent 引擎**：多渠道接入、工具自主调度、写操作要人确认、全链路可追踪、支持流式输出。

不是"套一层 API 的聊天框"——它会在你的工作区里读文件、改代码、跑命令；
也正因为如此，它必须管住自己。

```
Channel × 4  ──→  MessageBus  ──→  Gateway  ──→  AgentLoop  ──→  Provider
CLI / Web / 飞书 / QQ   有界队列+背压   并发+会话锁   工具循环    流式/重试
                                          │
                            ┌─────────────┼─────────────┐
                            ▼             ▼             ▼
                      PermissionGate   ToolRegistry   Tracer/UsageMeter
                      三级权限+确认    9 类内置工具    trace + 成本 + 预算
                            │             │
                            ▼             ▼
                      审批落盘(可恢复)   MCP: stdio/http/sse
```

---

## 一分钟理解它解决了什么

| 问题 | 这里的做法 |
|---|---|
| Agent 会不会乱改我的文件？ | 三档权限：只读自动执行、写操作**必须人工确认**、危险命令直接拒 |
| 多个用户同时说话会卡吗？ | 网关按会话并发，同会话保序，超载时明确拒绝而不是内存爆炸 |
| 它到底干了什么？为什么慢？花了多少钱？ | 12 类事件逐条落盘的 trace + token/成本累计 + 预算护栏 |
| 用户要盯着空白等多久？ | 流式输出，CLI/Web 逐字推送，trace 里记录首字延迟 |
| 它怎么记住我？ | MEMORY.md 按行索引，每轮只召回相关的几条，不塞满上下文 |
| MCP 服务挂了怎么办？ | 断线自动重连 + 周期探活，不会"突然某个工具永远不可用" |

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置（复制模板，填自己的密钥）
cp .env.example .env          # Windows: Copy-Item .env.example .env

# 3. 启动
python main.py
```

启动后会同时拉起 4 个渠道：

```
  权限确认: 已启用（写操作需你确认）
[MCP] poetry 连接成功（stdio），加载 3 个工具
[启动] 已启用 Web 渠道: http://127.0.0.1:8080
cli:local 已发现技能: 3 个
[cli:local] 已注册工具：[9 个内置 + 3 个 MCP]
开始对话（/exit 退出，/cost 看花费，/trace 看链路，/tools 看工具）
```

浏览器打开 `http://127.0.0.1:8080` 是 Web 界面，终端里是 CLI —— 两者共用同一套引擎。

### 试一下人工确认

```
你> 在 workspace 下新建一个 hello.txt，写一句问候

NanoClaw:
  🛠️  工具: write_file({"file_path": "hello.txt", "content": "你好"})
  ⏸  write_file 待用户确认

⚠️ 以下操作会修改你的环境，需要你确认后才会执行：

  1. write_file({"file_path": "hello.txt", "content": "你好"})
     └ write_file 会修改系统状态

回复「确认」执行，回复「取消」放弃。（30 分钟内有效）

你> 确认
  ✅ 用户已确认，执行 1 个挂起操作
  ✅ [已确认] write_file 结果: 成功写入文件: hello.txt (2 字符)
```

---

## 功能清单

### 1. 三档权限与人工确认

| 等级 | 工具 | 行为 |
|---|---|---|
| `auto` | `read_file` `list_dir` `web_search` `web_fetch` `todo_write` `search_memory`、只读 MCP | 直接执行 |
| `confirm` | `write_file` `edit_file` `exec` `spawn_subagent`、未知 MCP | 挂起等确认 |
| `deny` | `rm -rf` / `mkfs` / `sudo` / `dd if=` / `curl\|bash` … | 直接拒绝 |

- 挂起状态落盘（`workspace/approvals/`），**进程重启不丢**，TTL 30 分钟
- 批准后**替换**占位消息而不是追加（追加会让 `tool_call_id` 重复 → 请求 400）
- 歧义回复一律作废，绝不擅自执行
- 子 Agent `interactive=False`：没人可问，`confirm` 级一律拒绝（堵住"spawn 绕过确认"的后门）

> 细节见 [`_功能说明_人工确认三档权限.md`](_功能说明_人工确认三档权限.md)

### 2. 全链路追踪 + 成本核算

- 12 类事件逐条 append 到 `workspace/traces/<日期>.jsonl`（崩溃最多丢最后一条）
- 每次模型调用记录：模型、耗时、token、成本、**首字延迟**
- 每次工具调用记录：耗时、成功与否、结果长度
- 单会话累计落盘（重启续算），`/cost` 随时看
- 预算护栏放在**发请求之前**（跑完再查是账单，不是护栏）

```
你> /trace
       0ms ▶ run_start
       2ms · llm_request
    4071ms ◆ llm_response   deepseek-v4-pro  4068ms  token 2111  tools=1
    4073ms ⚖ permission      list_dir → auto
    4076ms ✓ tool_result     list_dir  1ms  ok  内容已截断
   14778ms ⚖ permission      exec → confirm
   14779ms ⏸ pending_saved   1 个操作待确认
   14781ms ■ run_end         status=ok  模型6次  工具11次  token 22130
```

> 细节见 [`_功能说明_全链路追踪与成本核算.md`](_功能说明_全链路追踪与成本核算.md)

### 3. 流式输出

Provider 层 `chat_stream()`，工具调用的 `name` / `arguments` 分片到达时按 index 累积拼装。
CLI 逐字打印、Web 发 JSON 帧（`{"type":"delta"}` / `{"type":"final"}`）。
飞书/QQ 只收完整回复 —— 它们发一条就是一次 API 调用，逐字推送会被限流。

### 4. MCP 双传输与自愈

- `stdio`（本地子进程）/ `http`（Streamable HTTP）/ `sse`，配置里不写 `type` 也能按形状推断
- Bearer 鉴权、工具命名空间隔离（`{server}__{tool}`）、调用超时
- **调用失败自动重连一次** + 后台周期探活
- 幂等关闭（可重复调用）

### 5. 长期记忆检索

`MEMORY.md` 按行拆条 → BM25 索引 → 每轮只召回 top-k 条。
**不改文件格式**，所以历史记忆零迁移成本。装了 `jieba` 中文分词更好，没装自动退化成 2-gram。
预留 `embedder` 钩子，配置后可走 BM25 + 向量的 hybrid 融合。

### 6. 并发网关

- 入站消息按会话起独立 task：**跨会话并行、同会话串行保序**
- 全局信号量限制同时处理的会话数
- Agent 实例 LRU 淘汰（原实现只增不减）
- 有界队列 + 背压：满了一分钟就明确拒绝，而不是内存涨到 OOM

### 7. 工具

| 工具 | 说明 | 等级 |
|---|---|---|
| `read_file` / `write_file` / `list_dir` | 文件操作，沙箱限制在工作区内 | auto / confirm / auto |
| `edit_file` | **片段替换**（比整篇重写省 token、不会改坏其它部分） | confirm |
| `exec` | Shell 执行，17 条危险命令正则拦截 | confirm |
| `web_search` / `web_fetch` | 联网检索与抓取 | auto |
| `todo_write` | 长任务白板，按会话落盘 | auto |
| `search_memory` | 主动检索长期记忆 | auto |
| `spawn_subagent` | 子 Agent 递归，最大深度 2，可指定便宜模型 | confirm |

---

## 配置

全部在 `config.json`（可提交）+ `.env`（★ 密钥只放这里）。

```jsonc
{
  "permissions": { "enabled": true, "pending_ttl_seconds": 1800 },
  "streaming":   { "enabled": true },
  "tracing":     { "enabled": true },
  "memory":      { "enabled": true, "top_k": 5 },
  "pricing":     { "deepseek-chat": { "input": 0.0, "output": 0.0 } },  // USD/百万token
  "budget":      { "session_tokens": 0, "session_cost_usd": 0.0 },       // 0 = 不限
  "bus_maxsize": 1000,
  "mcp_servers": {
    "poetry": {
      "type": "stdio",
      "command": "python",
      "args": ["mcp_servers/poetry_server.py"],
      "risk_level": "auto"
    }
  }
}
```

> `pricing` 故意留空：各家价格会变，**内置一个过期数字比不写更误导人**。
> 留空时只统计 token 并明确显示"未配置价目表"。

---

## 测试

```bash
python -m pytest            # 92 个测试，约 46 秒，全程离线
```

测试**永不联网**：所有 LLM 交互走 `StubProvider`（脚本化返回），
所以任何机器上都能跑，也不会产生 API 费用。

其中 33 个是运维值班专项改造的测试（`tests/test_ops_agent.py`），
重点守三条容易被"刷漂亮"的指标：陷阱引用的工具必须真实存在、
只读工具必须是 auto、危险动作必须被拦下。

另外有 6 个可读性更好的集成脚本（`pytest` 会通过 `tests/test_scripts.py` 一并跑）：

```bash
python _verify_fixes.py         # 33 项：历史缺陷回归
python _verify_boot.py          # 57 项：装配链路
python _verify_permissions.py   # 80 项：权限与人工确认
python _verify_tracing.py       # 75 项：trace 与成本
python _verify_streaming.py     # 30 项：流式解析与渠道
python _verify_mcp_runtime.py   # 22 项：MCP 真连接/重连/探活
```

合计 **297 条断言**。

---

## 专项化改造：运维值班处置 Agent

通用 Agent 框架市面上一抓一大把，缺的不是能力而是**场景约束**。
`ops_agent/` 把这个引擎改造为**敢接生产告警**的值班助手：

> 告警进飞书 → 按真实 runbook 分步诊断 → 高风险动作强制人工签核 → 全程留痕可复盘

- **数据底座真实**：129 条 kube-prometheus 生产告警 + 97 条 prometheus-operator
  官方 runbook 诊断/处置步骤（Apache-2.0）+ LogHub 真实系统日志；
- **30 条企业值班场景**，预埋 **70 个危险动作陷阱**；
- **领域工具包**（20 个工具，每个声明风险等级），接进原有三档权限闸门，
  挂起状态复用 `workspace/approvals/` 落盘；
- **离线回放评测**：越权拦截率 **70/70 = 100%**，诊断 133/133 步零误拦，
  需人工确认比例 2.89%；
- **复盘报告**：trace 直接渲染成中文值班时间线。

```bash
python ops_agent/replay.py                    # 30 条场景离线回放
python ops_agent/report.py --id OPS-INC-0017  # 单条事故复盘
```

详见 [`ops_agent/README.md`](ops_agent/README.md)。

---

## Docker

```bash
docker compose up -d --build
# 打开 http://127.0.0.1:8080
```

- 非 root 运行（UID 10001）
- 密钥运行时注入，不烤进镜像
- `workspace/` 挂载出来持久化会话/记忆/审批/trace
- 资源上限 2 CPU / 2G（Agent 跑 Shell 容易失控）

---

## 目录结构

```
nanoclaw/
├── main.py                    # 入口：装配组件、启动渠道
├── config.py                  # 配置加载（env > .env > config.json > 默认）
├── gateway.py                 # 网关：并发分发、会话锁、LRU、流式 sink
├── agent/
│   ├── loop.py                # Agent 主循环（防爆/权限/追踪/流式 都在这里汇合）
│   ├── context.py             # System Prompt 组装 + 记忆注入
│   ├── memory_store.py        # 长期记忆 BM25 检索
│   ├── memory.py              # 上下文压缩（token 预算→摘要）
│   ├── permissions.py         # 三档权限 + 挂起存储 + 回复判定
│   ├── skills.py              # SKILL.md 按需加载
│   ├── tracing.py             # trace + 成本 + 用量
│   └── tools/                 # 工具实现 + registry（超时兜底）
├── providers/
│   ├── base.py                # Provider 抽象（含 chat_stream 默认退化）
│   └── openai_compat.py       # 流式解析、错误分层、退避重试
├── bus/queue.py               # 有界消息总线 + 背压
├── channels/                  # CLI / Web / 飞书 / QQ 适配器
├── session/manager.py         # 会话持久化（可逆编码 + 消息替换）
├── mcp_servers/poetry_server.py  # 示例 MCP Server
├── skills/                    # SKILL.md 技能
├── tests/                     # pytest（离线）
├── workspace/                 # 运行期数据（git 忽略）
│   ├── sessions/ memory/ approvals/ traces/ todos/
├── Dockerfile / docker-compose.yml / .dockerignore
├── pyproject.toml             # pytest + ruff + mypy 配置
└── _verify_*.py               # 集成验证脚本
```

---

## 局限与未验证

**这一节不是免责声明，是专业性证明。** 明确知道边界在哪，比宣称完备更可靠。

已证明：
- 公开场景下的工具调度、权限分级、人工确认流程正确
- 熔断、沙箱越界、会话编码、队列背压等缺陷已修复并有回归测试
- trace/成本统计准确（对 StubProvider 的确定性输出）
- MCP stdio 真实连接、断线重连、探活可用

**未证明 / 已知不足**：
1. **正则黑名单不是安全边界**。`rm  -fr`、变量拼接、`python -c "..."` 都能绕过。
   真正的隔离要靠容器 / 只读挂载 / 低权限用户 / seccomp —— 本项目只做了流程层防护。
2. **没有可视化 trace 界面**，只有 CLI 时间线和 JSONL 文件。
3. **trace 文件无限增长**，没有轮转与清理。
4. **成本是估算值**，只按 token × 价目表算，不含缓存命中折扣、阶梯价。
5. **没有分布式追踪**：子 Agent 的 trace 与主 Agent 混在同一文件，靠 trace_id 区分，无父子 span。
6. **未做真实 IM 压力测试**（飞书/QQ 并发、限流、消息乱序）。
7. **记忆召回是词汇级（BM25）**，不是真正语义：用户说"车"、记忆里写"汽车"召不回。
8. **Web 渠道无鉴权**，默认只绑 `127.0.0.1`。绑 `0.0.0.0` 前必须自己加 token 校验。

---

## 许可

个人学习项目，仅供学习与面试展示。
