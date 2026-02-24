"""
命令执行模块
"""

import asyncio
import json
import locale
import os
import shutil
from typing import List, Optional, Tuple

import httpx

from astrbot.api import logger

from .session import OpenCodeSession


class CommandExecutor:
    """命令执行器 - 负责执行 OpenCode 和 Shell 命令"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logger
        self._remote_client: Optional[httpx.AsyncClient] = None

    def _get_basic_config(self) -> dict:
        return self.config.get("basic_config", {})

    def _get_connection_mode(self) -> str:
        mode = self._get_basic_config().get("connection_mode", "local")
        return str(mode).strip().lower() or "local"

    def is_remote_mode(self) -> bool:
        return self._get_connection_mode() == "remote"

    async def _get_remote_client(self) -> httpx.AsyncClient:
        if self._remote_client and not self._remote_client.is_closed:
            return self._remote_client

        basic_cfg = self._get_basic_config()
        server_url = str(basic_cfg.get("remote_server_url", "")).strip()
        if not server_url:
            raise ValueError(
                "未配置 remote_server_url。请在配置中填写 OpenCode Server 地址。"
            )

        timeout = int(basic_cfg.get("remote_timeout", 300))
        username = str(basic_cfg.get("remote_username", "opencode")).strip()
        password = str(basic_cfg.get("remote_password", ""))
        auth = (username, password) if password else None

        self._remote_client = httpx.AsyncClient(
            base_url=server_url.rstrip("/"),
            auth=auth,
            timeout=httpx.Timeout(timeout),
        )
        return self._remote_client

    async def close(self):
        """释放执行器持有的外部资源"""
        if self._remote_client and not self._remote_client.is_closed:
            await self._remote_client.aclose()

    async def health_check(self) -> Tuple[bool, str]:
        """执行运行模式健康检查"""
        if not self.is_remote_mode():
            return True, "local"

        try:
            client = await self._get_remote_client()
            resp = await client.get("/global/health")
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            version = data.get("version", "unknown")
            healthy = data.get("healthy", True)
            return bool(healthy), f"remote(version={version})"
        except Exception as e:
            return False, f"remote(error={e})"

    def _extract_remote_text(self, payload: dict) -> str:
        parts = payload.get("parts", [])
        texts = []
        for part in parts:
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()

    async def _run_opencode_remote(self, message: str, session: OpenCodeSession) -> str:
        client = await self._get_remote_client()

        try:
            if not session.opencode_session_id:
                create_resp = await client.post("/session", json={})
                create_resp.raise_for_status()
                created = create_resp.json()
                session_id = str(created.get("id", "")).strip()
                if not session_id:
                    return "❌ 远程会话创建成功但未返回 session ID。"
                session.set_opencode_session_id(session_id)
                self.logger.info(f"Remote OpenCode session created: {session_id}")

            body = {"parts": [{"type": "text", "text": message}]}
            resp = await client.post(
                f"/session/{session.opencode_session_id}/message", json=body
            )
            resp.raise_for_status()

            payload = resp.json()
            text = self._extract_remote_text(payload)
            return text or "(远程服务无文本响应)"
        except httpx.HTTPStatusError as e:
            # 远程 session 失效时自动重建一次
            if e.response.status_code == 404 and session.opencode_session_id:
                self.logger.warning(
                    f"Remote session {session.opencode_session_id} not found, recreating"
                )
                session.clear_opencode_session_id()
                return await self._run_opencode_remote(message, session)
            return f"❌ 远程请求失败: HTTP {e.response.status_code}"
        except httpx.RequestError as e:
            return f"❌ 远程网络错误: {e}"

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
        if self.is_remote_mode():
            return await self._run_opencode_remote(message, session)

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
        if self.is_remote_mode():
            try:
                client = await self._get_remote_client()
                resp = await client.get("/session")
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, list):
                    return []
                normalized = []
                for item in payload[:limit]:
                    if not isinstance(item, dict):
                        continue
                    normalized.append(
                        {
                            "id": str(item.get("id", "")),
                            "title": item.get("title") or "无标题",
                        }
                    )
                return normalized
            except Exception as e:
                self.logger.error(f"列出远程 session 时出错: {e}")
                return []

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
        if self.is_remote_mode():
            return "❌ 当前为服务器远程模式，已禁用 /oc-shell。本地 Shell 仅在本地模式可用。"

        try:
            # 默认超时 60 秒，防止死循环
            process = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=60
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                return "❌ 执行超时 (60s)"

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
