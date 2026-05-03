"""
安全检查模块
"""

import os
import re
from typing import Optional, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .session import OpenCodeSession


class SecurityChecker:
    """安全检查器"""

    def __init__(self, config: dict, base_data_dir: str):
        self.config = config
        self.base_data_dir = base_data_dir
        self.logger = logger
        # 用于加载历史记录的回调
        self._load_history_callback: Optional[Callable[[], list]] = None

    def set_load_history_callback(self, callback: Callable[[], list]):
        """设置加载历史记录的回调函数"""
        self._load_history_callback = callback

    def check_admin(self, event: AstrMessageEvent) -> bool:
        """检查权限"""
        if self.config.get("basic_config", {}).get("only_admin", True):
            return event.is_admin()
        return True

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """检查权限（兼容别名）"""
        return self.check_admin(event)

    def is_destructive(self, task: str) -> bool:
        """检查任务是否包含敏感操作"""
        basic_cfg = self.config.get("basic_config", {})

        # 1. 检查配置中的黑名单正则
        destructive_keywords = basic_cfg.get("destructive_keywords", [])
        task_lower = task.lower()
        if any(re.search(kw, task_lower) for kw in destructive_keywords):
            return True

        # 2. 检查写操作安全开关
        if basic_cfg.get("confirm_all_write_ops", True):
            write_keywords = [
                r"写",
                r"创建",
                r"修改",
                r"write",
                r"create",
                r"edit",
                r"modify",
                r"save",
                r"更新",
                r"update",
            ]
            if any(re.search(kw, task_lower) for kw in write_keywords):
                return True

        return False

    def is_path_safe(
        self, path: str, session: Optional[OpenCodeSession] = None
    ) -> bool:
        """检查路径是否在允许的范围内

        允许的路径：
        1. 当前会话的工作目录（如果提供了session）
        2. 插件数据目录
        3. 历史记录中的工作目录

        Args:
            path: 要检查的路径
            session: OpenCodeSession 对象（可选）

        Returns:
            bool: 路径是否安全
        """
        # 0. 检查配置开关
        check_enabled = self.config.get("basic_config", {}).get(
            "check_path_safety", False
        )
        if not check_enabled:
            return True

        try:
            abs_path = os.path.abspath(path)

            # 1. 检查当前会话的工作目录
            if session and abs_path.startswith(os.path.abspath(session.work_dir)):
                return True

            # 2. 检查插件数据目录
            if abs_path.startswith(self.base_data_dir):
                return True

            # 3. 检查历史工作目录
            if self._load_history_callback:
                history = self._load_history_callback()
                for record in history:
                    workdir = record.get("path")
                    if workdir and abs_path.startswith(os.path.abspath(workdir)):
                        return True

            return False
        except Exception as e:
            self.logger.warning(f"路径安全检查失败: {e}")
            return False
