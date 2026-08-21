"""DLL 独有输出格式的占位 Writer（bdx / litematic / mcfn / schematic / axiombp / fuhong）。

这些格式没有 Python 实现，只能由 C++ DLL（``ezbuild.native``）输出。
占位 Writer 仅用于注册（让 ``-l`` 列出、校验格式名、推断输出扩展名）；
真正写文件在 CLI 的 ``-c`` 路径里由 DLL 完成。没有 ``-c`` 时调用会抛错。
"""

from __future__ import annotations

from ..model import Building
from .base import Writer

#: ezbuild 格式名 -> (DLL writer 名, 输出扩展名, 描述)
DLL_ONLY_SPECS: dict[str, tuple[str, str, str]] = {
    "bdx": ("BDX", ".bdx", "Minecraft 基岩版 BDX 建筑文件（C++ DLL 输出）"),
    "schematic": (
        "Schematic",
        ".schematic",
        "Minecraft Java 版经典 .schematic 结构文件（C++ DLL 输出）",
    ),
    "litematic": (
        "Litematic",
        ".litematic",
        "Litematica 结构文件（C++ DLL 输出）",
    ),
    "mcfn": (
        "MCFunction",
        ".mcfunction",
        "Minecraft datapack 函数（C++ DLL 输出）",
    ),
    "axiombp": (
        "AxiomBP",
        ".bp",
        "Axiom 蓝图（C++ DLL 输出）",
    ),
    "fuhong": (
        "FuHongV5",
        ".fuhong",
        "FuHong V5 建筑文件（C++ DLL 输出）",
    ),
}


class _DllOnlyWriter(Writer):
    """占位：注册用，实际写出由 C++ DLL 完成（需 -c）。"""

    format_name = ""
    extensions = ()
    description = ""

    def render(self, building: Building) -> str | bytes:
        raise NotImplementedError(_need_cpp_message(self.format_name))

    def write(self, building: Building, path) -> "object":
        raise NotImplementedError(_need_cpp_message(self.format_name))


def _need_cpp_message(format_name: str) -> str:
    return (
        f"输出格式 {format_name} 由 C++ DLL 生成，需要加 -c/--cpp 参数；"
        f"请先确认已附带 native DLL（ezbuild/native/water_structure_shared.dll）。"
    )


def _make_classes() -> None:
    for fmt, (_, ext, desc) in DLL_ONLY_SPECS.items():
        cls = type(
            f"{fmt.upper()}Writer",
            (_DllOnlyWriter,),
            {"format_name": fmt, "extensions": (ext,), "description": desc},
        )
        globals()[cls.__name__] = cls


_make_classes()
