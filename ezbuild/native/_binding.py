"""ezbuild 原生 DLL 的 ctypes 绑定。

参考 ``water_structure/_binding.py`` 的 C ABI（``ws_*`` 函数），但做了两点改造：

- **惰性加载**：找不到 DLL 时不在 import 时报错，而是让 :func:`load` 抛异常。
  这样 ``import ezbuild.native`` 永远成功，CLI 可以检测 DLL 是否可用，
  不可用时自动回退 Python 原生转换（``-c`` 只是"尽量快"）。
- **无 GIL 依赖**：转换全程在 C++ 内完成——不建 Python Building 模型、
  不逐方块跑 Python 循环，DLL 内部并行（``threads``）用的是原生线程，
  不受解释器锁约束。这本身就突破 Python 的性能限制与 GIL。

DLL 搜索顺序：环境变量 ``WATER_STRUCTURE_LIBRARY`` → 包内自带
``water_structure_shared.dll``（``ezbuild/native/``）。运行时映射 assets 默认
取包内 ``assets/`` 目录。
"""

from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple, Union

PathValue = Union[str, os.PathLike[str]]


class Error(RuntimeError):
    """原生 DLL 报告错误时抛出。"""


class StructureInfo(NamedTuple):
    format_id: int
    width: int
    height: int
    length: int
    offset_x: int
    offset_y: int
    offset_z: int
    non_air_blocks: int


class _CStructureInfo(ctypes.Structure):
    _fields_ = [
        ("format_id", ctypes.c_uint8),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("length", ctypes.c_int32),
        ("offset_x", ctypes.c_int32),
        ("offset_y", ctypes.c_int32),
        ("offset_z", ctypes.c_int32),
        ("non_air_blocks", ctypes.c_uint64),
    ]


# ---------------------------------------------------------------------------
# 惰性加载
# ---------------------------------------------------------------------------
_lib: Optional[ctypes.CDLL] = None
_load_error: Optional[Exception] = None


def _library_candidates() -> List[Path]:
    configured = os.environ.get("WATER_STRUCTURE_LIBRARY")
    package = Path(__file__).resolve().parent
    candidates = [package / "water_structure_shared.dll"]
    if configured:
        candidates.insert(0, Path(configured).expanduser())
    return candidates


def _setup_lib(lib: ctypes.CDLL) -> None:
    """给 C 函数标注 argtypes/restype（只做一次）。"""
    lib.ws_version.argtypes = []
    lib.ws_version.restype = ctypes.c_char_p
    lib.ws_abi_version.argtypes = []
    lib.ws_abi_version.restype = ctypes.c_uint32
    lib.ws_context_create.argtypes = [ctypes.c_char_p]
    lib.ws_context_create.restype = ctypes.c_void_p
    lib.ws_context_destroy.argtypes = [ctypes.c_void_p]
    lib.ws_context_destroy.restype = None
    lib.ws_last_error.argtypes = [ctypes.c_void_p]
    lib.ws_last_error.restype = ctypes.c_char_p
    lib.ws_reader_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.ws_reader_open.restype = ctypes.c_void_p
    lib.ws_reader_close.argtypes = [ctypes.c_void_p]
    lib.ws_reader_close.restype = None
    lib.ws_reader_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CStructureInfo)]
    lib.ws_reader_info.restype = ctypes.c_int
    lib.ws_reader_format.argtypes = [ctypes.c_void_p]
    lib.ws_reader_format.restype = ctypes.c_char_p
    lib.ws_convert.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint64,
    ]
    lib.ws_convert.restype = ctypes.c_int
    lib.ws_to_world.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_int32,
    ]
    lib.ws_to_world.restype = ctypes.c_int


def load() -> ctypes.CDLL:
    """加载 DLL；失败时抛 ImportError（结果缓存）。"""
    global _lib, _load_error
    if _lib is not None:
        return _lib
    if _load_error is not None:
        raise _load_error
    if platform.system() != "Windows":
        _load_error = ImportError(
            "原生 DLL 目前只发布 Windows 版（water-structure）"
        )
        raise _load_error
    errors: List[str] = []
    for candidate in _library_candidates():
        if not candidate.is_file():
            continue
        try:
            lib = ctypes.CDLL(str(candidate))
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        _setup_lib(lib)
        _lib = lib
        return _lib
    detail = "; ".join(errors) if errors else "包内 DLL 缺失（ezbuild/native/water_structure_shared.dll）"
    _load_error = ImportError(
        "无法加载 ezbuild 原生 DLL（" + detail + "）。"
        "请确认已随包附带 DLL，或用环境变量 WATER_STRUCTURE_LIBRARY 指向兼容的 DLL。"
    )
    raise _load_error


def version() -> str:
    """返回自带 DLL 的版本号。"""
    value = load().ws_version()
    return value.decode("utf-8", "replace") if value else "unknown"


def abi_version() -> int:
    """返回原生 C ABI 版本号。"""
    return int(load().ws_abi_version())


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
class Context:
    """持有 inspect / convert 操作使用的运行时注册表。"""

    def __init__(self, assets_directory: Optional[PathValue] = None):
        if assets_directory is None:
            assets_directory = Path(__file__).resolve().parent / "assets"
        self._handle = load().ws_context_create(os.fsencode(assets_directory))
        if not self._handle:
            raise Error("failed to create native context")

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            load().ws_context_destroy(handle)
            self._handle = None

    def __enter__(self) -> "Context":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _require_open(self) -> ctypes.c_void_p:
        if not self._handle:
            raise Error("native context is closed")
        return self._handle

    def _error(self) -> str:
        value = load().ws_last_error(self._require_open())
        return value.decode("utf-8", "replace") if value else "unknown error"

    def inspect(self, path: PathValue) -> StructureInfo:
        """检查一个结构文件（不转换）。"""
        reader = load().ws_reader_open(self._require_open(), os.fsencode(path), 0)
        if not reader:
            raise Error(self._error())
        try:
            raw = _CStructureInfo()
            if not load().ws_reader_info(reader, ctypes.byref(raw)):
                raise Error(self._error())
            return StructureInfo(*(getattr(raw, field) for field, _ in raw._fields_))
        finally:
            load().ws_reader_close(reader)

    def format(self, path: PathValue) -> str:
        """返回检测到的格式名。"""
        reader = load().ws_reader_open(self._require_open(), os.fsencode(path), 0)
        if not reader:
            raise Error(self._error())
        try:
            value = load().ws_reader_format(reader)
            if not value:
                raise Error(self._error())
            return value.decode("utf-8", "replace")
        finally:
            load().ws_reader_close(reader)

    def convert(
        self,
        input_path: PathValue,
        target_format: str,
        output_path: PathValue,
        *,
        threads: int = 0,
    ) -> None:
        """把一个结构文件转换到目标格式（全程在 C++ 内完成，无 GIL）。"""
        if threads < 0:
            raise ValueError("threads must be >= 0")
        if not load().ws_convert(
            self._require_open(),
            os.fsencode(input_path),
            target_format.encode("utf-8"),
            os.fsencode(output_path),
            threads,
        ):
            raise Error(self._error())

    def to_world(
        self,
        input_path: PathValue,
        world_path: PathValue,
        *,
        start: Tuple[int, int, int] = (0, -4, 0),
    ) -> None:
        """把一个结构流式写入世界目录或 .mcworld 压缩包。"""
        if len(start) != 3:
            raise ValueError("start must contain x, subchunk-y, and z")
        if not load().ws_to_world(
            self._require_open(),
            os.fsencode(input_path),
            os.fsencode(world_path),
            int(start[0]),
            int(start[1]),
            int(start[2]),
        ):
            raise Error(self._error())
