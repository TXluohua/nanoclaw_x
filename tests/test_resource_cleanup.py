"""资源生命周期测试

为什么单独一个文件：这一条是真实冒烟测试撞出来的 ——
程序跑完一切正常，退出时却刷一屏

    RuntimeError: generator didn't stop after athrow()
    asyncgen: <async_generator object PoolByteStream.__aiter__>

根因不是流式解析写错了，而是**谁创建谁关闭**这条原则没落实：
`AsyncOpenAI` 内部持有的 httpx 连接池从头到尾没被关闭，
只能等垃圾回收；而 GC 常发生在事件循环关闭之后，异步生成器收不了尾。

这类问题很容易在功能测试里漏掉——因为**功能确实全对**。
所以在测试里直接断言"关闭方法被调用了"，而不是靠观察有没有warning。
"""

import asyncio


class _CloseRecorder:
    """记录 close 被调用几次的假客户端。"""

    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class TestProviderClose:
    def test_close_closes_http_client(self):
        from providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key="x", base_url="http://127.0.0.1:1", model="m"
        )
        recorder = _CloseRecorder()
        provider.client = recorder

        asyncio.run(provider.close())
        assert recorder.close_calls == 1

    def test_close_is_idempotent(self):
        """重复关闭不能报错，也不能重复关（连接池关第二次会抛）。"""
        from providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key="x", base_url="http://127.0.0.1:1", model="m"
        )
        recorder = _CloseRecorder()
        provider.client = recorder

        asyncio.run(provider.close())
        asyncio.run(provider.close())
        assert recorder.close_calls == 1

    def test_close_survives_client_error(self):
        """底层关闭抛异常时，不能让退出流程崩掉。"""
        from providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key="x", base_url="http://127.0.0.1:1", model="m"
        )

        class _Boom:
            async def close(self):
                raise RuntimeError("close failed")

        provider.client = _Boom()
        asyncio.run(provider.close())  # 不应抛出

    def test_streaming_requires_explicit_close(self):
        """接口约定：Provider 必须提供 close()，供上层释放资源。"""
        from providers.base import LLMProvider

        assert hasattr(LLMProvider, "close")


class TestAgentClose:
    def test_agent_close_delegates_to_provider(self):
        """AgentLoop.aclose() 必须把关闭动作转给 provider。"""
        from agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        recorder = _CloseRecorder()
        loop.provider = recorder

        asyncio.run(loop.aclose())
        assert recorder.close_calls == 1

    def test_agent_close_without_provider_close(self):
        """Provider 没实现 close() 时不应崩（桩 Provider 就没有）。"""

        class _NoClose:
            async def chat(self, *args, **kwargs):
                return None

        from agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.provider = _NoClose()
        asyncio.run(loop.aclose())  # 不应抛出
