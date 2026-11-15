"""
CLI 渠道适配器模块

CLIChannel 提供命令行交互界面，支持用户通过终端与 Agent 对话。
特性：
- 支持命令：/exit（退出）、/clear（清空历史）、/tools（查看工具列表）、
  /cost（本会话 token 与成本）、/trace（最近一次运行的全链路时间线）
- 使用 asyncio.Event 同步输入和回复
- input() 用 asyncio.to_thread 包装，避免阻塞事件循环

使用示例：
    bus = MessageBus()
    cli_channel = CLIChannel(bus)

    # 注入工具列表和清空回调（由 main.py 完成）
    cli_channel.tool_names = ["read_file", "write_file"]
    cli_channel._clear_callback = lambda: print("已清空")

    # 启动渠道
    asyncio.create_task(cli_channel.start())
"""

import asyncio

from bus.queue import MessageBus, InboundMessage, OutboundMessage, BusFullError
from channels.base import Channel

# 需要从用户输入里剥掉的不可见字符：
#   \ufeff  UTF-8 BOM —— 从管道/文件喂输入时第一个字符常常带它，
#           会让 "/cost" 变成 "\ufeff/cost"，命令匹配失败后被当成
#           普通问题发给模型（我就这么误触发过一次真实 API 调用）。
#   其余是零宽空格/连接符，复制粘贴网页文本时偶尔带进来。
_INVISIBLE_PREFIX = "\ufeff\u200b\u200c\u200d\u2060"


def normalize_input(text: str) -> str:
    """清洗用户输入：剥掉 BOM 与零宽字符，再去首尾空白。"""
    if not text:
        return ""
    return text.lstrip(_INVISIBLE_PREFIX).strip()


class CLIChannel(Channel):
    """
    CLI 渠道适配器

    提供命令行交互界面，监听用户输入并发布到 MessageBus。
    """

    # 终端逐字打印体验最好，支持流式增量
    supports_streaming = True

    def __init__(self, bus: MessageBus) -> None:
        """
        Args:
            bus: 消息总线实例
        """
        super().__init__(name="cli", bus=bus)

        # 用于同步"等待回复完成"
        self._response_event = asyncio.Event()

        # 本轮是否已经流式打印过内容（用于 final 时决定要不要重复打印全文）
        self._streamed = False

        # 预留属性，由 main.py 在创建后注入
        self.tool_names: list[str] = []
        self._clear_callback: callable | None = None
        self._cost_callback: callable | None = None
        self._trace_callback: callable | None = None

    async def start(self) -> None:
        """
        启动 CLI 交互循环

        循环读取用户输入，支持命令和普通对话。
        正常输入发布到 bus.inbound_queue，然后等待回复完成。
        """
        print("\n开始对话（/exit 退出，/cost 看花费，/trace 看链路，/tools 看工具）")

        while True:
            try:
                # 用 asyncio.to_thread 包装 input()，避免阻塞事件循环
                user_input = await asyncio.to_thread(input, "\n你> ")
                user_input = normalize_input(user_input)

                # 空输入跳过
                if not user_input:
                    continue

                # 处理命令
                if user_input == "/exit":
                    print("再见！")
                    break

                if user_input == "/clear":
                    # 调用清空回调（如果有）
                    if self._clear_callback:
                        self._clear_callback()
                    print("对话历史已清空")
                    continue

                if user_input == "/tools":
                    # 打印工具列表
                    print(f"可用工具: {', '.join(self.tool_names)}")
                    continue

                if user_input == "/cost":
                    # 本会话的 token / 成本累计
                    if self._cost_callback:
                        print(self._cost_callback())
                    else:
                        print("未启用用量统计")
                    continue

                if user_input == "/trace":
                    # 最近一次运行的全链路时间线
                    if self._trace_callback:
                        print(self._trace_callback())
                    else:
                        print("未启用追踪")
                    continue

                # 正常输入 → 构造入站消息
                inbound_msg = InboundMessage(
                    channel="cli",
                    sender_id="local",
                    chat_id="direct",
                    content=user_input,
                )

                # 新的一轮开始，重置流式标记（否则上一轮的标记会串到这一轮）
                self._streamed = False

                # 发布到消息总线
                try:
                    await self.bus.publish_inbound(inbound_msg)
                except BusFullError:
                    # 背压：网关忙不过来。明确告诉用户重试，而不是静默吞掉
                    print("⚠️ 系统繁忙（消息队列已满），请稍后重试。")
                    continue

                # 重置 Event，等待回复完成
                self._response_event.clear()
                await self._response_event.wait()

            except KeyboardInterrupt:
                # Ctrl+C 优雅退出
                print("\n\n再见！")
                break

            except EOFError:
                # 输入结束（如管道输入完毕）
                print("\n再见！")
                break

    async def send(self, message: OutboundMessage) -> None:
        """
        发送回复给用户

        - kind="delta"：逐字打印，不换行（流式）
        - kind="final"：如果本轮已经流式打印过内容，只补一个换行，
          避免把同一段话再完整打印一遍

        Args:
            message: 出站消息实例
        """
        if message.kind == "delta":
            if not self._streamed:
                # 第一段增量前先换行，跟 "你> " 分开
                print()
                self._streamed = True
            print(message.content, end="", flush=True)
            return

        # kind == "final"
        if self._streamed:
            # 内容已经边流边打完了，这里只需要收尾
            print()
            self._streamed = False
        else:
            print(f"\n🤖 {message.content}")

        # 通知 start() 可以继续了
        self._response_event.set()