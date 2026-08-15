"""ezbuild —— Minecraft 建筑/音乐文件格式转换工具核心库。

使用示例::

    import ezbuild
    building = ezbuild.convert_read("建筑.bdx")            # 读取建筑
    ezbuild.convert_write(building, "输出.json", "cmd_json")  # 输出命令方块 JSON
    song = ezbuild.convert_read("歌曲.mid")               # 读取 MIDI（音乐）
    ezbuild.convert_write(song, "歌曲.nbs", "nbs")        # 输出 NBS

框架结构:
    - ``model``     中立数据模型（Building / Block / CommandBlock）
    - ``song``      中立音乐模型（Song / Note / Layer）
    - ``readers``   输入格式解析器（自动注册）
    - ``writers``   输出格式渲染器（自动注册）
    - ``registry``  格式注册表与按扩展名分发
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import model, registry
from .model import Block, Building, CommandBlock
from .song import Layer, Note, Song
from .music_builder import song_to_building

# 触发 readers / writers 子模块自动注册
from . import readers, writers  # noqa: E402,F401
from .streaming import (  # noqa: E402
    schem_to_txt,
    schematic_to_cmd_json,
    schematic_to_ibi,
    schematic_to_mcstructure,
    schematic_to_schem,
    schematic_to_txt,
)

__version__ = "0.4.0"

__all__ = [
    "model",
    "registry",
    "readers",
    "writers",
    "Block",
    "Building",
    "CommandBlock",
    "Song",
    "Note",
    "Layer",
    "song_to_building",
    "schem_to_txt",
    "schematic_to_txt",
    "schematic_to_ibi",
    "schematic_to_mcstructure",
    "schematic_to_schem",
    "schematic_to_cmd_json",
    "convert_read",
    "convert_write",
    "auto_convert",
]

# 音乐格式名（Song 直接写出，不转建筑）
MUSIC_FORMATS = frozenset({"mid", "nbs"})


def _coerce_for_write(model, format_name: str | None):
    """把 Song 自动转成建筑（命令方块音乐机），当目标格式需要 Building 时。"""
    if isinstance(model, Song) and format_name not in MUSIC_FORMATS:
        return song_to_building(model)
    return model


def convert_read(source: Union[str, Path, bytes]) -> Building | Song:
    """按扩展名自动识别输入格式并读取建筑或歌曲。

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


def convert_read_from(
    source: Union[str, Path, bytes], format_name: str
) -> Building | Song:
    """用指定格式读取建筑或歌曲。"""
    cls = registry.get_reader(format_name)
    if cls is None:
        raise ValueError(
            f"未知输入格式 {format_name!r}，可用: {registry.list_readers()}"
        )
    return cls().read(source)


def convert_write(
    model: Building | Song,
    path: Union[str, Path],
    format_name: str | None = None,
) -> Path:
    """把建筑或歌曲写入文件。

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
    model = _coerce_for_write(model, fmt)
    return cls().write(model, path)


def auto_convert(
    source: Union[str, Path, bytes],
    output_path: Union[str, Path],
    to_format: str | None = None,
) -> Path:
    """一键转换：读取 → 输出。"""
    model = convert_read(source)
    return convert_write(model, output_path, to_format)
