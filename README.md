# AstrBot Plugin: OpenCode Bridge

让 AstrBot 对接 [OpenCode](https://github.com/anomalyco/opencode)，通过自然语言远程操控电脑。
本项目使用 OpenCode 构建。

使用过程中若有问题或宝贵意见，欢迎发布issues、提交PR！

## 功能特性

- **自然语言控制**: 通过聊天消息直接操作宿主电脑
- **多模态输入**: 支持图片、文件、引用消息作为任务上下文
- **AI 自动调用**: 注册为 LLM Function Tool，对话中自动触发
- **多种输出模式**: 文本摘要、长图渲染、TXT 文件、合并转发
- **安全机制**: 管理员权限、敏感操作确认、可配置关键词拦截、路径安全检查
- **会话隔离**: 每个用户独立工作目录和环境变量
- **会话持久化**: 多次 `/oc` 命令自动保持上下文，AI 记住之前的对话
- **会话管理**: 支持查看、切换历史会话，方便回溯和继续之前的工作
- **历史记录**: 自动记录使用过的工作目录，方便回溯

## 安装

### 前置条件

1. 安装 [OpenCode CLI](https://opencode.ai) 并确保终端可运行 `opencode` 命令
2. AstrBot v4.5.7 或更高版本

### 安装步骤

1. 在 AstrBot WebUI 插件市场搜索 `opencode` 安装
2. 或手动将插件文件夹放入 `data/plugins/` 目录
3. 重启 AstrBot 并在管理面板启用插件

## 指令

| 指令 | 说明 | 示例 |
|------|------|------|
| `/oc <任务>` | 执行自然语言任务（自动保持对话上下文） | `/oc 查看当前目录下的文件` |
| `/oc-new [路径]` | 重置会话并切换工作目录（清除对话上下文） | `/oc-new D:\\Projects` |
| `/oc-end` | 仅清除对话上下文（保留当前工作目录） | `/oc-end` |
| `/oc-session [ID]` | 查看、切换 OpenCode 会话 | `/oc-session`、`/oc-session [序号/ID/标题]` |
| `/oc-shell <命令>` | 执行原生 Shell 命令 | `/oc-shell dir` |
| `/oc-send <路径>` | 发送服务器上的文件（带路径安全检查） | `/oc-send C:\\log.txt` |
| `/oc-clean` | 手动清理临时文件 | `/oc-clean` |
| `/oc-history` | 查看工作目录使用历史 | `/oc-history` |

### 会话管理说明

插件会自动维护与 OpenCode 的会话上下文：

- **`/oc`**: 首次执行时创建新会话，后续执行复用同一会话，AI 会记住之前的对话内容
- **`/oc-new`**: 完全重置，创建新会话 + 切换工作目录
- **`/oc-end`**: 仅清除当前会话 ID，下次 `/oc` 将创建新会话，但工作目录不变
- **`/oc-session`**: 
  - 无参数：列出最近 10 个会话（显示标题和 ID）
  - 传入阿拉伯数字序号：切换到指定会话
  - 传入 ID：切换到指定会话
  - 传入标题关键词：模糊匹配并切换

## 配置项

在 AstrBot WebUI 中配置：

### 基础配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `only_admin` | 仅管理员可用 | `true` |
| `opencode_path` | OpenCode 可执行文件路径 | `opencode` |
| `work_dir` | 默认工作目录 | (插件数据目录) |
| `proxy_url` | HTTP 代理地址 | (空) |
| `destructive_keywords` | 敏感操作关键词 (正则) | `删除`, `rm`, `delete` 等 |
| `confirm_all_write_ops` | 写操作需确认 | `true` |
| `check_path_safety` | 文件路径安全检查 | `false` |
| `auto_clean_interval` | 自动清理间隔 (分钟) | `60` |

### 输出配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `output_modes` | 输出方式 (多选) | `last_line`, `txt_file` |
| `max_text_length` | 长文本阈值 | `1000` |

**可选输出模式**:
- `last_line`: 显示文本 (长文本自动截断首尾)
- `ai_summary`: AI 智能摘要
- `txt_file`: 生成 TXT 文件
- `long_image`: 渲染为代码风格长图
- `forward_msg`: 合并转发消息

### LLM 工具配置

| 配置项 | 说明 |
|--------|------|
| `tool_description` | Function Tool 描述 (影响 AI 何时调用) |
| `arg_description` | 参数描述 |

## 使用场景

### 场景一: 文件操作
```
用户: /oc 把桌面上所有 PDF 移动到"文档"文件夹
机器人: 🚀 执行中...
机器人: ✅ 已移动 5 个文件
```

### 场景二: 图片处理
```
用户: /oc 把这张图转成黑白 [附带图片]
机器人: ✅ 处理完成，已保存至 output.png
```

### 场景三: AI 自动调用
```
用户: 帮我看看系统内存占用多少
机器人: (自动调用 call_opencode)
机器人: 当前内存使用率 67%，可用 8.2GB
```

## 安全说明

本插件赋予机器人对宿主电脑的操作权限，请注意：

1. **保持 `only_admin` 为 `true`**
2. **敏感操作会要求二次确认** (如删除、格式化等)
3. **建议在隔离环境运行** (如 Docker 容器)
4. **定期检查日志** 确保无异常操作
5. **路径安全检查**: `/oc-send` 等命令会检查文件路径是否在允许的范围内，防止误操作访问敏感系统文件

## 工作目录历史

插件会自动记录所有使用过的工作目录到 `data/plugin_data/astrbot_plugin_opencode/workdir_history.json`。

历史记录包含：
- 路径
- 首次使用时间
- 最后使用时间
- 使用次数
- 使用者ID

使用 `/oc-history` 可查看最近使用的10个工作目录。

## 用例模拟

<details>
<summary>点击展开：插件交互用例模拟 (Use Cases)</summary>

### 场景 1：自然语言文件操作 (LLM Tool 调用)
*用户在聊天中直接提出需求，插件自动识别意图并调用 OpenCode 执行。*

**用户**：帮我在桌面上创建一个名为 "周报" 的文件夹，并在里面新建一个 "todo.txt"，内容写上 "下周一开会"。
**AstrBot**：(识别到意图，自动调用 `call_opencode`)
**AstrBot**：
> 🚀 执行中...
> 📂 D:\AstrBot\data\plugin_data\astrbot_plugin_opencode\workspace
> 
> OpenCode 输出：
> 1. Created directory: C:\Users\Admin\Desktop\周报
> 2. Created file: C:\Users\Admin\Desktop\周报\todo.txt
> 3. Content written to todo.txt

---

### 场景 2：基于图片的代码生成 (多模态)
*用户发送图片并要求写代码，插件下载图片并传给 OpenCode 进行分析。*

**用户**：[发送一张网页截图]
**用户**：/oc 用 HTML 和 CSS 复刻这个网页布局，保存为 index.html
**AstrBot**：
> 🚀 执行中...
> 📂 ...\workspace
> 
> OpenCode 输出：
> Analyzing image...
> Generating HTML structure...
> Writing to index.html...
> Done.

---

### 场景 3：直接执行 Shell 命令 (oc-shell)
*管理员需要快速查看系统状态或执行特定命令。*

**用户**：/oc-shell dir /w
**AstrBot**：🚀 Shell 执行中: dir /w
**AstrBot**：
> 输出:
> 驱动器 D 中的卷是 Data
> 卷的序列号是 1234-5678
>
> D:\AstrBot\data\plugin_data\astrbot_plugin_opencode\workspace 的目录
>
> [.]              [..]             [downloaded]     index.html       test.py
>                2 个文件          2,048 字节
>                3 个目录     50,000,000,000 可用字节
>
> (Return Code: 0)

---

### 场景 4：敏感操作安全拦截
*用户尝试执行危险操作，插件自动触发二次确认。*

**用户**：/oc 删除 D盘所有文件
**AstrBot**：
> ⚠️ 敏感操作确认：'删除 D盘所有文件'
> 回复'确认'继续，其他取消 (30s)

**用户**：确认
**AstrBot**：执行中...
**AstrBot**：(执行删除操作并返回结果)

---

### 场景 5：远程文件获取 (oc-send)
*用户想要获取服务器上的特定文件。*

**用户**：/oc-send D:\Projects\config.yaml
**AstrBot**：[发送文件: config.yaml]

*如果尝试获取非法路径：*
**用户**：/oc-send C:\Windows\System32\cmd.exe
**AstrBot**：
> ⚠️ 安全警告：该文件不在允许的工作目录范围内。
> 文件路径: C:\Windows\System32\cmd.exe
> 允许的目录包括：...

---

### 场景 6：长文本/大量日志处理
*当命令输出非常长时，插件会自动处理以防止刷屏。*

**用户**：/oc-shell type big_log.txt
**AstrBot**：(检测到输出超过 1000 字符)
> 2023-10-01 10:00:00 [INFO] Start...
> ...
> (中间省略 5000 字符)
> ...
> 2023-10-01 10:00:05 [INFO] End.
>
> [文件: opencode_output_1696123456.txt] (完整日志已保存为文件)

---

### 场景 7：会话持久化与管理
*多次 /oc 命令自动保持上下文，AI 记住之前的对话。*

**用户**：/oc 创建一个 Python 项目结构
**AstrBot**：✅ 已创建项目结构：main.py, utils/, tests/...

**用户**：/oc 在刚才创建的 main.py 里写一个 Hello World
**AstrBot**：✅ 已写入 main.py（AI 记住了上一条消息的上下文）

**用户**：/oc-session
**AstrBot**：
> 📋 最近的 OpenCode 会话：
> 1. `ses_abc123` - Python 项目创建
> 2. `ses_def456` - 文件整理任务
> ...

**用户**：/oc-session ses_def456
**AstrBot**：✅ 已切换到会话: ses_def456

**用户**：/oc-new D:\NewProject
**AstrBot**：✅ 已重置会话并切换工作目录到 D:\NewProject

</details>
