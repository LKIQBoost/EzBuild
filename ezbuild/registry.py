"""Reader / Writer 注册表。

- 任何 Reader 子类定义时通过 ``__init_subclass__`` 自动注册（见 readers/base.py）。
- 任何 Writer 子类同理自动注册（见 writers/base.py）。
- 新增一种格式 = 在 readers/ 或 writers/ 目录下新建一个文件、定义子类，
  其余代码零改动（模块会被自动发现并 import）。

本模块不依赖 readers/writers 的具体实现，避免循环导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pathlib import Path

    from .readers.base import Reader
    from .writers.base import Writer

# format_name -> 类（未实例化）
_READERS: dict[str, type["Reader"]] = {}
_WRITERS: dict[str, type["Writer"]] = {}

# 扩展名（小写，含点）-> format_name
_READER_EXT_TO_FORMAT: dict[str, str] = {}
_WRITER_EXT_TO_FORMAT: dict[str, str] = {}


def register_reader(cls: type["Reader"]) -> type["Reader"]:
    """注册一个 Reader 类。"""
    if cls.format_name in _READERS:
        raise ValueError(f"Reader 格式重复注册: {cls.format_name!r}")
    _READERS[cls.format_name] = cls
    for ext in cls.extensions:
        _READER_EXT_TO_FORMAT[ext.lower()] = cls.format_name
    return cls


def register_writer(cls: type["Writer"]) -> type["Writer"]:
    """注册一个 Writer 类。"""
    if cls.format_name in _WRITERS:
        raise ValueError(f"Writer 格式重复注册: {cls.format_name!r}")
    _WRITERS[cls.format_name] = cls
    for ext in cls.extensions:
        _WRITER_EXT_TO_FORMAT[ext.lower()] = cls.format_name
    return cls


def get_reader(format_name: str) -> type["Reader"] | None:
    return _READERS.get(format_name)


def get_writer(format_name: str) -> type["Writer"] | None:
    return _WRITERS.get(format_name)


def format_for_path(path: str | "Path") -> str | None:
    """根据文件扩展名推断输入（Reader）格式。"""
    return _match_ext(str(path), _READER_EXT_TO_FORMAT)


def writer_format_for_path(path: str | "Path") -> str | None:
    """根据文件扩展名推断输出（Writer）格式。"""
    return _match_ext(str(path), _WRITER_EXT_TO_FORMAT)


def _match_ext(path: str, mapping: dict[str, str]) -> str | None:
    path = path.lower()
    for suffix, fmt in mapping.items():
        if path.endswith(suffix):
            return fmt
    return None


def list_readers() -> list[str]:
    return sorted(_READERS)


def list_writers() -> list[str]:
    return sorted(_WRITERS)


def get_all_extensions() -> list[str]:
    """所有 Reader 支持的扩展名（去重）。"""
    seen: set[str] = set()
    for cls in _READERS.values():
        seen.update(cls.extensions)
    return sorted(seen)
