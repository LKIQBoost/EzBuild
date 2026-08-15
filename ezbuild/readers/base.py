"""Reader 抽象基类。

新增输入格式的方法：
    1. 在 ``ezbuild/readers/`` 下新建 ``xxx.py``；
    2. 定义 ``class XxxReader(Reader)``，设置 format_name / extensions，
       实现 ``read()`` 返回 :class:`~ezbuild.model.Building`；
    3. 完成 —— 子类会自动注册，CLI 与其它输出格式立刻可用。
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Union

from .. import registry
from ..model import Building

Source = Union[str, Path, bytes]


class Reader(abc.ABC):
    #: 格式唯一名，如 "bdx" / "mcstructure"
    format_name: str = ""
    #: 支持的扩展名，如 (".bdx",)，用于按扩展名自动识别
    extensions: tuple[str, ...] = ()
    #: 人类可读描述
    description: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.format_name:
            registry.register_reader(cls)

    @abc.abstractmethod
    def read(self, source: Source) -> Building:
        """把输入（文件路径或字节）解析为 :class:`Building`。"""
        raise NotImplementedError

    @staticmethod
    def _read_bytes(source: Source) -> bytes:
        """统一把路径/字节转为 bytes。"""
        if isinstance(source, (bytes, bytearray)):
            return bytes(source)
        with open(source, "rb") as f:
            return f.read()
