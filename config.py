"""
NanoClaw 配置模块

提供配置加载和管理功能，支持：
- 从 JSON 文件读取配置
- 从 .env 文件读取配置（零依赖，推荐用来放密钥）
- 环境变量覆盖（优先级最高）

优先级：环境变量 > .env 文件 > config.json > 代码默认值

配置文件使用 config.json，包含模型、工作区等参数；
**API 密钥、飞书/QQ 密钥建议只放在 .env 或环境变量里，不要写进 config.json**。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """
    把 .env 文件里的键值对读进 os.environ（零依赖实现，不覆盖已存在的环境变量）。

    Args:
        path: .env 文件路径；不存在则静默返回
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[配置] 警告：.env 读取失败（{e}），已忽略")
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        # setdefault：真实环境变量优先，.env 只做兜底
        os.environ.setdefault(key, value)


_MODULE_DIR = Path(__file__).resolve().parent
_ENV_FILE = _MODULE_DIR / ".env"
_load_dotenv(_ENV_FILE)


def _resolve_config_path(config_path: str) -> "tuple[Path | None, Path]":
    """
    解析 config.json 的实际位置。

    返回 (找到的文件路径或 None, 解析 workspace 用的基准目录)。

    ★ 为什么需要这个函数（真实踩过的坑）：
      .env 是用 __file__ 定位的 —— 跟"当前工作目录"无关，所以在哪启动都能读到。
      但 config.json 原先写的是 os.path.isfile("config.json")，这是相对
      【当前工作目录】的。于是从别的目录启动 main.py 时：

          PS D:\\huaqing-work\\python\\nanoclaw> python nanoclaw/nanoclaw/main.py

      config.json 读不到，而且**一声不吭**（没有 file-isfile 的报错，只是静默跳过）。
      后果是四处同时失效，但表面看起来程序是正常启动的：
        1) 飞书 / QQ 渠道不启动 —— 打印"未配置"，可你明明填了
        2) MCP Server 全部消失 —— 工具列表里少了 poetry 的 3 个工具
        3) workspace 变成父目录 —— 沙箱边界、人设、skills、MEMORY.md 全部错位
        4) 而 .env 里的 api_key / base_url / web 照常生效
      → 于是"看起来一切正常，只是密钥像是没配"。这是最难查的一类 bug。

      修法：先看当前工作目录，再看脚本所在目录；并把最终用的是哪一个打印出来。
    """
    p = Path(config_path)
    if p.is_absolute():
        return (p if p.is_file() else None), (p.parent if p.is_file() else _MODULE_DIR)

    candidates = [Path.cwd() / p, _MODULE_DIR / p]
    for cand in candidates:
        if cand.is_file():
            return cand, cand.parent

    return None, _MODULE_DIR


@dataclass
class NanoClawConfig:
    """
    NanoClaw 配置结构

    Attributes:
        api_key: API 密钥（建议放 .env / 环境变量，勿写进 config.json）
        base_url: API 基础 URL
        model: 主模型名（会被 models["main"] 或环境变量覆盖）
        models: 多档模型 {"main": ..., "subagent": ..., "cheap": ...}
        workspace: 工作区路径
        max_iterations: Agent 最大迭代次数
        identity_file: 人设文件名，相对于 workspace
        feishu_app_id / feishu_app_secret: 飞书应用凭据（可选）
        qq_app_id / qq_app_secret: QQ 机器人凭据（可选）
        web_enabled / web_host / web_port: Web 渠道配置
        mcp_servers: MCP Server 配置字典
    """

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = ""                     # ★ 显式声明，避免缺字段时 AttributeError
    models: dict = field(default_factory=dict)
    workspace: str = "."
    max_iterations: int = 32
    identity_file: str = "identity.md"
    # 飞书配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # QQ 配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    # Web 配置
    web_enabled: bool = True
    web_host: str = "127.0.0.1"         # ★ 默认只监听本机，避免局域网裸奔
    web_port: int = 8080
    # MCP Server 配置
    mcp_servers: dict = field(default_factory=dict)
    # 权限与人工确认（★ 默认开启。关掉它等于让 Agent 自由执行写操作 / Shell 命令）
    permissions_enabled: bool = True
    permissions_pending_ttl: int = 1800  # 待确认操作的有效期（秒）
    # 可观测性：全链路 trace
    trace_enabled: bool = True
    # 长期记忆检索（关闭则退回"整篇 MEMORY.md 注入"）
    memory_enabled: bool = True
    memory_top_k: int = 5
    # 消息总线队列容量（背压阈值）
    bus_maxsize: int = 1000
    # 流式输出（只有声明 supports_streaming 的渠道会收到增量）
    streaming_enabled: bool = True
    # 模型价目表（USD / 每百万 token）。留空则只统计 token、不算钱
    pricing: dict = field(default_factory=dict)
    # 预算护栏（0 或缺失表示不限制）
    budget: dict = field(default_factory=dict)
    # config.json 所在目录（绝对路径）。MCP Server 的相对路径以它为基准
    config_dir: str = ""


# 环境变量名 → 配置字段名
_ENV_FIELD_MAP = {
    "NANOCLAW_API_KEY": "api_key",
    "NANOCLAW_BASE_URL": "base_url",
    "NANOCLAW_MODEL": "model",
    "NANOCLAW_WORKSPACE": "workspace",
    "NANOCLAW_FEISHU_APP_ID": "feishu_app_id",
    "NANOCLAW_FEISHU_APP_SECRET": "feishu_app_secret",
    "NANOCLAW_QQ_APP_ID": "qq_app_id",
    "NANOCLAW_QQ_APP_SECRET": "qq_app_secret",
}


def load_config(config_path: str = "config.json") -> NanoClawConfig:
    """
    加载配置

    读取顺序与优先级：环境变量 > .env > config.json > 默认值。

    Args:
        config_path: 配置文件路径，默认 config.json

    Returns:
        NanoClawConfig: 配置对象
    """
    config = NanoClawConfig()
    json_model = ""      # config.json 里是否显式写了 model 字段

    # ---- 0. 定位配置文件（跟工作目录无关）----
    resolved_path, config_dir = _resolve_config_path(config_path)

    # ---- 1. 从 JSON 文件读取（如果存在）----
    if resolved_path is not None:
        print(f"[配置] 配置文件: {resolved_path}")
        data = None
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            # ★ 不再静默吞掉：报错要能直接定位到行列
            print(
                f"[配置] 错误：{resolved_path} 不是合法 JSON"
                f"（第 {e.lineno} 行第 {e.colno} 列：{e.msg}）。"
                f"\n       常见原因：多/少逗号、用了单引号、结尾多了逗号。已改用默认配置。"
            )
        except Exception as e:
            print(f"[配置] 错误：读取 {resolved_path} 失败（{e}）。已改用默认配置。")

        if isinstance(data, dict):
            # 逐字段读取（只覆盖 JSON 里出现的字段）
            for key in ("api_key", "base_url", "workspace", "identity_file"):
                if key in data:
                    setattr(config, key, data[key])
            if "max_iterations" in data:
                config.max_iterations = data["max_iterations"]

            # 多模型配置
            if isinstance(data.get("models"), dict):
                config.models = dict(data["models"])
            # model 字段：显式给出优先；否则用 models.main
            if data.get("model"):
                config.model = data["model"]
                json_model = data["model"]
            elif config.models.get("main"):
                config.model = config.models["main"]

            # 飞书 / QQ / Web / MCP
            feishu = data.get("feishu") or {}
            config.feishu_app_id = feishu.get("app_id", config.feishu_app_id)
            config.feishu_app_secret = feishu.get("app_secret", config.feishu_app_secret)
            qq = data.get("qq") or {}
            config.qq_app_id = qq.get("app_id", config.qq_app_id)
            config.qq_app_secret = qq.get("app_secret", config.qq_app_secret)
            web = data.get("web") or {}
            if "enabled" in web:
                config.web_enabled = bool(web["enabled"])
            if web.get("host"):
                config.web_host = web["host"]
            if web.get("port"):
                config.web_port = int(web["port"])
            if isinstance(data.get("mcp_servers"), dict):
                config.mcp_servers = dict(data["mcp_servers"])

            # 权限与人工确认
            perms = data.get("permissions") or {}
            if isinstance(perms, dict):
                if "enabled" in perms:
                    config.permissions_enabled = bool(perms["enabled"])
                if "pending_ttl_seconds" in perms:
                    try:
                        config.permissions_pending_ttl = int(
                            perms["pending_ttl_seconds"]
                        )
                    except (TypeError, ValueError):
                        print("[配置] 警告：permissions.pending_ttl_seconds 不是数字，已忽略")

            # 可观测性与成本
            tracing = data.get("tracing") or {}
            if isinstance(tracing, dict) and "enabled" in tracing:
                config.trace_enabled = bool(tracing["enabled"])

            streaming = data.get("streaming") or {}
            if isinstance(streaming, dict) and "enabled" in streaming:
                config.streaming_enabled = bool(streaming["enabled"])

            if isinstance(data.get("pricing"), dict):
                config.pricing = dict(data["pricing"])

            if isinstance(data.get("budget"), dict):
                config.budget = dict(data["budget"])

            # 长期记忆检索
            memory_cfg = data.get("memory") or {}
            if isinstance(memory_cfg, dict):
                if "enabled" in memory_cfg:
                    config.memory_enabled = bool(memory_cfg["enabled"])
                if "top_k" in memory_cfg:
                    try:
                        config.memory_top_k = int(memory_cfg["top_k"])
                    except (TypeError, ValueError):
                        print("[配置] 警告：memory.top_k 不是数字，已忽略")

            # 消息总线队列容量（背压阈值）
            if "bus_maxsize" in data:
                try:
                    config.bus_maxsize = int(data["bus_maxsize"])
                except (TypeError, ValueError):
                    print("[配置] 警告：bus_maxsize 不是数字，已忽略")

    # ---- 2. 环境变量覆盖（含 .env 载入的值），优先级最高 ----
    for env_name, attr in _ENV_FIELD_MAP.items():
        value = os.environ.get(env_name)
        if value:
            setattr(config, attr, value)

    # 模型档位分别覆盖：NANOCLAW_MODEL_MAIN / _SUBAGENT / _CHEAP
    for tier in ("main", "subagent", "cheap"):
        value = os.environ.get(f"NANOCLAW_MODEL_{tier.upper()}")
        if value:
            config.models[tier] = value

    # Web 监听地址 / 端口
    if os.environ.get("NANOCLAW_WEB_HOST"):
        config.web_host = os.environ["NANOCLAW_WEB_HOST"]
    if os.environ.get("NANOCLAW_WEB_PORT"):
        try:
            config.web_port = int(os.environ["NANOCLAW_WEB_PORT"])
        except ValueError:
            print("[配置] 警告：NANOCLAW_WEB_PORT 不是数字，已忽略")

    # 权限开关（只认 false/0/no/off 为关闭，避免手滑把它关掉）
    perm_env = os.environ.get("NANOCLAW_PERMISSIONS_ENABLED")
    if perm_env is not None:
        config.permissions_enabled = perm_env.strip().lower() not in (
            "false",
            "0",
            "no",
            "off",
        )

    # ---- 3. 收尾：保证 model 与 models["main"] 一定一致 ----
    # 只有在用户【没有显式指定单模型】时（没用 NANOCLAW_MODEL，config.json 也没写 model 字段），
    # 才让主模型跟随 models["main"]（因为 main 可能刚被 NANOCLAW_MODEL_MAIN 覆盖过）。
    if not os.environ.get("NANOCLAW_MODEL") and not json_model:
        if config.models.get("main"):
            config.model = config.models["main"]
    # 兜底：两个都空时给一个默认值
    if not config.model:
        config.model = config.models.get("main") or "deepseek-v4-pro"
    # 反向同步：确保 models["main"] == model（避免两处不一致导致"以为换了其实没换"）
    config.models["main"] = config.model
    for tier in ("subagent", "cheap"):
        config.models.setdefault(tier, config.model)

    # ---- 4. workspace 解析成绝对路径 ----
    # ★ 关键：相对路径按【配置文件所在目录】解析，而不是按"当前工作目录"。
    #   否则 `workspace: "."` 会随你在哪个目录敲命令而变 ——
    #   而 workspace 是文件读写工具的**沙箱根目录**（见 tools/filesystem.py），
    #   同时也是 identity.md / skills / MEMORY.md / sessions 的查找起点。
    #   它一飘，人设、技能、记忆会全部找不到，且不报错。
    #   绝对路径不受影响：os.path.join 遇到绝对路径会直接返回它。
    if config.workspace:
        config.workspace = os.path.abspath(os.path.join(str(config_dir), config.workspace))
    else:
        config.workspace = str(config_dir)

    # config.json 所在目录也带上，MCP Server 的相对路径要用它当基准
    config.config_dir = str(config_dir)

    return config
