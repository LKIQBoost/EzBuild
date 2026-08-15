"""Writer 抽象基类。

新增输出格式的方法：
    1. 在 ``ezbuild/writers/`` 下新建 ``xxx.py``；
    2. 定义 ``class XxxWriter(Writer)``，设置 format_name / extensions；
    3. 实现 ``render()``（返回 str 或 bytes）以及可选的 ``write()``；
    4. 完成 —— 子类会自动注册，所有输入格式都能输出到它。
"""

from __future__ import annotations

import abc
import os
from pathlib import Path
from typing import Union

from .. import registry
from ..model import Building


class Writer(abc.ABC):
    #: 格式唯一名，如 "cmd_json" / "txt" / "ibi"
    format_name: str = ""
    #: 输出文件扩展名，如 (".json",)，用于按输出文件扩展名自动推断格式
    extensions: tuple[str, ...] = ()
    #: 人类可读描述
    description: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.format_name:
            registry.register_writer(cls)

    @abc.abstractmethod
    def render(self, building: Building) -> Union[str, bytes]:
        """把建筑渲染为输出内容（str 为文本，bytes 为二进制）。"""
        raise NotImplementedError

    def write(self, building: Building, path: str | Path) -> Path:
        """把渲染结果写入文件（自动创建父目录）。"""
        content = self.render(building)
        path = Path(path)
        os.makedirs(path.parent, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
        with open(path, mode, **kwargs) as f:
            f.write(content)
        return path
