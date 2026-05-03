"""
通用工具函数模块
"""


def write_file_sync(filepath: str, data: bytes) -> None:
    """同步写入二进制文件，供 asyncio.to_thread 调用"""
    with open(filepath, "wb") as f:
        f.write(data)


def write_text_file_sync(filepath: str, text: str) -> None:
    """同步写入文本文件，供 asyncio.to_thread 调用"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text)
