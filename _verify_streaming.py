"""流式输出验证（全离线，不调真实模型）

用法： cd nanoclaw/nanoclaw && python _verify_streaming.py
"""

import asyncio
import json
import os
import sys
import tempfile

from providers.base import LLMResponse, ToolCallRequest

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name}  {detail}")


# ------------------------------------------------------------- 假 chunk
class Obj:
    """鸭子类型的对象，用来模拟 openai 的响应 chunk。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def content_chunk(text):
    return Obj(
        choices=[
            Obj(
                delta=Obj(content=text, tool_calls=None, reasoning_content=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def tool_chunk(index, call_id, name, arguments):
    return Obj(
        choices=[
            Obj(
                delta=Obj(
                    content=None,
                    tool_calls=[
                        Obj(
                            index=index,
                            id=call_id,
                            function=Obj(name=name, arguments=arguments),
                        )
                    ],
                    reasoning_content=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def finish_chunk(reason="stop", usage=None):
    return Obj(
        choices=[Obj(delta=Obj(content=None, tool_calls=None), finish_reason=reason)],
        usage=usage,
    )


class FakeStream:
    """模拟 openai 的 AsyncStream：可迭代、有 close()、有 response。"""

    def __init__(self, chunks, raise_after=None, boom=None):
        self.chunks = chunks
        self.raise_after = raise_after
        self.boom = boom
        self.closed = False
        self.response_closed = False
        self.response = self

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for index, chunk in enumerate(self.chunks):
            if self.raise_after is not None and index == self.raise_after:
                raise self.boom
            yield chunk
        if self.raise_after is not None and self.raise_after >= len(self.chunks):
            raise self.boom

    async def close(self):
        self.closed = True

    async def aclose(self):
        self.response_closed = True


class FakeClientWithStream:
    """返回 FakeStream 的假客户端，可配置前 N 次调用抛错。"""

    def __init__(self, make_stream, fail_times=0, boom=None):
        self.make_stream = make_stream
        self.fail_times = fail_times
        self.boom = boom
        self.calls = 0
        self.streams = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls += 1
                if outer.calls <= outer.fail_times:
                    raise outer.boom
                stream = outer.make_stream()
                outer.streams.append(stream)
                return stream

        self.chat = Obj(completions=_Completions())


class FakeClient:
    """假的 AsyncOpenAI 客户端：按预设吐出 chunk。"""

    def __init__(self, chunks, fail_first_with=None):
        self.chunks = chunks
        self.fail_first_with = fail_first_with
        self.calls = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.fail_first_with and len(outer.calls) == 1:
                    raise RuntimeError(outer.fail_first_with)

                async def gen():
                    for chunk in outer.chunks:
                        yield chunk

                return gen()

        self.chat = Obj(completions=_Completions())


# ------------------------------------------------- 1. 流式解析
def test_provider_stream():
    print("\n=== 1. Provider 流式解析（含 tool_calls 分片）===")
    from providers.openai_compat import OpenAICompatProvider

    chunks = [
        content_chunk("你好"),
        content_chunk("，我是"),
        content_chunk("NanoClaw。"),
        finish_chunk("stop", usage=Obj(prompt_tokens=10, completion_tokens=6, total_tokens=16)),
    ]

    provider = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
    provider.client = FakeClient(chunks)

    received = []

    async def on_delta(text):
        received.append(text)

    resp = asyncio.run(
        provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}], on_delta=on_delta
        )
    )

    check("三段落增量都被回调", received == ["你好", "，我是", "NanoClaw。"], str(received))
    check("最终内容拼接正确", resp.content == "你好，我是NanoClaw。", repr(resp.content))
    check("finish_reason 正确", resp.finish_reason == "stop", resp.finish_reason)
    check("usage 被解析", resp.usage.get("total_tokens") == 16, str(resp.usage))
    check("请求带了 stream=True", provider.client.calls[0].get("stream") is True)

    print("\n=== 2. tool_calls 分片拼装（最容易写错的地方）===")
    # 模拟真实情况：name 和 arguments 都是分片到达的
    chunks2 = [
        tool_chunk(0, "call_1", "read_f", '{"file_'),
        tool_chunk(0, None, "ile", 'path": "a.py"}'),
        tool_chunk(1, "call_2", "list_dir", '{"dir_path"'),
        tool_chunk(1, None, "", ': "."}'),
        finish_chunk("tool_calls"),
    ]
    provider2 = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
    provider2.client = FakeClient(chunks2)

    resp2 = asyncio.run(provider2.chat_stream([{"role": "user", "content": "hi"}]))

    check("解析出 2 个 tool_call", len(resp2.tool_calls) == 2, str(len(resp2.tool_calls)))
    if len(resp2.tool_calls) == 2:
        first, second = resp2.tool_calls
        check("分片的函数名被拼全（read_f + ile）", first.name == "read_file", first.name)
        check(
            "分片的 arguments 被拼成合法 JSON",
            first.arguments == {"file_path": "a.py"},
            str(first.arguments),
        )
        check("id 保留", first.id == "call_1", first.id)
        check("第二个工具名正确", second.name == "list_dir", second.name)
        check("第二个参数正确", second.arguments == {"dir_path": "."}, str(second.arguments))
    check("finish_reason 为 tool_calls", resp2.finish_reason == "tool_calls")

    print("\n=== 3. 服务端不支持 stream_options 时自动退化 ===")
    provider3 = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
    provider3.client = FakeClient(
        [content_chunk("ok"), finish_chunk()],
        fail_first_with="Unrecognized request argument supplied: stream_options",
    )
    resp3 = asyncio.run(provider3.chat_stream([{"role": "user", "content": "hi"}]))
    check("退化后仍拿到内容", resp3.content == "ok", repr(resp3.content))
    check("确实发了两次请求", len(provider3.client.calls) == 2, str(len(provider3.client.calls)))
    check(
        "第二次请求去掉了 stream_options",
        "stream_options" not in provider3.client.calls[1],
        str(provider3.client.calls[1].keys()),
    )


# ------------------------------------------------- 2. AgentLoop 流式
class StreamingStubProvider:
    """支持流式的桩 Provider。"""

    def __init__(self, deltas, final_content, tool_calls=None):
        self.deltas = deltas
        self.final_content = final_content
        self.tool_calls = tool_calls or []
        self.model = "stub"
        self.stream_called = 0
        self.chat_called = 0

    async def chat(self, messages, tools=None, model=None):
        self.chat_called += 1
        return LLMResponse(
            content=self.final_content,
            tool_calls=self.tool_calls,
            finish_reason="tool_calls" if self.tool_calls else "stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def chat_stream(self, messages, tools=None, model=None, on_delta=None):
        self.stream_called += 1
        for text in self.deltas:
            if on_delta:
                await on_delta(text)
        return LLMResponse(
            content=self.final_content,
            tool_calls=self.tool_calls,
            finish_reason="tool_calls" if self.tool_calls else "stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


def build_agent(tmpdir, provider, streaming=True):
    from agent.context import ContextBuilder
    from agent.loop import AgentLoop
    from agent.permissions import ApprovalStore, PermissionGate
    from agent.tools.filesystem import ListDirTool, ReadFileTool
    from agent.tools.registry import ToolRegistry
    from agent.tracing import CostEstimator, Tracer, UsageMeter
    from session.manager import SessionManager

    workspace = os.path.join(tmpdir, "ws")
    os.makedirs(workspace, exist_ok=True)

    tools = ToolRegistry(default_timeout=5)
    tools.register(ReadFileTool(workspace))
    tools.register(ListDirTool(workspace))

    estimator = CostEstimator()
    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=ContextBuilder(workspace=workspace, identity_file=""),
        session_manager=SessionManager(sessions_dir=os.path.join(workspace, "sessions")),
        model="stub",
        max_iterations=6,
        session_key="test:user",
        permission_gate=PermissionGate(
            enabled=True,
            store=ApprovalStore(base_dir=os.path.join(workspace, "approvals")),
        ),
        tracer=Tracer(
            base_dir=os.path.join(workspace, "traces"), session_key="test:user"
        ),
        usage_meter=UsageMeter(
            path=os.path.join(workspace, "traces", "sessions", "s.json"),
            estimator=estimator,
        ),
        streaming=streaming,
    )
    return agent, workspace


def test_loop_streaming():
    print("\n=== 4. AgentLoop 把增量转给 sink ===")
    tmp = tempfile.mkdtemp(prefix="ncstream_")
    provider = StreamingStubProvider(["第一段", "第二段", "第三段"], "第一段第二段第三段")
    agent, _ = build_agent(tmp, provider)

    got = []

    async def sink(text):
        got.append(text)

    agent.set_delta_sink(sink)
    answer = asyncio.run(agent.run("说点什么"))

    check("走了 chat_stream 而不是 chat", provider.stream_called == 1 and provider.chat_called == 0,
          f"stream={provider.stream_called} chat={provider.chat_called}")
    check("三段增量都推到了 sink", got == ["第一段", "第二段", "第三段"], str(got))
    check("最终答案完整", answer == "第一段第二段第三段", repr(answer))

    # trace 应记录首字延迟
    events = agent.tracer.last_run_timeline()
    llm_events = [e for e in events if e["kind"] == "llm_response"]
    check("trace 记录了 first_delta_ms", llm_events and llm_events[0].get("first_delta_ms") is not None,
          str(llm_events[0] if llm_events else None))

    print("\n=== 5. 关闭流式 / 无 sink 时退回非流式 ===")
    tmp2 = tempfile.mkdtemp(prefix="ncstream2_")
    provider2 = StreamingStubProvider(["a"], "a")
    agent2, _ = build_agent(tmp2, provider2, streaming=False)
    agent2.set_delta_sink(lambda t: None)
    asyncio.run(agent2.run("hi"))
    check("streaming=False 时走 chat()", provider2.chat_called == 1, str(provider2.chat_called))

    tmp3 = tempfile.mkdtemp(prefix="ncstream3_")
    provider3 = StreamingStubProvider(["a"], "a")
    agent3, _ = build_agent(tmp3, provider3, streaming=True)
    # 不装 sink
    asyncio.run(agent3.run("hi"))
    check("没装 sink 时走 chat()", provider3.chat_called == 1, str(provider3.chat_called))


# ------------------------------------------------- 3. 渠道行为
def test_channels():
    print("\n=== 6. 渠道能力与增量渲染 ===")
    from bus.queue import MessageBus, OutboundMessage
    from channels.cli import CLIChannel
    from channels.base import Channel
    from channels.qq import QQChannel  # noqa: F401  仅为确认可用
    from channels.feishu import FeishuChannel  # noqa: F401

    bus = MessageBus()
    cli = CLIChannel(bus)
    check("CLI 声明支持流式", cli.supports_streaming is True)
    check("基类默认不支持流式", Channel.supports_streaming is False)

    # CLI 收到 delta 应逐段打印且不换行
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        asyncio.run(cli.send(OutboundMessage(channel="cli", chat_id="c", content="你好", kind="delta")))
        asyncio.run(cli.send(OutboundMessage(channel="cli", chat_id="c", content="世界", kind="delta")))
        asyncio.run(cli.send(OutboundMessage(channel="cli", chat_id="c", content="你好世界", kind="final")))
    out = buf.getvalue()
    check("CLI 增量被逐段打印", "你好世界" in out, repr(out))
    check("CLI final 没有重复打印全文", out.count("你好世界") == 1, repr(out))

    print("\n=== 7. Web 渠道发 JSON 帧 ===")
    from channels.web import WebChannel

    web = WebChannel(bus)
    check("Web 声明支持流式", web.supports_streaming is True)

    sent = []

    class FakeWS:
        async def send_text(self, text):
            sent.append(text)

    web._connections["client1"] = FakeWS()
    asyncio.run(web.send(OutboundMessage(channel="web", chat_id="client1", content="片段", kind="delta")))
    asyncio.run(web.send(OutboundMessage(channel="web", chat_id="client1", content="完整回复", kind="final")))

    check("发了 2 帧", len(sent) == 2, str(len(sent)))
    frame0 = json.loads(sent[0])
    frame1 = json.loads(sent[1])
    check("增量帧 type=delta", frame0 == {"type": "delta", "text": "片段"}, str(frame0))
    check("最终帧 type=final", frame1 == {"type": "final", "text": "完整回复"}, str(frame1))

    print("\n=== 8. 网关按渠道能力装/卸 sink ===")
    from gateway import Gateway

    class FakeChannel(Channel):
        name = "fake"
        supports_streaming = False

        async def start(self):
            await asyncio.sleep(3600)

        async def send(self, message):
            pass

    class FakeAgent:
        def __init__(self):
            self.sink = "unset"

        def set_delta_sink(self, sink):
            self.sink = sink

        async def run(self, content):
            return "ok"

    bus2 = MessageBus()
    fake_channel = FakeChannel("fake", bus2)
    gw = Gateway(bus=bus2, channels=[fake_channel], agent_factory=lambda k: FakeAgent())

    from bus.queue import InboundMessage

    asyncio.run(
        gw._handle_inbound(
            InboundMessage(channel="fake", sender_id="u1", chat_id="c1", content="hi")
        )
    )
    agent_obj = gw._agents["fake:u1"]
    check(
        "不支持流式的渠道不会装 sink（处理后已卸下为 None）",
        agent_obj.sink is None,
        str(agent_obj.sink),
    )


# ------------------------------------------------- 9. 重试边界与资源清理
def test_stream_retry_and_cleanup():
    print("\n=== 9. 已吐字后不再重试（否则前半段会输出两遍）===")

    import providers.openai_compat as oc
    from providers.openai_compat import OpenAICompatProvider

    class BoomConnection(Exception):
        """伪装成可重试异常。"""

    original_retryable = oc._RETRYABLE
    oc._RETRYABLE = original_retryable + (BoomConnection,)

    try:
        # 情形 A：已经吐了一段内容后断开 → 不该重试
        chunks = [content_chunk("前半段")]
        provider = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
        provider.client = FakeClientWithStream(
            lambda: FakeStream(chunks, raise_after=1, boom=BoomConnection("断了"))
        )

        received = []

        async def on_delta(text):
            received.append(text)

        resp = asyncio.run(
            provider.chat_stream(
                messages=[{"role": "user", "content": "hi"}], on_delta=on_delta
            )
        )

        check("只发了 1 次请求（不重试）", provider.client.calls == 1,
              str(provider.client.calls))
        check("增量没有被重复输出", received == ["前半段"], str(received))
        check("失败被翻译成 error 响应", resp.finish_reason == "error", resp.finish_reason)

        # 情形 B：一个字都没出来就失败 → 应该重试
        provider2 = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
        provider2.client = FakeClientWithStream(
            lambda: FakeStream(
                [content_chunk("成功"), finish_chunk()], raise_after=None
            ),
            fail_times=1,
            boom=BoomConnection("连接失败"),
        )
        received2 = []

        async def on_delta2(text):
            received2.append(text)

        resp2 = asyncio.run(
            provider2.chat_stream(
                messages=[{"role": "user", "content": "hi"}], on_delta=on_delta2
            )
        )

        check("未吐字时会重试（共 2 次请求）", provider2.client.calls == 2,
              str(provider2.client.calls))
        check("重试后成功", resp2.finish_reason == "stop", resp2.finish_reason)
        check("内容正常", resp2.content == "成功", repr(resp2.content))

        print("\n=== 10. 流必须被显式关闭（否则退出时刷红字）===")
        provider3 = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
        provider3.client = FakeClientWithStream(
            lambda: FakeStream([content_chunk("hi"), finish_chunk()])
        )
        asyncio.run(provider3.chat_stream([{"role": "user", "content": "x"}]))
        stream = provider3.client.streams[0]
        check("正常结束后 stream.close() 被调用", stream.closed is True)
        check("底层 response 也被关闭", stream.response_closed is True)

        # 出错路径也必须关
        provider4 = OpenAICompatProvider(api_key="x", base_url="http://fake", model="m")
        provider4.client = FakeClientWithStream(
            lambda: FakeStream([], raise_after=0, boom=BoomConnection("炸了")),
            fail_times=0,
        )
        asyncio.run(provider4.chat_stream([{"role": "user", "content": "x"}]))
        stream4 = provider4.client.streams[0]
        check("异常路径下 stream 也被关闭", stream4.closed is True)

    finally:
        oc._RETRYABLE = original_retryable


if __name__ == "__main__":
    test_provider_stream()
    test_loop_streaming()
    test_channels()
    test_stream_retry_and_cleanup()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        sys.exit(1)
    print("流式输出验证通过")
