"""ezbuild —— Minecraft 建筑文件格式转换工具核心库。

使用示例::

    import ezbuild
    building = ezbuild.convert_read("建筑.bdx")            # 读取建筑
    ezbuild.convert_write(building, "输出.json", "cmd_json")  # 输出命令方块 JSON

框架结构:
    - ``model``     中立数据模型（Building / Block / CommandBlock）
    - ``readers``   输入格式解析器（自动注册）
    - ``writers``   输出格式渲染器（自动注册）
    - ``registry``  格式注册表与按扩展名分发
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import model, registry
from .model import Block, Building, CommandBlock

# 触发 readers / writers 子模块自动注册
from . import readers, writers  # noqa: E402,F401

__version__ = "0.1.0"

__all__ = [
    "model",
    "registry",
    "readers",
    "writers",
    "Block",
    "Building",
    "CommandBlock",
    "convert_read",
    "convert_write",
    "auto_convert",
]


def convert_read(source: Union[str, Path, bytes]) -> Building:
    """按扩展名自动识别输入格式并读取建筑。

    ``source`` 可以是文件路径或原始字节（字节时需额外指定格式，
    见 :func:`convert_read_from`）。
    """
    fmt = registry.format_for_path(source) if not isinstance(source, bytes) else None
    if fmt is None:
        raise ValueError(
            f"无法根据输入推断格式: {source!r}，"
            f"支持的输入格式: {registry.list_readers()}"
        )
    return convert_read_from(source, fmt)


def convert_read_from(source: Union[str, Path, bytes], format_name: str) -> Building:
    """用指定格式读取建筑。"""
    cls = registry.get_reader(format_name)
    if cls is None:
        raise ValueError(
            f"未知输入格式 {format_name!r}，可用: {registry.list_readers()}"
        )
    return cls().read(source)


def convert_write(
    building: Building, path: Union[str, Path], format_name: str | None = None
) -> Path:
    """把建筑写入文件。

    未指定 ``format_name`` 时按输出文件扩展名推断；
    二者都没有匹配项时抛错。
    """
    fmt = format_name or registry.writer_format_for_path(path)
    if fmt is None:
        raise ValueError(
            f"无法推断输出格式（指定 --to 或使用已知扩展名）: {path!r}"
        )
    cls = registry.get_writer(fmt)
    if cls is None:
        raise ValueError(f"未知输出格式 {fmt!r}，可用: {registry.list_writers()}")
    return cls().write(building, path)


def auto_convert(
    source: Union[str, Path, bytes],
    output_path: Union[str, Path],
    to_format: str | None = None,
) -> Path:
    """一键转换：读取 → 输出。"""
    building = convert_read(source)
    return convert_write(building, output_path, to_format)
