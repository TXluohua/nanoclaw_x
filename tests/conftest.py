"""pytest 公共夹具

设计原则：
1. **测试永不联网**。所有 LLM 交互都走 StubProvider（脚本化返回），
   所以整个测试集可以在任何机器上离线跑，也不会产生 API 费用。
2. **每个测试用自己的临时目录**（pytest 的 tmp_path），互不污染。
3. **不 mock 掉被测逻辑**：工具、权限、追踪、流式这些都跑真实实现，
   只把"最外层的外部世界"（模型 API）替换掉。mock 太多等于测试自己的假设。
"""

import os
import sys

import pytest

# 让测试能 import 项目模块（nanoclaw/ 目录本身）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from providers.base import LLMResponse, ToolCallRequest  # noqa: E402


class StubProvider:
    """脚本化 Provider。

    - `chat()` 按顺序吐出预设的 LLMResponse
    - `chat_stream()` 把内容拆成两段回调，用来模拟流式
    - 脚本用尽后返回一句默认回答，避免测试因为少准备一条响应就挂
    """

    def __init__(self, scripted=None, model="stub-model"):
        self.scripted = list(scripted or [])
        self.calls = []
        self.model = model
        self.stream_calls = 0

    async def chat(self, messages, tools=None, model=None):
        self.calls.append(list(messages))
        if self.scripted:
            return self.scripted.pop(0)
        return LLMResponse(content="（默认回答）", tool_calls=[], finish_reason="stop")

    async def chat_stream(self, messages, tools=None, model=None, on_delta=None):
        self.stream_calls += 1
        response = await self.chat(messages, tools, model)
        if on_delta is not None and response.content:
            text = response.content
            middle = max(1, len(text) // 2)
            await on_delta(text[:middle])
            await on_delta(text[middle:])
        return response


def final_response(text: str, usage=None) -> LLMResponse:
    """一轮正常结束的响应。"""
    return LLMResponse(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def tool_call_response(
    tool_name: str, arguments: dict, call_id: str = "call_1", usage=None
) -> LLMResponse:
    """模型决定调用工具的响应。"""
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name=tool_name,
                arguments=arguments,
                reasoning_content=None,
            )
        ],
        finish_reason="tool_calls",
        usage=usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


@pytest.fixture
def stub_provider():
    """直接给测试用的类（需要自行实例化）。"""
    return StubProvider


@pytest.fixture
def make_agent(tmp_path):
    """Agent 工厂夹具。

    用法：
        agent, workspace, provider = make_agent([tool_call_response(...), final_response("好")])
    """

    def _make(
        scripted=None,
        *,
        interactive=True,
        streaming=False,
        permissions=True,
        trace=True,
        budget=None,
        pricing=None,
        max_iterations=8,
        session_key="test:user",
        tool_names=("read_file", "write_file", "list_dir", "exec"),
    ):
        from agent.context import ContextBuilder
        from agent.loop import AgentLoop
        from agent.permissions import ApprovalStore, PermissionGate
        from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
        from agent.tools.registry import ToolRegistry
        from agent.tools.shell import ExecTool
        from agent.tracing import CostEstimator, Tracer, UsageMeter
        from session.manager import SessionManager

        workspace = os.path.join(tmp_path, "ws")
        os.makedirs(workspace, exist_ok=True)

        tools = ToolRegistry(default_timeout=5)
        tool_map = {
            "read_file": lambda: ReadFileTool(workspace),
            "write_file": lambda: WriteFileTool(workspace),
            "list_dir": lambda: ListDirTool(workspace),
            "exec": lambda: ExecTool(workspace),
        }
        for name in tool_names:
            tools.register(tool_map[name]())

        provider = StubProvider(scripted)
        estimator = CostEstimator(pricing or {})

        gate = None
        if permissions:
            gate = PermissionGate(
                enabled=True,
                store=ApprovalStore(base_dir=os.path.join(workspace, "approvals")),
            )

        tracer = None
        meter = None
        if trace:
            tracer = Tracer(
                base_dir=os.path.join(workspace, "traces"),
                session_key=session_key,
                estimator=estimator,
            )
            meter = UsageMeter(
                path=os.path.join(workspace, "traces", "sessions", "u.json"),
                estimator=estimator,
                session_key=session_key,
            )

        agent = AgentLoop(
            provider=provider,
            tools=tools,
            context=ContextBuilder(workspace=workspace, identity_file=""),
            session_manager=SessionManager(
                sessions_dir=os.path.join(workspace, "sessions")
            ),
            model="stub-model",
            max_iterations=max_iterations,
            session_key=session_key,
            permission_gate=gate,
            interactive=interactive,
            tracer=tracer,
            usage_meter=meter,
            budget=budget or {},
            streaming=streaming,
        )
        return agent, workspace, provider

    return _make
