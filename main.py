"""
AstrBot OpenCode 插件 - 让 AstrBot 对接 OpenCode，通过自然语言远程指挥电脑干活。使用此插件，意味着你已知晓相关风险。
"""

import asyncio
import os
import re
import shlex
from datetime import datetime
from typing import Optional

from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.all import *
from astrbot.core.utils.session_waiter import session_waiter, SessionController
from astrbot.api.message_components import File

# 导入核心模块
from .core.session import SessionManager
from .core.storage import StorageManager
from .core.security import SecurityChecker
from .core.input import InputProcessor
from .core.executor import CommandExecutor
from .core.output import OutputProcessor


@register(
    "astrbot_plugin_opencode",
    "singularity2000",
    "让 AstrBot 对接 OpenCode，通过自然语言远程指挥电脑干活。使用此插件，意味着你已知晓相关风险。",
    "1.3.0",
    "https://github.com/singularity2000/astrbot_plugin_opencode",
)
class OpenCodePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.logger = logger

        # 基础数据目录（使用框架 API 获取，兼容不同部署环境）
        self.base_data_dir = str(
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_opencode"
        )

        # 初始化各个核心模块
        self.session_mgr = SessionManager(config, self.base_data_dir)
        self.storage_mgr = StorageManager(self.base_data_dir, config)
        self.security = SecurityChecker(config, self.base_data_dir)
        self.input_proc = InputProcessor()
        self.executor = CommandExecutor(config)
        self.output_proc = OutputProcessor(config, self.base_data_dir)
        self._send_file_list_cache: dict[str, dict] = {}

        # 设置模块间的回调函数，建立模块间的通信
        self.session_mgr.set_record_workdir_callback(self.storage_mgr.record_workdir)
        self.storage_mgr.set_get_workdirs_callback(self.session_mgr.get_all_workdirs)
        self.security.set_load_history_callback(self.storage_mgr.load_workdir_history)

    def _render_exec_status(self, session) -> str:
        """根据运行模式渲染执行中提示"""
        if self.executor.is_remote_mode():
            return f"🚀 执行中... (服务器远程模式)\n📦 本地缓存目录: {session.work_dir}"
        return f"🚀 执行中... (本地模式)\n📂 工作目录: {session.work_dir}"

    def _find_local_path_refs(self, text: str) -> list[str]:
        """从文本中提取疑似本地路径引用（用于 remote 模式保护）"""
        if not text:
            return []

        findings = []
        patterns = [
            r"[A-Za-z]:\\[^\s\"']+",  # Windows 绝对路径
            r"/(?:[^\s\"']+/)+[^\s\"']*",  # Unix 风格绝对路径
        ]

        for pattern in patterns:
            for match in re.findall(pattern, text):
                findings.append(match)

        # downloaded 目录是此插件最常见的本地资源路径
        if "downloaded" in text.lower():
            findings.append("<downloaded-resource>")

        # 去重并限制长度，避免提示过长
        deduped = []
        for item in findings:
            if item not in deduped:
                deduped.append(item)
        return deduped[:3]

    def _remote_input_guard_message(self, local_refs: list[str]) -> str:
        refs = (
            "\n".join([f"- {r}" for r in local_refs])
            if local_refs
            else "- (未识别具体路径)"
        )
        return (
            "⚠️ 当前为服务器远程模式，检测到本地路径/本地缓存资源引用，远端 OpenCode 无法直接访问这些文件。\n\n"
            f"检测到的本地引用：\n{refs}\n\n"
            "建议：\n"
            "1. 改为纯文本描述任务；\n"
            "2. 先把文件放到远端服务器可访问路径后再让 OpenCode 处理；\n"
            "3. 需要直接操作本机文件时，请将 connection_mode 切换为 local。"
        )

    def _get_send_page_size(self) -> int:
        return 50

    def _get_send_scan_limit(self) -> int:
        return 10000

    def _extract_oc_send_args(self, event: AstrMessageEvent, fallback_path: str) -> str:
        full_command = event.message_str.strip()
        parts = full_command.split(" ", 1)
        if len(parts) > 1:
            return parts[1].strip()
        return (fallback_path or "").strip()

    def _is_absolute_like_path(self, path_text: str) -> bool:
        if os.path.isabs(path_text):
            return True
        return bool(re.match(r"^[A-Za-z]:[\\/]", path_text))

    def _tokenize_send_args(self, arg_text: str) -> list[str]:
        if not arg_text:
            return []
        try:
            pieces = shlex.split(arg_text, posix=False)
        except ValueError:
            pieces = arg_text.split()

        tokens: list[str] = []
        for piece in pieces:
            for part in piece.split(","):
                token = part.strip().strip('"').strip("'")
                if token:
                    tokens.append(token)
        return tokens

    def _scan_workspace_files(self, work_dir: str, keyword: str = "") -> dict:
        page_size = self._get_send_page_size()
        scan_limit = self._get_send_scan_limit()
        keyword_lower = keyword.lower().strip()

        rel_files: list[str] = []
        scanned = 0
        truncated = False

        for root, _, files in os.walk(work_dir, onerror=lambda _: None):
            for filename in files:
                scanned += 1
                if scanned > scan_limit:
                    truncated = True
                    break

                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, work_dir).replace("\\", "/")
                if keyword_lower and keyword_lower not in rel_path.lower():
                    continue
                rel_files.append(rel_path)

            if truncated:
                break

        rel_files.sort()
        total = len(rel_files)
        total_pages = max(1, (total + page_size - 1) // page_size)

        return {
            "work_dir": work_dir,
            "files": rel_files,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "keyword": keyword,
            "scanned": scanned,
            "truncated": truncated,
            "created_at": datetime.now().isoformat(),
        }

    def _render_send_file_page(self, snapshot: dict, page: int) -> str:
        files = snapshot["files"]
        total = snapshot["total"]
        page_size = snapshot["page_size"]
        total_pages = snapshot["total_pages"]
        page = max(1, min(page, total_pages))

        start = (page - 1) * page_size
        end = min(start + page_size, total)

        lines = [
            "📄 可发送文件列表（当前工作区，递归）",
            f"📂 目录: {snapshot['work_dir']}",
            f"📊 共 {total} 个文件 | 第 {page}/{total_pages} 页 | 每页 {page_size} 条",
        ]

        keyword = (snapshot.get("keyword") or "").strip()
        if keyword:
            lines.append(f"🔎 过滤关键词: {keyword}")

        if snapshot.get("truncated"):
            lines.append(
                f"⚠️ 已触发扫描上限（{self._get_send_scan_limit()}），结果可能不完整。建议使用 /oc-send --find 关键词 缩小范围。"
            )

        lines.append("")

        if total == 0:
            lines.append("（没有可发送的文件）")
        else:
            for idx in range(start, end):
                rel_path = files[idx]
                display = (
                    rel_path
                    if len(rel_path) <= 120
                    else rel_path[:57] + "..." + rel_path[-60:]
                )
                lines.append(f"{idx + 1}. {display}")

        lines.extend(
            [
                "",
                "快捷发送示例:",
                "- /oc-send 1",
                "- /oc-send 2,5,8",
                "- /oc-send 10-15",
                "- /oc-send src/main.py docs/readme.md",
                "- /oc-send --page 2",
                "- /oc-send --find config",
            ]
        )
        return "\n".join(lines)

    def _parse_send_page_query(self, arg_text: str) -> Optional[int]:
        match = re.fullmatch(r"--page\s+(\d+)", arg_text.strip())
        if not match:
            return None
        return int(match.group(1))

    def _parse_send_find_query(self, arg_text: str) -> Optional[str]:
        match = re.fullmatch(r"--find\s+(.+)", arg_text.strip())
        if not match:
            return None
        return match.group(1).strip()

    def _expand_index_tokens(
        self, tokens: list[str], max_index: int
    ) -> tuple[list[int], list[str]]:
        indexes: list[int] = []
        errors: list[str] = []

        for token in tokens:
            range_match = re.fullmatch(r"(\d+)-(\d+)", token)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                if start > end:
                    errors.append(f"无效范围: {token}")
                    continue
                for idx in range(start, end + 1):
                    if 1 <= idx <= max_index:
                        indexes.append(idx)
                    else:
                        errors.append(f"序号越界: {idx}")
                continue

            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= max_index:
                    indexes.append(idx)
                else:
                    errors.append(f"序号越界: {idx}")

        deduped_indexes: list[int] = []
        for idx in indexes:
            if idx not in deduped_indexes:
                deduped_indexes.append(idx)
        return deduped_indexes, errors

    def _resolve_send_targets(
        self,
        sender_id: str,
        session,
        arg_text: str,
    ) -> tuple[list[str], list[str]]:
        tokens = self._tokenize_send_args(arg_text)
        if not tokens:
            return [], ["未提供可识别的文件参数。"]

        snapshot = self._send_file_list_cache.get(sender_id)
        max_index = len(snapshot["files"]) if snapshot else 0

        index_tokens: list[str] = []
        path_tokens: list[str] = []
        for token in tokens:
            if re.fullmatch(r"\d+", token) or re.fullmatch(r"\d+-\d+", token):
                index_tokens.append(token)
            else:
                path_tokens.append(token)

        resolved: list[str] = []
        errors: list[str] = []

        if index_tokens:
            if not snapshot:
                errors.append(
                    "未找到可用编号快照，请先执行一次 /oc-send 获取列表后再按序号发送。"
                )
            else:
                indexes, idx_errors = self._expand_index_tokens(index_tokens, max_index)
                errors.extend(idx_errors)
                for idx in indexes:
                    rel_path = snapshot["files"][idx - 1]
                    abs_path = os.path.abspath(
                        os.path.join(snapshot["work_dir"], rel_path)
                    )
                    resolved.append(abs_path)

        for token in path_tokens:
            candidate = os.path.expanduser(token)
            if self._is_absolute_like_path(candidate):
                abs_path = os.path.abspath(candidate)
            else:
                abs_path = os.path.abspath(os.path.join(session.work_dir, candidate))
            resolved.append(abs_path)

        deduped: list[str] = []
        for p in resolved:
            if p not in deduped:
                deduped.append(p)
        return deduped, errors

    async def initialize(self):
        """插件初始化"""
        # 配置 LLM 工具描述
        tool_mgr = self.context.get_llm_tool_manager()
        tool = tool_mgr.get_func("call_opencode")
        if tool:
            tool_cfg = self.config.get("tool_config", {})
            desc = tool_cfg.get("tool_description")
            if desc:
                tool.description = desc

            arg_desc = tool_cfg.get("arg_description")
            if (
                arg_desc
                and "properties" in tool.parameters
                and "task_description" in tool.parameters["properties"]
            ):
                tool.parameters["properties"]["task_description"]["description"] = (
                    arg_desc
                )

        # 配置输出处理器
        self.output_proc.set_html_render(self.html_render)
        self.output_proc.set_llm_functions(
            self.context.llm_generate, self.context.get_current_chat_provider_id
        )
        self.output_proc.set_template_dir(os.path.dirname(__file__))

        # 启动自动清理任务
        self.storage_mgr.start_auto_clean_task()

        # 运行模式健康检查
        ok, detail = await self.executor.health_check()
        mode_text = "服务器远程模式" if self.executor.is_remote_mode() else "本地模式"
        if ok:
            self.logger.info(
                f"OpenCode Plugin initialized. mode={mode_text}, detail={detail}"
            )
        else:
            self.logger.warning(
                f"OpenCode Plugin initialized with warning. mode={mode_text}, detail={detail}"
            )

    async def terminate(self):
        """插件卸载/停用时的清理"""
        await self.executor.close()
        await self.storage_mgr.stop_auto_clean_task()
        self.logger.info("OpenCode Plugin terminated.")

    # ==================== 命令处理器 ====================

    @filter.command("oc")
    async def oc_handler(self, event: AstrMessageEvent, message: str = ""):
        """调用 OpenCode 执行任务。用法：/oc [任务描述]。同一会话内的多次调用会保持对话上下文。"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        # 手动解析完整指令，保留空格和换行
        full_command = event.message_str.strip()
        parts = full_command.split(" ", 1)
        actual_message = parts[1].strip() if len(parts) > 1 else ""

        sender_id = event.get_sender_id()
        session = self.session_mgr.get_or_create_session(sender_id)

        # 统一预处理：下载图片、处理引用、合并文本
        final_message = await self.input_proc.process_input_message(
            event, session, actual_message
        )

        if not final_message:
            yield event.plain_result("请输入任务、发送图片或引用消息。")
            return

        if self.executor.is_remote_mode():
            local_refs = self._find_local_path_refs(final_message)
            if local_refs:
                yield event.plain_result(self._remote_input_guard_message(local_refs))
                return

        # 获取超时配置
        timeout = self.config.get("basic_config", {}).get("confirm_timeout", 30)

        if self.security.is_destructive(final_message):
            yield event.plain_result(
                f"⚠️ 敏感操作确认：'{final_message}'\n回复'确认'继续，其他取消 ({timeout}s)"
            )

            user_choice = asyncio.Event()
            approved = False

            @session_waiter(timeout=timeout)
            async def confirm(c: SessionController, e: AstrMessageEvent):
                nonlocal approved
                if e.message_str == "确认":
                    approved = True
                    user_choice.set()
                    c.stop()
                else:
                    user_choice.set()
                    c.stop()

            try:
                await confirm(event)
                await user_choice.wait()
            except TimeoutError:
                yield event.plain_result("超时取消")
                return

            if not approved:
                yield event.plain_result("已取消")
                return

            yield event.plain_result(self._render_exec_status(session))
            output = await self.executor.run_opencode(final_message, session)
            send_plan = await self.output_proc.parse_output_plan(output, event, session)
            for idx, components in enumerate(send_plan):
                if idx > 0:
                    await asyncio.sleep(self.output_proc.next_send_delay())
                yield event.chain_result(components)
            return

        yield event.plain_result(self._render_exec_status(session))
        output = await self.executor.run_opencode(final_message, session)
        send_plan = await self.output_proc.parse_output_plan(output, event, session)
        for idx, components in enumerate(send_plan):
            if idx > 0:
                await asyncio.sleep(self.output_proc.next_send_delay())
            yield event.chain_result(components)

    @filter.command("oc-shell")
    async def oc_shell(self, event: AstrMessageEvent, cmd: str = ""):
        """执行原生 Shell 命令。用法：/oc-shell [命令]"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        command = event.message_str.strip()
        parts = command.split(" ", 1)
        actual_cmd = parts[1].strip() if len(parts) > 1 else ""

        if not actual_cmd:
            yield event.plain_result("请输入要执行的 Shell 命令。")
            return

        if self.executor.is_remote_mode():
            yield event.plain_result(
                "❌ 当前为服务器远程模式，已禁用 /oc-shell。本地 Shell 仅在本地模式可用。"
            )
            return

        timeout = self.config.get("basic_config", {}).get("confirm_timeout", 30)

        if self.security.is_destructive(actual_cmd):
            yield event.plain_result(
                f"⚠️ Shell 敏感操作确认：'{actual_cmd}'\n回复'确认'继续，其他取消 ({timeout}s)"
            )

            user_choice = asyncio.Event()
            approved = False

            @session_waiter(timeout=timeout)
            async def confirm_shell(c: SessionController, e: AstrMessageEvent):
                nonlocal approved
                if e.message_str == "确认":
                    approved = True
                    user_choice.set()
                    c.stop()
                else:
                    user_choice.set()
                    c.stop()

            try:
                await confirm_shell(event)
                await user_choice.wait()
            except TimeoutError:
                yield event.plain_result("超时取消")
                return

            if not approved:
                yield event.plain_result("已取消")
                return

            yield event.plain_result(f"🚀 Shell 执行中: {actual_cmd}")
            result = await self.executor.exec_shell_cmd(actual_cmd)
            sender_id = event.get_sender_id()
            session = self.session_mgr.get_or_create_session(sender_id)
            send_plan = await self.output_proc.parse_output_plan(result, event, session)
            for idx, components in enumerate(send_plan):
                if idx > 0:
                    await asyncio.sleep(self.output_proc.next_send_delay())
                yield event.chain_result(components)
            return

        yield event.plain_result(f"🚀 Shell 执行中: {actual_cmd}")
        result = await self.executor.exec_shell_cmd(actual_cmd)
        sender_id = event.get_sender_id()
        session = self.session_mgr.get_or_create_session(sender_id)
        send_plan = await self.output_proc.parse_output_plan(result, event, session)
        for idx, components in enumerate(send_plan):
            if idx > 0:
                await asyncio.sleep(self.output_proc.next_send_delay())
            yield event.chain_result(components)

    @filter.command("oc-send")
    async def oc_send(self, event: AstrMessageEvent, path: str = ""):
        """发送文件。用法：/oc-send（列文件）| /oc-send 1,2 | /oc-send 相对路径/绝对路径"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        sender_id = event.get_sender_id()
        session = self.session_mgr.get_or_create_session(sender_id)
        arg_text = self._extract_oc_send_args(event, path)

        if not arg_text:
            snapshot = self._scan_workspace_files(session.work_dir)
            self._send_file_list_cache[sender_id] = snapshot
            yield event.plain_result(self._render_send_file_page(snapshot, page=1))
            return

        page_query = self._parse_send_page_query(arg_text)
        if page_query is not None:
            snapshot = self._send_file_list_cache.get(sender_id)
            if not snapshot:
                snapshot = self._scan_workspace_files(session.work_dir)
                self._send_file_list_cache[sender_id] = snapshot
            yield event.plain_result(
                self._render_send_file_page(snapshot, page=page_query)
            )
            return

        keyword = self._parse_send_find_query(arg_text)
        if keyword is not None:
            snapshot = self._scan_workspace_files(session.work_dir, keyword=keyword)
            self._send_file_list_cache[sender_id] = snapshot
            yield event.plain_result(self._render_send_file_page(snapshot, page=1))
            return

        resolved_paths, parse_errors = self._resolve_send_targets(
            sender_id, session, arg_text
        )
        valid_files: list[str] = []
        validation_errors: list[str] = []

        for target_path in resolved_paths:
            if not os.path.exists(target_path) or not os.path.isfile(target_path):
                validation_errors.append(f"不存在或不是文件: {target_path}")
                continue

            if not self.security.is_path_safe(target_path, session):
                validation_errors.append(f"不在允许目录范围内: {target_path}")
                continue

            valid_files.append(target_path)

        all_errors = parse_errors + validation_errors
        if not valid_files:
            if all_errors:
                lines = "\n".join([f"- {msg}" for msg in all_errors[:20]])
                yield event.plain_result(
                    "❌ 没有可发送的有效文件：\n"
                    f"{lines}\n\n"
                    "提示：先执行 /oc-send 查看编号，或改用明确的相对/绝对路径。"
                )
            else:
                yield event.plain_result("❌ 没有可发送的有效文件。")
            return

        try:
            components = [
                File(file=os.path.abspath(p), name=os.path.basename(p))
                for p in valid_files
            ]
            yield event.chain_result(components)

            if all_errors:
                lines = "\n".join([f"- {msg}" for msg in all_errors[:20]])
                yield event.plain_result(
                    f"✅ 已发送 {len(valid_files)} 个文件。\n"
                    f"⚠️ 另有 {len(all_errors)} 项未发送：\n{lines}"
                )
        except OSError as e:
            self.logger.error(f"文件发送失败 (权限或路径问题): {e}")
            yield event.plain_result(f"❌ 发送失败: {e}")
        except Exception as e:
            self.logger.error(f"文件发送失败: {e}")
            yield event.plain_result(f"❌ 发送失败: {e}")

    @filter.command("oc-new")
    async def oc_new(self, event: AstrMessageEvent, path: str = ""):
        """重置会话并切换工作目录。用法：/oc-new [路径]。会清除对话上下文，下次 /oc 开始全新对话。"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        sender_id = event.get_sender_id()
        target_path = path.strip() if path else None

        # 默认工作目录逻辑
        basic_cfg = self.config.get("basic_config", {})
        default_wd = basic_cfg.get("work_dir", "").strip()
        if not default_wd:
            default_wd = os.path.join(self.base_data_dir, "workspace")

        final_wd = default_wd

        if target_path:
            if not os.path.exists(target_path):
                yield event.plain_result(
                    f"⚠️ 目录不存在：{target_path}\n是否创建并使用此目录？(y/n, 30s超时)"
                )

                @session_waiter(timeout=30)
                async def confirm_path(c: SessionController, e: AstrMessageEvent):
                    if e.message_str.lower() in ["y", "yes", "确认", "是"]:
                        try:
                            os.makedirs(target_path, exist_ok=True)
                            await self._init_session(event, sender_id, target_path)
                            c.stop()
                        except Exception as ex:
                            await e.send(
                                e.plain_result(
                                    f"❌ 创建目录失败: {ex}\n已回退到默认目录。"
                                )
                            )
                            await self._init_session(event, sender_id, default_wd)
                            c.stop()
                    else:
                        await e.send(e.plain_result("已取消自定义路径，使用默认目录。"))
                        await self._init_session(event, sender_id, default_wd)
                        c.stop()

                try:
                    await confirm_path(event)
                except TimeoutError:
                    yield event.plain_result("超时，自动使用默认工作目录。")
                    await self._init_session(event, sender_id, default_wd)
                return
            else:
                final_wd = target_path

        await self._init_session(event, sender_id, final_wd)

    async def _init_session(self, event, sender_id, work_dir):
        """初始化会话的辅助函数"""
        self.session_mgr.delete_session(sender_id)

        if not os.path.exists(work_dir):
            try:
                os.makedirs(work_dir, exist_ok=True)
            except Exception:
                work_dir = os.getcwd()

        session = self.session_mgr.get_or_create_session(sender_id, work_dir)
        proxy_info = session.env.get("http_proxy", "无")
        mode_hint = "服务器远程模式" if self.executor.is_remote_mode() else "本地模式"
        work_dir_label = (
            "本地缓存目录" if self.executor.is_remote_mode() else "工作目录"
        )
        await event.send(
            event.plain_result(
                f"✅ 已启动 OpenCode 新会话\n🔌 运行模式: {mode_hint}\n📂 {work_dir_label}: {session.work_dir}\n🌐 代理环境: {proxy_info}"
            )
        )

    @filter.command("oc-end")
    async def oc_end(self, event: AstrMessageEvent):
        """清除当前对话上下文，但保留工作目录。下次 /oc 将开始新对话。"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        sender_id = event.get_sender_id()
        if self.session_mgr.delete_session(sender_id):
            yield event.plain_result("🚫 已结束当前会话。")
        else:
            yield event.plain_result("当前没有活跃的会话。")

    @filter.command("oc-clean")
    async def oc_clean(self, event: AstrMessageEvent):
        """手动清理临时文件"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        count, size_mb = await self.storage_mgr.clean_temp_files()
        yield event.plain_result(
            f"🧹 清理完成：共删除 {count} 个文件，释放 {size_mb:.2f} MB 空间。"
        )

    @filter.command("oc-history")
    async def oc_history(self, event: AstrMessageEvent):
        """查看工作目录使用历史。用法：/oc-history"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        history = self.storage_mgr.load_workdir_history()

        if not history:
            yield event.plain_result("📂 暂无工作目录使用历史。")
            return

        lines = ["📂 工作目录使用历史（最近10条）：\n"]
        for i, record in enumerate(history[:10], 1):
            path = record.get("path", "未知")
            last_used = record.get("last_used", "未知")
            used_count = record.get("used_count", 0)

            try:
                dt = datetime.fromisoformat(last_used)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = last_used

            lines.append(f"{i}. {path}")
            lines.append(f"   最后使用: {time_str} | 使用次数: {used_count}\n")

        yield event.plain_result("\n".join(lines))

    @filter.command("oc-session")
    async def oc_session(self, event: AstrMessageEvent, query: str = ""):
        """管理 OpenCode 会话。用法：/oc-session [序号/ID/标题]"""
        if not self.security.is_admin(event):
            yield event.plain_result("权限不足。")
            return

        sender_id = event.get_sender_id()
        query = query.strip()

        # 如果没有参数，列出最近 10 个 session
        if not query:
            sessions = await self.executor.list_opencode_sessions(limit=10)
            if not sessions:
                yield event.plain_result("📋 暂无 OpenCode 会话记录。")
                return

            lines = ["📋 OpenCode 会话列表（最近10个）：\n"]
            for i, s in enumerate(sessions, 1):
                session_id = s.get("id", "未知")
                title = s.get("title", "无标题")
                # 截断过长的标题
                if len(title) > 40:
                    title = title[:37] + "..."
                lines.append(f"{i}. {title}")
                lines.append(f"   ID: {session_id}\n")

            # 显示当前绑定的 session
            current_session = self.session_mgr.get_session(sender_id)
            if current_session and current_session.opencode_session_id:
                lines.append(f"📌 当前绑定: {current_session.opencode_session_id}")
            else:
                lines.append("📌 当前绑定: 无（下次 /oc 将创建新会话）")

            yield event.plain_result("\n".join(lines))
            return

        # 有参数：尝试切换到指定 session
        sessions = await self.executor.list_opencode_sessions(limit=50)
        target_session = None

        # 先检查是否为序号（1-10）
        if query.isdigit():
            index = int(query)
            if 1 <= index <= min(10, len(sessions)):
                target_session = sessions[index - 1]

        # 如果不是序号，尝试精确匹配 ID
        if not target_session:
            for s in sessions:
                if s.get("id") == query:
                    target_session = s
                    break

        # 如果 ID 没匹配到，尝试模糊匹配标题
        if not target_session:
            query_lower = query.lower()
            for s in sessions:
                title = s.get("title", "").lower()
                if query_lower in title:
                    target_session = s
                    break

        if not target_session:
            yield event.plain_result(
                f"❌ 未找到匹配的会话：{query}\n请使用 /oc-session 查看可用会话列表。"
            )
            return

        # 切换到目标 session
        session = self.session_mgr.get_or_create_session(sender_id)
        session.set_opencode_session_id(target_session["id"])

        yield event.plain_result(
            f"✅ 已切换到会话：\n"
            f"📝 标题: {target_session.get('title', '无标题')}\n"
            f"🔑 ID: {target_session['id']}"
        )

    # ==================== LLM 工具 ====================

    @filter.llm_tool(name="call_opencode")
    async def call_opencode_tool(
        self, event: AstrMessageEvent, task_description: str
    ) -> MessageEventResult:
        """在用户电脑上调用 OpenCode 等 AI 智能体 Agent 的工具。当用户有执行编程、处理文档等复杂任务的高级需求时，调用此工具。

        Args:
            task_description(string): 详细的任务描述。保持原意，允许适当编辑以提升精准度，也可以不修改。此参数会被传送给 OpenCode 作为输入。
        """
        if not self.security.is_admin(event):
            await event.send(event.plain_result("权限不足。"))
            return

        sender_id = event.get_sender_id()
        session = self.session_mgr.get_or_create_session(sender_id)

        final_task = await self.input_proc.process_input_message(
            event, session, task_description
        )

        if self.executor.is_remote_mode():
            local_refs = self._find_local_path_refs(final_task)
            if local_refs:
                await event.send(
                    event.plain_result(self._remote_input_guard_message(local_refs))
                )
                return

        # 敏感操作需要用户确认
        if self.security.is_destructive(final_task):
            timeout = self.config.get("basic_config", {}).get("confirm_timeout", 30)
            await event.send(
                event.plain_result(
                    f"⚠️ AI 请求敏感操作：'{final_task}'\n回复'确认执行'批准 ({timeout}s)"
                )
            )

            user_choice = asyncio.Event()
            approved = False

            @session_waiter(timeout=timeout)
            async def tool_confirm(c: SessionController, e: AstrMessageEvent):
                nonlocal approved
                if e.message_str == "确认执行":
                    approved = True
                    user_choice.set()
                    c.stop()
                else:
                    user_choice.set()
                    c.stop()

            try:
                await tool_confirm(event)
                await user_choice.wait()
            except TimeoutError:
                await event.send(event.plain_result("超时拒绝"))
                return

            if not approved:
                await event.send(event.plain_result("拒绝执行"))
                return

        # 发送"执行中"状态，然后在后台执行，避免框架 60s 超时
        await event.send(event.plain_result(self._render_exec_status(session)))

        # 保存主动推送所需的信息
        umo = event.unified_msg_origin

        # 启动后台任务执行 OpenCode
        asyncio.create_task(
            self._execute_opencode_background(umo, final_task, session, event)
        )

        # 不 yield 任何内容，框架会认为工具已自行处理，AI 不再额外回复

    async def _execute_opencode_background(
        self,
        umo: str,
        task: str,
        session,
        event: AstrMessageEvent,
    ):
        """后台执行 OpenCode 任务并主动推送结果"""
        from astrbot.api.event import MessageChain

        try:
            output = await self.executor.run_opencode(task, session)
            send_plan = await self.output_proc.parse_output_plan(output, event, session)

            for idx, components in enumerate(send_plan):
                if idx > 0:
                    await asyncio.sleep(self.output_proc.next_send_delay())

                message_chain = MessageChain()
                for comp in components:
                    message_chain.chain.append(comp)
                await self.context.send_message(umo, message_chain)
        except Exception as e:
            self.logger.error(f"OpenCode 后台执行失败: {e}")
            try:
                await self.context.send_message(
                    umo, MessageChain().message(f"❌ OpenCode 执行失败: {e}")
                )
            except Exception as send_err:
                self.logger.error(f"发送错误消息失败: {send_err}")
