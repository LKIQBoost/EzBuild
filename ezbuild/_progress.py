"""进度条公共工具：实时内存显示 + 懒加载 tqdm.rich 进度条。

writers/ 与 streaming/ 共用（独立模块，避免 writers → streaming 循环导入）。
进度条用 ``tqdm.rich``（rich 美化）；rich 是实验特性，过滤实验警告。
"""

from __future__ import annotations

import warnings

try:
    from tqdm import TqdmExperimentalWarning

    warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)
except ImportError:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# 当前进程内存（MB）——跨平台，懒加载
# ---------------------------------------------------------------------------
_memory_fn = None


def _build_memory_fn():
    """返回一个零参返回 RSS(MB) 的函数；无法测量返回 None。"""
    # psutil 最通用
    try:
        import psutil  # noqa: F401

        return lambda: psutil.Process().memory_info().rss / 1e6
    except ImportError:
        pass
    # Windows：K32GetProcessMemoryInfo
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        dll = ctypes.WinDLL("kernel32")
        fn = dll.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        get_cur = ctypes.windll.kernel32.GetCurrentProcess

        def _win_mem() -> float | None:
            c = _PMC()
            c.cb = ctypes.sizeof(_PMC)
            if fn(get_cur(), ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 1e6
            return None

        return _win_mem
    except Exception:
        pass
    # Linux：/proc/self/status
    try:
        def _linux_mem() -> float | None:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
            return None

        _linux_mem()  # 试一次确认可用
        return _linux_mem
    except Exception:
        pass
    return None


def memory_mb() -> float | None:
    """当前进程 RSS（MB）；无法测量返回 None。"""
    global _memory_fn
    if _memory_fn is None:
        _memory_fn = _build_memory_fn()
    if _memory_fn is None:
        return None
    try:
        return _memory_fn()
    except Exception:
        return None


def refresh_memory_postfix(bar) -> None:
    """在进度条描述里刷新实时内存占用（当前 RSS）。

    ``tqdm.rich`` 不渲染 postfix（无槽位），改写到 desc（task.description）。
    """
    cur = memory_mb()
    if cur is None:
        return
    base = getattr(bar, "_mem_base", None)
    if base is None:
        base = bar.desc or ""
        bar._mem_base = base
    bar.desc = f"{base} | 内存 {cur:.0f}MB"
    bar.refresh()


def make_progress(total: int, desc: str, unit: str = "个"):
    """懒创建 tqdm.rich 进度条（自带实时内存显示），供各转换路径复用。"""
    from tqdm.rich import tqdm

    bar = tqdm(total=total, desc=desc, unit=unit)
    refresh_memory_postfix(bar)
    return bar
