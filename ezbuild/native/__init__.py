"""ezbuild 原生 C++ DLL 桥接（``-c`` 快速转换路径）。

这个包把 WaterStructure 的 ``water_structure_shared.dll`` 连同运行时映射
assets 一起随 ezbuild 分发，让 CLI 的 ``-c/--cpp`` 直接把整个转换丢给 C++
完成——不建 Python Building 模型、不逐方块跑 Python 循环，DLL 内部线程
不受解释器锁约束，从而突破 Python 的性能限制与 GIL。

- DLL 缺失时 ``is_available()`` 返回 False，``-c`` 会自动回退 Python 原生转换。
- DLL 搜索顺序：环境变量 ``WATER_STRUCTURE_LIBRARY`` → 包内自带 DLL。

用法::

    import ezbuild.native as native

    if native.is_available():
        with native.Context() as ctx:
            ctx.convert("建筑.schem", "MCStructure", "建筑.mcstructure", threads=0)
"""

from __future__ import annotations

import os
from typing import Union

from . import _binding

PathValue = Union[str, "os.PathLike[str]"]

__all__ = [
    "Context",
    "Error",
    "StructureInfo",
    "is_available",
    "not_found_reason",
    "version",
    "abi_version",
]

#: ezbuild 格式名 -> DLL writer 名（``-c`` 快速路径支持的全部输出格式）。
#: 其中 schem/mcstructure/ibi/txt 与 ezbuild 自带 Writer 语义重叠（DLL 失败
#: 可回退 Python；txt 的 DLL 输出是 MCFunction——绝对坐标指令文件，与本库
#: txt 读取器互读），其余为 DLL 独有格式（无 -c 时不可用）。
DLL_WRITERS: dict[str, str] = {
    "schem": "SchemV2",
    "mcstructure": "MCStructure",
    "ibi": "IBImport",
    "txt": "MCFunction",
    "bdx": "BDX",
    "schematic": "Schematic",
    "litematic": "Litematic",
    "mcfn": "MCFunction",
    "axiombp": "AxiomBP",
    "fuhong": "FuHongV5",
}

#: 有 Python 原生实现的格式（DLL 失败时回退 Python 的集合）。
PYTHON_FALLBACK: frozenset[str] = frozenset({"schem", "mcstructure", "ibi", "txt"})

#: 与 ezbuild 自带 Writer 重叠、同时存在 Python 实现的格式。
DLL_ONLY_FORMATS: frozenset[str] = frozenset(DLL_WRITERS) - PYTHON_FALLBACK

Context = _binding.Context
Error = _binding.Error
StructureInfo = _binding.StructureInfo


def is_available() -> bool:
    """DLL 是否可加载（可用即返回 True，不抛异常）。"""
    try:
        _binding.load()
        return True
    except Exception:
        return False


def not_found_reason() -> str:
    """DLL 不可用时的原因描述（可用时返回空串）。"""
    try:
        _binding.load()
        return ""
    except Exception as exc:
        return str(exc)


def version() -> str:
    return _binding.version()


def abi_version() -> int:
    return _binding.abi_version()
