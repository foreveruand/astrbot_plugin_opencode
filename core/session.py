"""
会话管理模块
"""

import os
import time
from typing import Dict, Optional

from astrbot.api import logger


class OpenCodeSession:
    """OpenCode 会话对象"""

    def __init__(self, work_dir: str, env: dict):
        self.work_dir = work_dir
        self.env = env
        self.created_at = time.time()
        # OpenCode 的 session ID，用于跨进程保持对话上下文
        self.opencode_session_id: Optional[str] = None

    def set_opencode_session_id(self, session_id: str):
        """设置 OpenCode session ID"""
        self.opencode_session_id = session_id

    def clear_opencode_session_id(self):
        """清除 OpenCode session ID（用于 oc-new 重置会话）"""
        self.opencode_session_id = None


class SessionManager:
    """会话管理器"""

    def __init__(self, config: dict, base_data_dir: str):
        self.config = config
        self.base_data_dir = base_data_dir
        self.sessions: Dict[str, OpenCodeSession] = {}
        self.logger = logger
        # 用于记录工作目录的回调函数（由外部注入）
        self._record_workdir_callback = None

    def set_record_workdir_callback(self, callback):
        """设置记录工作目录的回调函数"""
        self._record_workdir_callback = callback

    def get_session(self, sender_id: str) -> Optional[OpenCodeSession]:
        """获取已有会话"""
        return self.sessions.get(sender_id)

    def delete_session(self, sender_id: str) -> bool:
        """删除会话"""
        if sender_id in self.sessions:
            del self.sessions[sender_id]
            return True
        return False

    def get_or_create_session(
        self, sender_id: str, custom_work_dir: str = None
    ) -> OpenCodeSession:
        """获取或创建会话"""
        if sender_id in self.sessions and not custom_work_dir:
            return self.sessions[sender_id]

        basic_cfg = self.config.get("basic_config", {})
        default_wd = basic_cfg.get("work_dir", "").strip()

        if custom_work_dir:
            wd = custom_work_dir
        elif default_wd:
            wd = default_wd
        else:
            wd = os.path.join(self.base_data_dir, "workspace")

        if not os.path.exists(wd):
            try:
                os.makedirs(wd, exist_ok=True)
            except Exception as e:
                self.logger.warning(
                    f"Failed to create work dir {wd}: {e}, fallback to cwd"
                )
                wd = os.getcwd()
                self.logger.warning(f"Fallback to cwd: {wd}")

        env = os.environ.copy()

        # 代理配置
        proxy_url = basic_cfg.get("proxy_url", "").strip()
        if proxy_url:
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            self.logger.info(f"Proxy configured for session {sender_id}: {proxy_url}")

        session = OpenCodeSession(wd, env)
        self.sessions[sender_id] = session

        # 记录工作目录到历史
        if self._record_workdir_callback:
            self._record_workdir_callback(wd, sender_id)

        self.logger.info(f"Session created for {sender_id} at {wd}")
        return session

    def get_all_workdirs(self) -> list:
        """获取所有活跃会话的工作目录"""
        return [s.work_dir for s in self.sessions.values()]
