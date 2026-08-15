"""增量（流式）转换：把超大 .schem 一部分一部分转成 txt，避免整座建筑载入内存。

常规路径是 Reader → Building（整座建筑在内存）→ Writer，对千万级方块的巨型
schem 会吃掉数 GB。这里直接按 16×16 区块列从 BlockData 提取方块、就地
fill 合并并**逐行写出**，内存 ≈ BlockData + 单个区块，与总方块数无关。
"""

from __future__ import annotations

import gzip
import io
import os
from typing import Iterator, Union

import nbtlib
import numpy as np

from .readers.base import Source
from .readers.schem import _as_uint8, _parse_blockstate
from .utils import format_block_states
from .writers.txt import _divide_chunks, _optimize_region, _s_sort, _y_sort_key

Output = Union[str, "os.PathLike", "io.TextIOBase"]


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


def _memory_mb() -> float | None:
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


def _refresh_memory_postfix(bar) -> None:
    """在 tqdm 进度条右侧刷新当前内存占用。"""
    mem = _memory_mb()
    if mem is not None:
        bar.set_postfix_str(f"内存 {mem:.0f}MB")


def schem_to_txt(
    source: Source,
    output: Output,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
):
    """流式把 Sponge .schem 渲染为分区块 txt，增量写入 ``output``。

    输出与非流式「schem → Building → txt」逐字节一致（绝对坐标含 Offset）。
    ``progress`` 为 True 时显示 tqdm 进度条。返回 ``output``。
    """
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    try:
        first = True
        for line in iter_schem_txt_lines(
            source, chunk_size=chunk_size, fill_merge=fill_merge,
            strip_states=strip_states, progress=progress,
        ):
            if not first:
                fileobj.write("\n")
            fileobj.write(line)
            first = False
    finally:
        if close:
            fileobj.close()
    return output


def iter_schem_txt_lines(
    source: Source,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
) -> Iterator[str]:
    """生成器：逐行产出 schem 的分区块 txt（内存 O(单区块)）。"""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        with open(source, "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":  # gzip 魔数
        data = gzip.decompress(data)
    schem = nbtlib.File.parse(io.BytesIO(data), byteorder="big")

    W, H, L = int(schem["Width"]), int(schem["Height"]), int(schem["Length"])
    ox, oy, oz = (int(v) for v in schem.get("Offset", [0, 0, 0]))
    parsed = {
        int(idx): _parse_blockstate(blockstate)
        for blockstate, idx in schem["Palette"].items()
    }
    air = next((i for i, (n, _) in parsed.items() if not n or n == "air"), None)

    bd = _as_uint8(schem["BlockData"])
    total = W * H * L
    if bd.size == 0 or total <= 0:
        return
    # BlockData 可能比 W*H*L 略长/略短（尾部省略空气格），补齐后按
    # x 最快、y 最慢排布 reshape：i = x + z*W + y*W*L
    bd = bd[:total]
    if bd.size < total:
        bd = np.pad(bd, (0, total - bd.size), constant_values=air if air is not None else 0)
    bd3 = bd.reshape(H, L, W)

    # 以实际方块范围为界分块（与非流式一致，跳过全空列）
    if air is None:
        anycol = np.ones((L, W), dtype=bool)
    else:
        anycol = np.any(bd3 != air, axis=0)  # (L, W)
    zs, xs = np.nonzero(anycol)
    if len(zs) == 0:
        return
    min_x, max_x = int(xs.min()) + ox, int(xs.max()) + ox
    min_z, max_z = int(zs.min()) + oz, int(zs.max()) + oz
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)

    # 进度条（启用时按非空气方块总数显示）
    bar = None
    try:
        if progress:
            from tqdm import tqdm

            total_blocks = (
                int(np.count_nonzero(bd3 != air)) if air is not None else int(bd3.size)
            )
            bar = tqdm(total=total_blocks, desc="转换方块", unit="个")
            _refresh_memory_postfix(bar)

        prev_x = prev_z = 0
        for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
            # 绝对区块范围 -> bd3 相对范围（边缘裁剪）
            rx0, rx1 = max(sx - ox, 0), min(ex - ox, W - 1)
            rz0, rz1 = max(sz - oz, 0), min(ez - oz, L - 1)
            if rx0 > rx1 or rz0 > rz1:
                continue
            col = bd3[:, rz0:rz1 + 1, rx0:rx1 + 1]  # (H, zlen, xlen) 视图
            if air is None:
                mask = np.ones(col.shape, dtype=bool)
            else:
                mask = col != air
            ys, zs, xs = np.nonzero(mask)
            if len(ys) == 0:
                continue

            # 批量取值，避免逐格 numpy 标量索引
            vals = col[ys, zs, xs]
            region = {}
            for y, zl, xl, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
                item = parsed.get(v)
                if item is None:
                    continue
                name, states = item
                region[(xl, y + oy, zl)] = (
                    name, format_block_states(states, strip_states) if states else "",
                )
            if bar is not None:
                bar.update(len(region))
                _refresh_memory_postfix(bar)
            if not region:
                continue

            rel_x, rel_z = sx - prev_x, sz - prev_z
            yield f"tp ~{rel_x} ~ ~{rel_z}"
            prev_x, prev_z = sx, sz
            fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
            for cmd in sorted(fill_cmds + setblock_cmds, key=_y_sort_key):
                yield cmd
    finally:
        if bar is not None:
            bar.close()
