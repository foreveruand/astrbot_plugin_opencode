"""
命令执行模块
"""

import asyncio
import json
import locale
import os
import shutil
from typing import List, Tuple

from astrbot.api import logger

from .session import OpenCodeSession


class CommandExecutor:
    """命令执行器 - 负责执行 OpenCode 和 Shell 命令"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logger

    def _parse_json_output(self, raw_output: str) -> Tuple[str, str]:
        """解析 OpenCode JSON 格式输出

        返回: (session_id, text_content)
        """
        session_id = None
        text_parts = []

        for line in raw_output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                # 提取 session ID（从任意事件中）
                if not session_id and "sessionID" in event:
                    session_id = event["sessionID"]

                # 只提取 text 类型事件的内容
                if event.get("type") == "text":
                    part = event.get("part", {})
                    text = part.get("text", "")
                    if text:
                        text_parts.append(text)
            except json.JSONDecodeError:
                # 如果某行不是有效 JSON，跳过
                continue

        return session_id, "\n".join(text_parts)

    async def run_opencode(self, message: str, session: OpenCodeSession) -> str:
        """执行 OpenCode 命令，支持会话持久化"""
        opencode_path = self.config.get("basic_config", {}).get(
            "opencode_path", "opencode"
        )

        # 自动探测绝对路径 (特别是针对 Windows npm 全局安装)
        if opencode_path == "opencode":
            resolved_path = shutil.which("opencode")
            if resolved_path:
                opencode_path = resolved_path
            # Windows 特殊处理：npm 安装的通常是 .cmd
            elif os.name == "nt":
                resolved_path = shutil.which("opencode.cmd")
                if resolved_path:
                    opencode_path = resolved_path

        # 构建命令参数
        cmd_args = [opencode_path, "run", "--format", "json"]

        # 如果已有 session ID，继续该会话
        if session.opencode_session_id:
            cmd_args.extend(["--session", session.opencode_session_id])

        # 添加消息内容
        cmd_args.append(message)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=session.env,
                cwd=session.work_dir,
            )
            stdout, stderr = await process.communicate()
            raw_output = stdout.decode("utf-8", errors="ignore")
            error_output = stderr.decode("utf-8", errors="ignore")

            if process.returncode != 0:
                # 如果是 session 不存在导致的错误，清除 session ID 并重试
                if session.opencode_session_id and "session" in error_output.lower():
                    self.logger.warning(
                        f"Session {session.opencode_session_id} 可能已失效，清除并重试"
                    )
                    session.clear_opencode_session_id()
                    return await self.run_opencode(message, session)
                return f"执行失败 (Return Code: {process.returncode})\n错误信息: {error_output}\n输出: {raw_output}"

            # 解析 JSON 输出
            new_session_id, text_content = self._parse_json_output(raw_output)

            # 保存 session ID（仅在首次或更新时）
            if new_session_id and new_session_id != session.opencode_session_id:
                session.set_opencode_session_id(new_session_id)
                self.logger.info(f"OpenCode session ID 已保存: {new_session_id}")

            # 如果解析出文本内容，返回；否则返回原始输出
            if text_content:
                return text_content
            else:
                # 解析失败时回退到原始输出（去除 JSON 格式）
                return raw_output

        except FileNotFoundError:
            return f"❌ 找不到 OpenCode 可执行文件: {opencode_path}\n请检查配置中的 opencode_path 是否正确。"
        except PermissionError:
            return f"❌ 没有权限执行 OpenCode: {opencode_path}"
        except OSError as e:
            return f"❌ 系统错误: {e}"

    async def list_opencode_sessions(self, limit: int = 10) -> List[dict]:
        """列出 OpenCode 的 session 列表"""
        opencode_path = self.config.get("basic_config", {}).get(
            "opencode_path", "opencode"
        )

        if opencode_path == "opencode":
            resolved_path = shutil.which("opencode")
            if resolved_path:
                opencode_path = resolved_path
            elif os.name == "nt":
                resolved_path = shutil.which("opencode.cmd")
                if resolved_path:
                    opencode_path = resolved_path

        cmd_args = [
            opencode_path,
            "session",
            "list",
            "-n",
            str(limit),
            "--format",
            "json",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            raw_output = stdout.decode("utf-8", errors="ignore")

            if process.returncode != 0:
                self.logger.error(
                    f"列出 session 失败: {stderr.decode('utf-8', errors='ignore')}"
                )
                return []

            return json.loads(raw_output)
        except Exception as e:
            self.logger.error(f"列出 session 时出错: {e}")
            return []

    async def exec_shell_cmd(self, cmd: str) -> str:
        """执行 Shell 命令"""
        try:
            # 默认超时 30 秒，防止死循环
            process = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=30
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                return "❌ 执行超时 (30s)"

            # 使用系统默认编码解码（Windows中文版=GBK, 英文版=CP1252, Linux=UTF-8等）
            encoding = locale.getpreferredencoding(False)
            output = stdout.decode(encoding, errors="replace").strip()
            error = stderr.decode(encoding, errors="replace").strip()

            resp = []
            if output:
                resp.append(f"输出:\n{output}")
            if error:
                resp.append(f"错误:\n{error}")
            if not resp:
                resp.append("执行完成，无输出。")

            resp.append(f"\n(Return Code: {process.returncode})")
            return "\n".join(resp)
        except FileNotFoundError as e:
            return f"❌ 命令不存在: {e}"
        except PermissionError as e:
            return f"❌ 权限不足: {e}"
        except OSError as e:
            return f"❌ 系统错误: {e}"
