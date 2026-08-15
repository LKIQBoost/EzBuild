"""Reader 集合：把各种建筑文件解析为中立模型。

新增格式：在此目录新建 ``xxx.py`` 定义 ``XxxReader(Reader)`` 即可。
源码运行时自动发现注册；若要用 Nuitka 打包成 exe，还需在下方
「显式导入兜底」中补一行新模块名（打包后无法扫描文件系统）。
"""

from .base import Reader
from .._discover import discover

# 自动发现（源码运行）
discover(__name__, Reader)

# 显式导入兜底（Nuitka 打包后 pkgutil 扫不到模块，必须显式 import 触发注册）
from . import bdx, ibi, mcstructure, mid, nbs, schem, schematic, txt  # noqa: E402,F401

__all__ = ["Reader"]
