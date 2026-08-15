"""子模块自动发现：import 一个包下的所有模块，触发其中的注册逻辑。

用法（在 readers/__init__.py 或 writers/__init__.py 中）：
    from .base import Reader
    from .._discover import discover
    discover(__name__, Reader)
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Type


def discover(package_name: str, base_cls: Type):
    """导入 ``package_name`` 包下所有子模块。

    ``base_cls`` 用于触发类的注册（如 ``Reader.__init_subclass__``），
    只要子模块内定义了 base_cls 的子类且格式名非空，注册表就会收录它。
    """
    pkg = importlib.import_module(package_name)
    for mod in pkgutil.iter_modules(pkg.__path__, prefix=package_name + "."):
        if not mod.name.endswith("__init__"):
            importlib.import_module(mod.name)
