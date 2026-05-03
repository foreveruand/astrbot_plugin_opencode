"""
输入处理模块
"""

import asyncio
import os
import time
import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Image, File, Reply

from .session import OpenCodeSession
from .utils import write_file_sync


class InputProcessor:
    """输入处理器 - 负责处理用户输入消息和下载资源"""

    def __init__(self):
        self.logger = logger

    async def process_input_message(
        self,
        event: AstrMessageEvent,
        session: OpenCodeSession,
        raw_command_text: str = "",
    ) -> str:
        """
        统一处理输入消息：
        1. 扫描直接发送的图片/文件
        2. 扫描引用的图片/文件/文本
        3. 下载资源到 downloaded 目录
        4. 构造最终 prompt
        """
        download_dir = os.path.join(session.work_dir, "downloaded")
        os.makedirs(download_dir, exist_ok=True)

        final_prompt_parts = []

        # 兼容性处理：AstrBotMessage 组件列表属性名为 message (v4.13+)
        # 如果 message 属性不存在，尝试 content 作为回退
        message_chain = getattr(
            event.message_obj, "message", getattr(event.message_obj, "content", [])
        )

        # --- 1. 处理引用消息 (Reply) ---
        reply_component = next((c for c in message_chain if isinstance(c, Reply)), None)
        if reply_component:
            # 尝试获取引用消息的 chain (AstrBot 可能会自动拉取)
            quote_chain = getattr(reply_component, "chain", []) or []

            quote_text_parts = []
            for c in quote_chain:
                if isinstance(c, Plain):
                    quote_text_parts.append(c.text)
                elif isinstance(c, (Image, File)):  # 处理引用中的媒体
                    path = await self._download_resource(c, download_dir)
                    if path:
                        quote_text_parts.append(f" {path} ")

            full_quote_text = "".join(quote_text_parts).strip()
            if full_quote_text:
                final_prompt_parts.append(f"[引用:{full_quote_text}]")

        # --- 2. 处理当前消息中的文本和媒体 ---
        current_msg_parts = []
        for c in message_chain:
            if isinstance(c, (Image, File)):
                path = await self._download_resource(c, download_dir)
                if path:
                    current_msg_parts.append(f" {path} ")
            elif isinstance(c, Plain):
                pass

        # 组装
        if raw_command_text:
            final_prompt_parts.append(raw_command_text)

        if current_msg_parts:
            final_prompt_parts.append(" ".join(current_msg_parts))

        return " ".join(final_prompt_parts).strip()

    async def _download_resource(self, component, save_dir: str) -> str:
        """下载 Image/File 组件资源"""
        url = getattr(component, "url", None)
        if not url:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()

                        # 智能获取后缀名
                        ext = ".bin"  # 默认值

                        # 1. 尝试从组件的原始文件名获取 (优先级最高)
                        orig_name = getattr(component, "name", "")
                        if orig_name and "." in orig_name:
                            ext = os.path.splitext(orig_name)[1].lower()
                        else:
                            # 2. 尝试从 Content-Type 获取
                            import mimetypes

                            ct = (
                                resp.headers.get("Content-Type", "")
                                .split(";")[0]
                                .strip()
                            )
                            guessed_ext = mimetypes.guess_extension(ct)
                            if guessed_ext:
                                ext = guessed_ext
                            elif "text/plain" in ct:
                                ext = ".txt"
                            elif "image/png" in ct:
                                ext = ".png"
                            elif "image/jpeg" in ct:
                                ext = ".jpg"
                            elif "image/gif" in ct:
                                ext = ".gif"
                            # 3. 如果还是没有，尝试从 URL 路径获取
                            else:
                                from urllib.parse import urlparse

                                path = urlparse(url).path
                                url_ext = os.path.splitext(path)[1]
                                if url_ext:
                                    ext = url_ext

                        # 构造基础文件名：resource_{时间戳}_{随机HEX}
                        base_name_no_ext = (
                            f"resource_{int(time.time())}_{os.urandom(4).hex()}"
                        )
                        name = f"{base_name_no_ext}{ext}"
                        filepath = os.path.join(save_dir, name)

                        # 冲突检测与自动重命名：自动添加 (1), (2)...
                        counter = 1
                        while os.path.exists(filepath):
                            name = f"{base_name_no_ext}({counter}){ext}"
                            filepath = os.path.join(save_dir, name)
                            counter += 1

                        # 异步写入文件，防止阻塞
                        try:
                            await asyncio.to_thread(write_file_sync, filepath, data)
                        except OSError as e:
                            self.logger.error(f"文件写入失败 (权限或磁盘问题): {e}")
                            return None

                        return os.path.abspath(filepath)
                    else:
                        self.logger.warning(f"资源下载失败, HTTP 状态码: {resp.status}")
                        return None
        except aiohttp.ClientError as e:
            self.logger.error(f"网络请求失败: {e}")
            return None
        except asyncio.TimeoutError:
            self.logger.error("资源下载超时")
            return None
