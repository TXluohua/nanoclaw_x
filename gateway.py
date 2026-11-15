"""
Gateway 网关模块

Gateway 是多渠道接入的核心网关，负责：
- 管理多个渠道适配器（Channel）
- 处理入站消息：从 MessageBus 消费 → 分发给对应的 Agent
- 分发出站消息：从 MessageBus 消费 → 发送给对应的渠道
- 缓存 Agent 实例（按 session_key，带 LRU 淘汰）

并发模型（★ 原实现是单消费者，一个慢任务会阻塞所有人）：
    入站：每来一条消息就 spawn 一个独立 task
          同一会话用 asyncio.Lock 串行保序（否则同一个人连发两句会乱序），
          不同会话之间完全并行
    限流：全局 Semaphore 控制同时在跑的会话数，防止 100 个用户同时进来把 API 打穿

使用示例：
    bus = MessageBus()
    cli_channel = CLIChannel(bus)

    def create_agent(session_key: str) -> AgentLoop:
        # 根据 session_key 创建 Agent
        ...

    gateway = Gateway(
        bus=bus,
        channels=[cli_channel],
        agent_factory=create_agent
    )

    # 启动网关
    asyncio.run(gateway.run())
"""

import asyncio
from collections import OrderedDict
from typing import Callable

from bus.queue import MessageBus, InboundMessage, OutboundMessage, BusFullError
from channels.base import Channel
from agent.loop import AgentLoop


class Gateway:
    """
    网关核心类

    管理多渠道接入和消息路由。
    """

    # Agent 实例缓存上限（按会话数）。超过后淘汰最久未用的会话。
    MAX_CACHED_AGENTS = 200

    # 同时处理的会话任务上限
    MAX_CONCURRENT_SESSIONS = 16

    def __init__(
        self,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str], AgentLoop],
        max_cached_agents: int | None = None,
        max_concurrent_sessions: int | None = None,
    ) -> None:
        """
        Args:
            bus: 消息总线实例
            channels: 渠道适配器列表
            agent_factory: Agent 创建函数，输入 session_key，返回 AgentLoop 实例
            max_cached_agents: 缓存的 Agent 实例上限
            max_concurrent_sessions: 同时在跑的会话任务上限
        """
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory

        # 内部状态
        # OrderedDict 而非 dict：LRU 淘汰需要 move_to_end / popitem(last=False)
        self._agents: "OrderedDict[str, AgentLoop]" = OrderedDict()
        self._channel_map: dict[str, Channel] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._max_agents = max_cached_agents or self.MAX_CACHED_AGENTS
        self._max_concurrency = (
            max_concurrent_sessions or self.MAX_CONCURRENT_SESSIONS
        )

        # 构建渠道映射表
        for channel in channels:
            self._channel_map[channel.name] = channel

    async def run(self) -> None:
        """
        启动网关

        用 asyncio.gather 并发启动：
        - 所有渠道的 start()
        - _process_inbound() 入站消费循环
        - _dispatch_outbound() 出站分发循环
        """
        # 信号量要在事件循环里创建（放到 __init__ 里会和"构造时的 loop"绑定）
        self._semaphore = asyncio.Semaphore(self._max_concurrency)

        # 构建任务列表
        tasks = []

        # 添加所有渠道的启动任务
        for channel in self.channels:
            tasks.append(channel.start())

        # 添加入站和出站处理循环
        tasks.append(self._process_inbound())
        tasks.append(self._dispatch_outbound())

        # 并发启动所有任务
        try:
            # 等待所有任务（通常会阻塞直到某个渠道退出）
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            # 取消所有未完成的任务
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 取消所有仍在处理的会话任务
            await self._cancel_session_tasks()
            # 等待任务取消完成（忽略 CancelledError）
            await asyncio.gather(*tasks, return_exceptions=True)
            # 关闭所有渠道
            await self.shutdown()

    async def _cancel_session_tasks(self) -> None:
        """取消所有还在跑的会话任务，并等它们真正结束。"""
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        print(f"[网关] 正在取消 {len(pending)} 个未完成的会话任务...")
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    async def _process_inbound(self) -> None:
        """
        处理入站消息循环

        ★ 关键改造：这个循环只负责"取消息 + 派活"，绝不 await Agent。
          原实现在这里直接 `response = await agent.run(...)`，而 run() 最多可跑
          32 轮 LLM + 工具调用（几十秒到几分钟）。这段时间整个网关停摆：
          飞书的第二个用户、Web 的第二个连接、QQ 群里的所有人，消息全在排队。
          "多渠道网关"实际退化成了单线程、一次一条。
        """
        while True:
            try:
                # 从消息总线消费入站消息
                inbound_msg = await self.bus.consume_inbound()

            except asyncio.CancelledError:
                # 任务被取消，退出循环
                break

            except Exception as e:
                # 消费循环异常，打印日志并继续
                print(f"警告: 入站处理循环异常 - {e}")
                continue

            # 信号量兜底：正常由 run() 创建，但 _process_inbound 也可能被
            # 单独驱动（调试、embed、单测），这里补一次确保不会拿到 None。
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self._max_concurrency)

            # 每条消息一个独立 task：跨会话并行
            task = asyncio.create_task(self._handle_inbound(inbound_msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle_inbound(self, inbound_msg: InboundMessage) -> None:
        """
        处理单条入站消息

        同会话加锁串行（保证同一用户的消息不乱序），跨会话并行。
        并用全局信号量限制同时在跑的会话数，避免把模型 API 打爆。
        """
        session_key = f"{inbound_msg.channel}:{inbound_msg.sender_id}"

        # 同一会话的锁：setdefault 是原子的，无需额外加锁
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())

        try:
            # 先抢全局并发额度，再抢会话锁：
            # 反过来会让"排队中的同一会话"长时间占着额度却不干活
            async with self._get_semaphore():
                async with lock:
                    agent = self._get_or_create_agent(session_key)

                    # 按渠道能力装/卸流式出口：
                    # CLI 与 Web 支持增量，飞书/QQ 只收完整回复（它们发一条就是
                    # 一次 API 调用，逐字推送会被限流）
                    channel = self._channel_map.get(inbound_msg.channel)
                    if (
                        channel is not None
                        and getattr(channel, "supports_streaming", False)
                        and hasattr(agent, "set_delta_sink")
                    ):
                        agent.set_delta_sink(self._make_delta_sink(inbound_msg))

                    try:
                        response = await agent.run(inbound_msg.content)
                    finally:
                        if hasattr(agent, "set_delta_sink"):
                            agent.set_delta_sink(None)

                    outbound_msg = OutboundMessage(
                        channel=inbound_msg.channel,
                        chat_id=inbound_msg.chat_id,
                        content=response,
                    )
                    try:
                        await self.bus.publish_outbound(outbound_msg)
                    except BusFullError:
                        # 出站队列满 = 渠道发送跟不上。bus 已打日志，这里补一条上下文
                        print(f"[网关] 出站队列已满，回复未投递（{session_key}）")

        except asyncio.CancelledError:
            # 网关关闭时被取消，不需要给用户回消息
            raise

        except Exception as e:
            # Agent 出错，发送友好错误消息（带上原因，别让人只看到"出错了"）
            print(f"警告: Agent 处理出错（{session_key}）- {e}")
            try:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=inbound_msg.channel,
                        chat_id=inbound_msg.chat_id,
                        content=f"抱歉，处理消息时发生了错误：{type(e).__name__}: {e}",
                    )
                )
            except Exception as send_error:
                print(f"警告: 错误提示也发送失败了 - {send_error}")

    def _get_semaphore(self) -> asyncio.Semaphore:
        """惰性创建并发信号量。

        正常由 run() 创建；但 _handle_inbound 也可能被单独驱动
        （调试、embed、单测），这里兜一次，避免拿到 None 直接崩。
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    def _make_delta_sink(self, inbound_msg: InboundMessage):
        """造一个把流式片段推进出站队列的 sink。"""

        async def sink(text: str) -> None:
            if not text:
                return
            try:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=inbound_msg.channel,
                        chat_id=inbound_msg.chat_id,
                        content=text,
                        kind="delta",
                    )
                )
            except BusFullError:
                # 丢一段增量不算事故：最终结果会以 final 消息完整送达
                pass

        return sink

    def _get_or_create_agent(self, session_key: str) -> AgentLoop:
        """
        取该会话的 Agent，没有就创建。

        ★ 带 LRU 淘汰：原实现只增不减，飞书群里每个人一个 session_key，
          每人都建一个 Agent 实例（内含 provider 客户端、工具表、历史列表），
          跑一天内存就上去了，而且永远不会释放。
        """
        agent = self._agents.get(session_key)
        if agent is not None:
            self._agents.move_to_end(session_key)  # 标记为最近使用
            return agent

        agent = self.agent_factory(session_key)
        self._agents[session_key] = agent

        # 超出上限则淘汰最久未使用的会话
        while len(self._agents) > self._max_agents:
            old_key, _ = self._agents.popitem(last=False)
            print(f"[网关] Agent 缓存已满（{self._max_agents}），淘汰最久未用的会话: {old_key}")
            # 顺带回收它的锁，但只回收空闲的 —— 正在用的锁不能拆
            old_lock = self._session_locks.get(old_key)
            if old_lock is not None and not old_lock.locked():
                self._session_locks.pop(old_key, None)

        return agent

    async def _dispatch_outbound(self) -> None:
        """
        分发出站消息循环

        while True 循环：
        - 从 bus.consume_outbound() 取回复
        - 根据 msg.channel 找到对应的 Channel
        - 调用 channel.send(msg)

        发送本身很快，保持串行可以确保同一会话的回复顺序稳定。
        """
        while True:
            try:
                # 从消息总线消费出站消息
                outbound_msg = await self.bus.consume_outbound()

                # 查找对应的渠道
                channel = self._channel_map.get(outbound_msg.channel)

                if channel is None:
                    print(f"警告: 渠道 '{outbound_msg.channel}' 不存在，无法发送消息")
                    continue

                # 调用渠道发送消息
                try:
                    await channel.send(outbound_msg)

                except Exception as e:
                    # 渠道发送失败，打印日志
                    print(f"警告: 渠道 '{outbound_msg.channel}' 发送失败 - {e}")

            except asyncio.CancelledError:
                # 任务被取消，退出循环
                break

            except Exception as e:
                # 分发循环异常，打印日志并继续
                print(f"警告: 出站分发循环异常 - {e}")
                continue

    async def shutdown(self) -> None:
        """
        关闭网关

        - 遍历 channels 调用 stop()
        - 清空 _agents / _session_locks 缓存
        """
        # 停止所有渠道
        for channel in self.channels:
            try:
                await channel.stop()
            except Exception as e:
                print(f"警告: 渠道 '{channel.name}' 关闭失败 - {e}")

        # 清空 Agent 缓存
        self._agents.clear()
        self._session_locks.clear()
