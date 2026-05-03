"""
数据持久化和清理模块
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Tuple, Callable, Optional

from astrbot.api import logger


class StorageManager:
    """存储管理器 - 负责历史记录和临时文件清理"""

    def __init__(self, base_data_dir: str, config: dict):
        self.base_data_dir = base_data_dir
        self.config = config
        self.history_file = os.path.join(base_data_dir, "workdir_history.json")
        self.logger = logger
        self.auto_clean_task_handle = None
        # 用于获取活跃会话工作目录的回调
        self._get_workdirs_callback: Optional[Callable[[], list]] = None

    def set_get_workdirs_callback(self, callback: Callable[[], list]):
        """设置获取工作目录列表的回调函数"""
        self._get_workdirs_callback = callback

    # ==================== 历史记录管理 ====================

    def load_workdir_history(self) -> list:
        """加载工作目录历史记录"""
        if not os.path.exists(self.history_file):
            return []

        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            self.logger.warning(f"读取工作目录历史失败: {e}")
            return []

    def save_workdir_history(self, history: list):
        """保存工作目录历史记录"""
        try:
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"保存工作目录历史失败: {e}")

    def record_workdir(self, work_dir: str, sender_id: str = None):
        """记录工作目录到历史

        Args:
            work_dir: 工作目录路径
            sender_id: 发送者ID（可选）
        """
        history = self.load_workdir_history()

        # 检查是否已存在相同路径的记录
        existing_index = -1
        for i, record in enumerate(history):
            if record.get("path") == work_dir:
                existing_index = i
                break

        # 构造新记录
        new_record = {
            "path": work_dir,
            "last_used": datetime.now().isoformat(),
            "used_count": 1,
            "sender_id": sender_id,
        }

        if existing_index >= 0:
            # 更新已有记录
            old_record = history[existing_index]
            new_record["used_count"] = old_record.get("used_count", 0) + 1
            new_record["first_used"] = old_record.get(
                "first_used", new_record["last_used"]
            )
            history[existing_index] = new_record
        else:
            # 添加新记录
            new_record["first_used"] = new_record["last_used"]
            history.append(new_record)

        # 保持最多100条记录，按最后使用时间排序
        history.sort(key=lambda x: x.get("last_used", ""), reverse=True)
        history = history[:100]

        self.save_workdir_history(history)

    # ==================== 临时文件清理 ====================

    def start_auto_clean_task(self):
        """启动或重启自动清理任务"""
        if self.auto_clean_task_handle:
            self.auto_clean_task_handle.cancel()

        interval = self.config.get("basic_config", {}).get("auto_clean_interval", 60)
        if interval > 0:
            self.auto_clean_task_handle = asyncio.create_task(
                self._auto_clean_loop(interval)
            )
            self.logger.info(f"Auto-clean task started, interval: {interval} minutes")

    async def stop_auto_clean_task(self):
        """停止自动清理任务"""
        if self.auto_clean_task_handle:
            self.auto_clean_task_handle.cancel()
            try:
                await self.auto_clean_task_handle
            except asyncio.CancelledError:
                pass

    async def _auto_clean_loop(self, interval_minutes: int):
        """定期清理临时文件循环"""
        while True:
            try:
                # 每次循环重新获取配置间隔，支持热重载
                current_interval = self.config.get("basic_config", {}).get(
                    "auto_clean_interval", 60
                )

                # 如果配置变为0，则暂停清理（等待一段时间再检查）
                if current_interval <= 0:
                    await asyncio.sleep(60)
                    continue

                await asyncio.sleep(current_interval * 60)
                await self.clean_temp_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Auto-clean task error: {e}")
                await asyncio.sleep(60)  # 出错后等待1分钟重试

    async def clean_temp_files(self) -> Tuple[int, float]:
        """执行清理操作，返回 (清理文件数, 清理大小MB)"""
        count = 0
        total_size = 0

        # 1. 清理日志文件 (data/plugin_data/astrbot_plugin_opencode/*.txt)
        if os.path.exists(self.base_data_dir):
            for f in os.listdir(self.base_data_dir):
                if f.startswith("opencode_output_") and f.endswith(".txt"):
                    full_path = os.path.join(self.base_data_dir, f)
                    try:
                        size = os.path.getsize(full_path)
                        os.remove(full_path)
                        count += 1
                        total_size += size
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to delete temp log {full_path}: {e}"
                        )

        # 2. 清理各个会话的 downloaded 目录
        default_workspace = os.path.join(self.base_data_dir, "workspace")
        workspaces = [default_workspace]

        # 添加活跃会话的自定义 workspace
        if self._get_workdirs_callback:
            for wd in self._get_workdirs_callback():
                if wd not in workspaces:
                    workspaces.append(wd)

        for wd in workspaces:
            download_dir = os.path.join(wd, "downloaded")
            if os.path.exists(download_dir):
                for f in os.listdir(download_dir):
                    full_path = os.path.join(download_dir, f)
                    if os.path.isfile(full_path):
                        try:
                            size = os.path.getsize(full_path)
                            os.remove(full_path)
                            count += 1
                            total_size += size
                        except Exception as e:
                            self.logger.warning(
                                f"Failed to delete temp file {full_path}: {e}"
                            )

        total_mb = total_size / (1024 * 1024)
        if count > 0:
            self.logger.info(f"Cleaned {count} temp files, total {total_mb:.2f} MB")
        return count, total_mb
