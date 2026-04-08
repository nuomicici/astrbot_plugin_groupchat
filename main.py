# main.py
import logging

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.agent.message import AssistantMessageSegment, UserMessageSegment, TextPart

logger = logging.getLogger("astrbot")

class AutoChatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 动态保存已开启群聊监听的会话 UMO (热重载会丢失，符合要求)
        self.active_sessions = set()
        self.commands = self._get_all_commands()

    @staticmethod
    def _get_all_commands() -> list[str]:
        """遍历所有注册的处理器获取所有命令，用于规避误拦截正常指令"""
        commands = []
        for handler in star_handlers_registry:
            for fl in handler.event_filters:
                if isinstance(fl, CommandFilter):
                    commands.append(fl.command_name)
                elif isinstance(fl, CommandGroupFilter):
                    commands.append(fl.group_name)
        return commands

    @filter.command("开始群聊")
    async def start_chat(self, event: AstrMessageEvent):
        """开启当前会话的全局监听"""
        umo = event.unified_msg_origin
        whitelist = self.config.get("whitelist", [])
        
        if not whitelist:
            yield event.plain_result("白名单为空，功能未启用。请在控制台配置白名单。")
            return
            
        if umo not in whitelist:
            yield event.plain_result(f"当前会话 ({umo}) 不在白名单中，无法开启群聊监听。")
            return
            
        self.active_sessions.add(umo)
        yield event.plain_result("已开启群聊监听！后续消息将自动视为唤醒并回复。")

    @filter.command("结束群聊")
    async def stop_chat(self, event: AstrMessageEvent):
        """关闭当前会话的全局监听"""
        umo = event.unified_msg_origin
        if umo in self.active_sessions:
            self.active_sessions.remove(umo)
            yield event.plain_result("已关闭群聊监听。")
        else:
            yield event.plain_result("群聊监听未开启。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99999)
    async def on_group_msg(self, event: AstrMessageEvent):
        """收到消息后：视为唤醒并处理，绝不阻塞其他事件"""
        umo = event.unified_msg_origin
        
        # 1. 判断是否处于开启状态
        if umo not in self.active_sessions:
            return
            
        # 2. 校验白名单（防止运行时面板清空了白名单，但缓存状态还在）
        whitelist = self.config.get("whitelist", [])
        if not whitelist or umo not in whitelist:
            if umo in self.active_sessions:
                self.active_sessions.remove(umo)
            return

        # 3. 规避机器人自身的消息
        if event.get_sender_id() == event.get_self_id():
            return

        msg_str = event.message_str.strip()
        if not msg_str:
            return

        # 4. 规避常规指令：以 / 开头，或者第一项命中已注册指令，直接放行交由原插件处理
        if msg_str.startswith("/"):
            return
        
        first_arg = msg_str.split(" ", 1)[0]
        if first_arg in self.commands or first_arg in ["开始群聊", "结束群聊"]:
            return

        # 5. 触发LLM回复机制并维护历史记录
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            
            user_msg = UserMessageSegment(content=[TextPart(text=msg_str)])
            
            # 手动请求大模型回复 (等同于被唤醒后的正常响应)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=msg_str
            )
            
            if llm_resp and llm_resp.completion_text:
                # 规范操作：将当前手动触发的对话对存入平台历史上下文记录
                await conv_mgr.add_message_pair(
                    cid=curr_cid,
                    user_message=user_msg,
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=llm_resp.completion_text)]
                    )
                )
                
                # 发送大模型结果，且不调用 event.stop_event()，保证完全非阻塞其他功能
                yield event.plain_result(llm_resp.completion_text)

        except Exception as e:
            logger.error(f"群聊监听 LLM 处理异常: {e}")
