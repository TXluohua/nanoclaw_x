"""
消息总线模块

MessageBus 提供异步消息队列，支持多渠道接入：
- inbound_queue：入站消息队列（用户发给 Agent）
- outbound_queue：出站消息队列（Agent 回复给用户）

支持多种渠道接入，如飞书、QQ、Web、CLI 等。

使用示例：
    bus = MessageBus()

    # 发布入站消息
    await bus.publish_inbound(InboundMessage(
        channel="feishu",
        sender_id="user123",
        chat_id="group456",
        content="你好"
    ))

    # 消费入站消息
    msg = await bus.consume_inbound()

    # 发布出站消息
    await bus.publish_outbound(OutboundMessage(
        channel="feishu",
        chat_id="group456",
        content="你好！有什么可以帮你的？"
    ))
"""

import asyncio
from dataclasses import dataclass
from typing import Any


class BusFullError(RuntimeError):
    """队列已满，触发背压。"""


@dataclass
class InboundMessage:
    """
    入站消息结构

    用户发给 Agent 的消息，包含来源渠道、发送者、会话和内容。

    Attributes:
        channel: 来源渠道名称，如 "feishu"、"qq"、"web"、"cli"
        sender_id: 发送者标识（用户 ID）
        chat_id: 会话标识（群聊或私聊的 ID）
        content: 消息正文
        raw: 原始消息数据，调试用（可选）
    """

    channel: str
    sender_id: str
    chat_id: str
    content: str
    raw: dict[str, Any] | None = None


@dataclass
class OutboundMessage:
    """
    出站消息结构

    Agent 回复给用户的消息，包含目标渠道、会话和内容。

    Attributes:
        channel: 目标渠道名称
        chat_id: 目标会话 ID
        content: 回复正文
        reply_to: 引用的消息 ID（可选）
        kind: 消息类型
            "final"  完整回复（默认，所有渠道都支持）
            "delta"  流式增量片段（只有声明了 supports_streaming 的渠道会收到）

    ★ 为什么用 kind 而不是拆成两个类：
      网关只维护一条出站队列，分两种消息类型就要两套路由逻辑。
      加一个字段最省事，而且老渠道读到 kind="final" 行为完全不变。
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    kind: str = "final"


class MessageBus:
    """
    消息总线

    使用 asyncio.Queue 实现异步消息队列，支持多渠道并发接入。

    inbound_queue：入站消息队列，存放用户发给 Agent 的消息
    outbound_queue：出站消息队列，存放 Agent 回复给用户的消息

    使用示例：
        bus = MessageBus()

        # 启动消费者（后台任务）
        asyncio.create_task(process_inbound(bus))
        asyncio.create_task(process_outbound(bus))

        # 发布消息
        await bus.publish_inbound(msg)
    """

    # 单个队列的默认容量上限
    DEFAULT_MAXSIZE = 1000

    def __init__(self, maxsize: int | None = None) -> None:
        """初始化消息总线，创建两个【有界】异步队列。

        Args:
            maxsize: 单个队列的容量上限，默认 1000。
                设 0 表示无界 —— 不建议，洪峰时会把内存吃光。

        ★ 为什么要有界：原实现是无界队列，消息洪峰（例如 QQ 群被刷屏、
          或网关处理不过来）时内存无限增长，最后 OOM 直接进程死掉，
          连"当时发生了什么"都来不及记录。有界 + 明确拒绝，
          至少能告诉调用方"现在忙，请重试"，并在日志里留下证据。
        """
        size = self.DEFAULT_MAXSIZE if maxsize is None else maxsize
        self.maxsize = size
        self.inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue(
            maxsize=size
        )
        self.outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue(
            maxsize=size
        )
        # 背压统计：队满被拒的次数，便于事后判断"当时到底有多忙"
        self.rejected_inbound = 0
        self.rejected_outbound = 0

    def stats(self) -> dict[str, Any]:
        """当前队列水位，供可观测性使用。"""
        return {
            "maxsize": self.maxsize,
            "inbound_size": self.inbound_queue.qsize(),
            "outbound_size": self.outbound_queue.qsize(),
            "rejected_inbound": self.rejected_inbound,
            "rejected_outbound": self.rejected_outbound,
        }

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """
        发布入站消息

        队列满时抛 BusFullError（背压），而不是无限堆积。

        Args:
            msg: 入站消息实例

        Raises:
            BusFullError: 入站队列已满
        """
        try:
            self.inbound_queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.rejected_inbound += 1
            print(
                f"警告: 入站队列已满（上限 {self.maxsize}），拒绝消息 "
                f"channel={msg.channel} sender={msg.sender_id}"
                f"（累计拒绝 {self.rejected_inbound} 条）"
            )
            raise BusFullError(f"入站队列已满（上限 {self.maxsize}）") from None

    async def consume_inbound(self) -> InboundMessage:
        """
        消费入站消息

        从 inbound_queue 取出一条消息。如果队列空，自动等待直到有消息。

        Returns:
            InboundMessage: 入站消息实例
        """
        return await self.inbound_queue.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """
        发布出站消息

        队列满时抛 BusFullError。出站队列满意味着渠道发送跟不上，
        通常是某个渠道卡住了（例如飞书接口超时），这时继续堆积只会更糟。

        Args:
            msg: 出站消息实例

        Raises:
            BusFullError: 出站队列已满
        """
        try:
            self.outbound_queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.rejected_outbound += 1
            print(
                f"警告: 出站队列已满（上限 {self.maxsize}），丢弃回复 "
                f"channel={msg.channel} chat={msg.chat_id}"
                f"（累计丢弃 {self.rejected_outbound} 条）"
            )
            raise BusFullError(f"出站队列已满（上限 {self.maxsize}）") from None

    async def consume_outbound(self) -> OutboundMessage:
        """
        消费出站消息

        从 outbound_queue 取出一条消息。如果队列空，自动等待直到有消息。

        Returns:
            OutboundMessage: 出站消息实例
        """
        return await self.outbound_queue.get()