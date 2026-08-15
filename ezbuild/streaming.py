"""增量（流式）转换：把超大 schem/schematic 一部分一部分转成 txt，避免整座建筑载入内存。

常规路径是 Reader → Building（整座建筑在内存）→ Writer，对千万级方块的巨型
结构会吃掉数 GB。这里直接按 16×16 区块列从原始方块数组提取、就地 fill 合并并
**逐行写出**，内存 ≈ 方块数组 + 单个区块，与总方块数无关。

支持两种 Java 结构格式（自动识别）：
- Sponge ``.schem``（``Palette``/``BlockData``/``Offset``）
- 经典 ``.schematic``（``Blocks``/``Data`` + 方块映射表）
"""

from __future__ import annotations

import gzip
import io
import json
import os
import threading
import time
from typing import Callable, Iterator, Union

import nbtlib
import numpy as np

from .model import Block, CommandBlock, COMMAND_BLOCK_IDS, COMMAND_BLOCK_MODES, MODE_IMPULSE
from .readers.base import Source
from .readers.schem import _as_uint8, _parse_blockstate
from .readers.schematic import _parse_spec
from .utils import format_block_states, load_schematic_table, tag_to_python
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


def _refresh_memory_postfix(bar, peak=None) -> None:
    """在 tqdm 进度条右侧刷新内存占用。

    ``peak`` 为 ``_PeakMemory`` 时显示**峰值**（覆盖解析/构造阶段）；
    否则显示当前 RSS。
    """
    if peak is not None:
        p = peak.peak_mb()
        if p:
            bar.set_postfix_str(f"内存峰值 {p:.0f}MB")
    else:
        mem = _memory_mb()
        if mem is not None:
            bar.set_postfix_str(f"内存 {mem:.0f}MB")


class _PeakMemory:
    """后台线程采样当前进程 RSS 峰值（覆盖整段转换含解析构造）。"""

    def __init__(self):
        self._peak = 0.0
        self._stop = False
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        return False

    def _run(self):
        while not self._stop:
            m = _memory_mb()
            if m is not None:
                self._peak = max(self._peak, m)
            time.sleep(0.1)

    def peak_mb(self) -> float:
        return self._peak


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def schematic_to_txt(
    source: Source,
    output: Output,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
):
    """流式把 Sponge .schem 或经典 .schematic 渲染为分区块 txt，增量写入 ``output``。

    输出与「格式 → Building → txt」逐字节一致。``progress`` 为 True 时显示
    tqdm 进度条（含当前内存占用）。返回 ``output``。
    """
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    try:
        first = True
        for line in iter_schematic_txt_lines(
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


# 向后兼容别名（schem_to_txt 现在也接受经典 .schematic）
schem_to_txt = schematic_to_txt


def iter_schematic_txt_lines(
    source: Source,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
) -> Iterator[str]:
    """生成器：逐行产出 schem/schematic 的分区块 txt（内存 O(单区块)）。"""
    peak = _PeakMemory() if progress else None
    if peak is not None:
        peak.__enter__()
    try:
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            with open(source, "rb") as f:
                data = f.read()
        if data[:2] == b"\x1f\x8b":  # gzip 魔数
            data = gzip.decompress(data)
        schem = nbtlib.File.parse(io.BytesIO(data), byteorder="big")

        if "Palette" in schem:
            yield from _iter_sponge_lines(
                schem, chunk_size=chunk_size, fill_merge=fill_merge,
                strip_states=strip_states, progress=progress, peak=peak,
            )
        else:
            yield from _iter_classic_lines(
                schem, chunk_size=chunk_size, fill_merge=fill_merge,
                strip_states=strip_states, progress=progress, peak=peak,
            )
    finally:
        if peak is not None:
            peak.__exit__(None, None, None)


# 向后兼容别名
iter_schem_txt_lines = iter_schematic_txt_lines


def _write_output(output: Output, data) -> Output:
    """写文本或字节到输出（路径或文件对象）。"""
    close = not hasattr(output, "write")
    if close:
        mode = "wb" if isinstance(data, bytes) else "w"
        kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
        fileobj = open(output, mode, **kwargs)
    else:
        fileobj = output
    try:
        fileobj.write(data)
    finally:
        if close:
            fileobj.close()
    return output


# ---------------------------------------------------------------------------
# 流式 cmd_json（命令方块 JSON，lemon 格式）
# ---------------------------------------------------------------------------
def schematic_to_cmd_json(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 的命令方块写为 lemon JSON（不建 Building 模型）。"""
    from .writers.cmd_json import CommandBlockJsonWriter

    src = SchematicSource(source)
    entries = [CommandBlockJsonWriter._entry(cb) for cb in src.command_blocks()]
    text = json.dumps(entries, ensure_ascii=False, indent=4)
    return _write_output(output, text)


# ---------------------------------------------------------------------------
# 流式 ibi（setblock 文本段 + 命令方块 JSON 段，XOR 加密）
# ---------------------------------------------------------------------------
def schematic_to_ibi(
    source: Source,
    output: Output,
    *,
    progress: bool = False,
    strip_states: frozenset | None = None,
) -> Output:
    """流式把 schem/schematic 写为 IBI 包（不建 Building 模型）。

    setblock 文本段流式写入临时文件，再按 1MB 块流式 XOR 加密写出，
    避免把上千万行文本整体放进内存。
    """
    import base64
    import random
    import tempfile

    from .writers.ibi import encode_varint

    peak = _PeakMemory() if progress else None
    if peak is not None:
        peak.__enter__()
    try:
        src = SchematicSource(source)  # 解析/构造在采样内（捕获内存峰值）

        # 阶段 1：setblock 文本流式写入临时文件（避免列表累积）
        bar = None
        if progress:
            from tqdm import tqdm

            counts = np.bincount(src.array3.ravel())
            total_blocks = int(src.array3.size - counts[src.air])
            bar = tqdm(total=total_blocks, desc="生成 setblock", unit="个")
            _refresh_memory_postfix(bar, peak)

        tmp = tempfile.TemporaryFile()
        tw = io.TextIOWrapper(tmp, encoding="utf-8")
        first = True
        n = 0
        try:
            for x, y, z, name, states in src.iter_all_blocks():
                state_str = format_block_states(states, strip_states) if states else ""
                coord = f"~{x} ~{y} ~{z}"
                tw.write(
                    ("" if first else "\n")
                    + (f"setblock {coord} {name} [{state_str}]" if state_str
                       else f"setblock {coord} {name}")
                )
                first = False
                if bar is not None:
                    bar.update(1)
                    n += 1
                    if n % 10000 == 0:  # 节流内存刷新，避免每方块渲染刷屏
                        _refresh_memory_postfix(bar, peak)
            tw.flush()
        finally:
            if bar is not None:
                _refresh_memory_postfix(bar, peak)
                bar.close()

        # 阶段 2：命令方块 JSON（小）
        json_content = [
            {
                "posX": f"~{cb.x}",
                "posY": f"~{cb.y}",
                "posZ": f"~{cb.z}",
                "CommandMessage": base64.b64encode(cb.command.encode("utf-8")).decode("utf-8"),
                "Commandtitle": base64.b64encode(str(i).encode("utf-8")).decode("utf-8"),
                "mode": cb.mode,
                "isTime": cb.tick_delay,
                "Conditional": cb.conditional,
                "isRedstone": cb.needs_redstone,
            }
            for i, cb in enumerate(src.command_blocks(), start=1)
        ]
        json_bytes = json.dumps(json_content, ensure_ascii=False, indent=4).encode("utf-8")

        # 阶段 3：打包加密（IBImport + 两段 XOR，快速 translate + 进度条）
        tmp.seek(0, 2)
        txt_len = tmp.tell()
        tmp.seek(0)
        close_out = not hasattr(output, "write")
        fileobj = open(output, "wb") if close_out else output
        pack_bar = None
        if progress:
            from tqdm import tqdm

            pack_bar = tqdm(total=txt_len + len(json_bytes), desc="打包加密", unit="B", unit_scale=True)
            _refresh_memory_postfix(pack_bar, peak)
        try:
            fileobj.write(b"IBImport ")
            for data, length in ((tmp, txt_len), (io.BytesIO(json_bytes), len(json_bytes))):
                key = random.randint(1, 255)
                table = bytes(i ^ key for i in range(256))  # 快速 XOR 映射表（C 速度）
                fileobj.write(encode_varint(length))
                fileobj.write(bytes([key]))
                while True:
                    chunk = data.read(1 << 20)
                    if not chunk:
                        break
                    fileobj.write(chunk.translate(table))
                    if pack_bar is not None:
                        pack_bar.update(len(chunk))
                        _refresh_memory_postfix(pack_bar, peak)
        finally:
            tmp.close()
            if pack_bar is not None:
                pack_bar.close()
            if close_out:
                fileobj.close()
    finally:
        if peak is not None:
            peak.__exit__(None, None, None)
    return output


# ---------------------------------------------------------------------------
# 共享：区块循环（tp + fill/setblock 逐行）
# ---------------------------------------------------------------------------
def _iter_chunk_lines(
    chunks,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
    total_blocks: int,
    get_region: Callable,
    peak=None,
) -> Iterator[str]:
    """按 S 序遍历区块，逐行产出 tp 与 fill/setblock 命令。"""
    bar = None
    try:
        if progress:
            from tqdm import tqdm

            bar = tqdm(total=total_blocks, desc="转换方块", unit="个")
            _refresh_memory_postfix(bar, peak)

        prev_x = prev_z = 0
        for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
            region = get_region(sx, ex, sz, ez)
            if not region:
                continue
            if bar is not None:
                bar.update(len(region))
                _refresh_memory_postfix(bar, peak)
            rel_x, rel_z = sx - prev_x, sz - prev_z
            yield f"tp ~{rel_x} ~ ~{rel_z}"
            prev_x, prev_z = sx, sz
            fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
            for cmd in sorted(fill_cmds + setblock_cmds, key=_y_sort_key):
                yield cmd
    finally:
        if bar is not None:
            bar.close()


# ---------------------------------------------------------------------------
# Sponge .schem（Palette/BlockData/Offset）
# ---------------------------------------------------------------------------
def _iter_sponge_lines(
    schem,
    *,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
    peak=None,
) -> Iterator[str]:
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
    bd = bd[:total]
    if bd.size < total:
        bd = np.pad(bd, (0, total - bd.size), constant_values=air if air is not None else 0)
    bd3 = bd.reshape(H, L, W)

    if air is None:
        anycol = np.ones((L, W), dtype=bool)
    else:
        anycol = np.any(bd3 != air, axis=0)
    zs, xs = np.nonzero(anycol)
    if len(zs) == 0:
        return
    min_x, max_x = int(xs.min()) + ox, int(xs.max()) + ox
    min_z, max_z = int(zs.min()) + oz, int(zs.max()) + oz
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)
    # 进度条总数 = 非空气**方块数**（不是列数）；bincount 内存友好
    total_blocks = 0
    if progress:
        counts = np.bincount(bd)
        total_blocks = int(bd.size - counts[air]) if air is not None else int(bd.size)

    def get_region(sx, ex, sz, ez):
        rx0, rx1 = max(sx - ox, 0), min(ex - ox, W - 1)
        rz0, rz1 = max(sz - oz, 0), min(ez - oz, L - 1)
        if rx0 > rx1 or rz0 > rz1:
            return {}
        col = bd3[:, rz0:rz1 + 1, rx0:rx1 + 1]
        if air is None:
            mask = np.ones(col.shape, dtype=bool)
        else:
            mask = col != air
        ys, zs, xs = np.nonzero(mask)
        if len(ys) == 0:
            return {}
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
        return region

    yield from _iter_chunk_lines(
        chunks, chunk_size, fill_merge, strip_states, progress, total_blocks, get_region, peak
    )


# ---------------------------------------------------------------------------
# SchematicSource：统一流式方块源（不建 Building 模型）
# ---------------------------------------------------------------------------
class SchematicSource:
    """流式方块源：解析 Sponge .schem 或经典 .schematic，提供逐块迭代。

    - ``array3``: ``(H, L, W)`` 数组 —— Sponge 存调色板索引(uint8)，经典存
      ``id*16+data`` 组合键(uint16)。
    - ``parsed``: ``{索引: (方块名, 状态)}``。
    - ``iter_blocks`` / ``iter_all_blocks``: 产出 ``(绝对x, y, 绝对z, name, states)``。
    - ``command_blocks``: 从 BlockEntities / TileEntities 产出 CommandBlock。
    内存 = 方块数组 + 调色板，与方块数无关。
    """

    def __init__(self, source: Source):
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            with open(source, "rb") as f:
                data = f.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        root = nbtlib.File.parse(io.BytesIO(data), byteorder="big")
        if "Palette" in root:
            self._init_sponge(root)
        else:
            self._init_classic(root)

    # ---------------- 两种格式初始化 ----------------
    def _init_sponge(self, root):
        self.W, self.H, self.L = (
            int(root["Width"]), int(root["Height"]), int(root["Length"]),
        )
        self.ox, self.oy, self.oz = (int(v) for v in root.get("Offset", [0, 0, 0]))
        self.parsed = {
            int(idx): _parse_blockstate(bs) for bs, idx in root["Palette"].items()
        }
        self.air = next((i for i, (n, _) in self.parsed.items() if not n or n == "air"), None)
        bd = _as_uint8(root["BlockData"])
        total = self.W * self.H * self.L
        bd = bd[:total]
        if bd.size < total:
            bd = np.pad(bd, (0, total - bd.size),
                        constant_values=self.air if self.air is not None else 0)
        self.array3 = bd.reshape(self.H, self.L, self.W)
        self._entities = root.get("BlockEntities")

    def _init_classic(self, root):
        self.W, self.H, self.L = (
            int(root["Width"]), int(root["Height"]), int(root["Length"]),
        )
        self.ox = self.oy = self.oz = 0
        self.parsed = {}
        for key, spec in load_schematic_table().items():
            try:
                self.parsed[int(key)] = _parse_spec(spec)
            except (TypeError, ValueError):
                continue
        self.air = 0
        blocks = _as_uint8(root["Blocks"])
        data = _as_uint8(root["Data"]) if "Data" in root else None
        total = self.W * self.H * self.L

        blocks = blocks[:total]
        if blocks.size < total:
            blocks = np.pad(blocks, (0, total - blocks.size), constant_values=0)
        combined = blocks.astype(np.uint16)  # id*16+data 组合键
        combined <<= 4
        if data is not None:
            data = data[:total]
            if data.size < total:
                data = np.pad(data, (0, total - data.size), constant_values=0)
            combined |= data.astype(np.uint16)
        self.array3 = combined.reshape(self.H, self.L, self.W)
        self._entities = root.get("TileEntities")

    # ---------------- 迭代 ----------------
    @property
    def size(self):
        return (self.W, self.H, self.L)

    def _region(self, x0, x1, z0, z1):
        return self.array3[:, z0:z1 + 1, x0:x1 + 1]

    def iter_blocks(self, x0=None, x1=None, z0=None, z1=None):
        """yield (绝对x, y, 绝对z, name, states)；可限 x/z 范围。"""
        if x0 is None:
            x0, x1 = 0, self.W - 1
        if z0 is None:
            z0, z1 = 0, self.L - 1
        col = self._region(x0, x1, z0, z1)
        mask = col != self.air
        ys, zs, xs = np.nonzero(mask)
        for y, zl, xl in zip(ys.tolist(), zs.tolist(), xs.tolist()):
            item = self.parsed.get(int(col[y, zl, xl]))
            if item is None:
                continue
            yield (x0 + xl + self.ox, y + self.oy, z0 + zl + self.oz, item[0], item[1])

    def iter_all_blocks(self):
        """yield 全部方块（按 y 分块迭代，避免一次性大掩码）。"""
        for y0 in range(0, self.H, 64):
            col = self.array3[y0:y0 + 64, :, :]
            mask = col != self.air
            ys, zs, xs = np.nonzero(mask)
            for y, z, x in zip(ys.tolist(), zs.tolist(), xs.tolist()):
                item = self.parsed.get(int(col[y, z, x]))
                if item is None:
                    continue
                yield (x + self.ox, y0 + y + self.oy, z + self.oz, item[0], item[1])

    def command_blocks(self):
        """yield CommandBlock（从 BlockEntities/TileEntities）。"""
        if self._entities is None:
            return
        for be_tag in self._entities:
            be = tag_to_python(be_tag)
            pos = be.get("Pos")
            if pos is not None:
                x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
            elif "x" in be:
                x, y, z = int(be["x"]), int(be["y"]), int(be["z"])
            else:
                continue
            rx, ry, rz = x - self.ox, y - self.oy, z - self.oz
            if not (0 <= rx < self.W and 0 <= ry < self.H and 0 <= rz < self.L):
                continue
            item = self.parsed.get(int(self.array3[ry, rz, rx]))
            if item is None:
                continue
            name = item[0]
            if name not in COMMAND_BLOCK_MODES:
                continue
            yield _command_block_from_entity(
                x=rx + self.ox, y=ry + self.oy, z=rz + self.oz,
                name=name, states=item[1], be=be,
            )


# ---------------------------------------------------------------------------
# 流式 mcstructure / schem（直接构建调色板 + block_data，不建 Building 模型）
# ---------------------------------------------------------------------------
def _trimmed_source(src: SchematicSource):
    """裁切到实际方块范围，返回调色板信息。

    返回 ``(min_coords, size, palette, lookup, region)``：
    - ``palette``: ``[(name, states), ...]``（不含空气）
    - ``lookup``: array 值 -> 调色板索引（-1 为空气/未映射）
    - ``region``: 裁切后的 array3 ``(H2, L2, W2)``
    空建筑返回 None。
    """
    arr = src.array3
    air = src.air
    anyz = np.any(arr != air, axis=0)
    zs, xs = np.nonzero(anyz)
    if len(zs) == 0:
        return None
    anyy = np.any(arr != air, axis=(1, 2))
    ys = np.nonzero(anyy)[0]
    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    size = (max_x - min_x + 1, max_y - min_y + 1, max_z - min_z + 1)

    palette: list[tuple[str, dict]] = []
    palette_map: dict[tuple, int] = {}
    lookup = np.full(4096, -1, dtype=np.int32)  # 覆盖经典 uint16 组合键
    counts = np.bincount(arr.ravel())
    for v in np.nonzero(counts)[0]:
        v = int(v)
        if v == air:
            continue
        item = src.parsed.get(v)
        if item is None:
            continue
        key = (item[0], tuple(sorted(item[1].items())))
        if key not in palette_map:
            palette_map[key] = len(palette)
            palette.append((item[0], dict(item[1])))
        lookup[v] = palette_map[key]

    region = arr[min_y:max_y + 1, min_z:max_z + 1, min_x:max_x + 1]
    return (min_x, min_y, min_z), size, palette, lookup, region


def _states_to_nbt(states: dict):
    from nbtlib.tag import Byte, Compound, Int, String

    out = Compound()
    for key, val in states.items():
        if isinstance(val, bool):
            out[key] = Byte(int(val))
        elif isinstance(val, int):
            out[key] = Int(val)
        else:
            out[key] = String(str(val))
    return out


def schematic_to_mcstructure(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 写为 .mcstructure（不建 Building 模型）。"""
    from nbtlib.tag import Compound, Int, IntArray, List, Short, String

    src = SchematicSource(source)
    trimmed = _trimmed_source(src)
    if trimmed is None:
        return _write_output(output, _empty_mcstructure())
    (min_x, min_y, min_z), (W2, H2, L2), palette, lookup, region = trimmed
    ox, oy, oz = src.ox + min_x, src.oy + min_y, src.oz + min_z
    n_cells = W2 * H2 * L2

    # mcstructure 索引：z + y*Z + x*Y*Z（z 最快）=> transpose(2,0,1) 后 ravel
    block_data = lookup[region.transpose(2, 0, 1).ravel()].astype(np.int32)

    palette_tags = []
    for name, states in palette:
        val = states.get("facing_direction", 0) if name in COMMAND_BLOCK_MODES else 0
        palette_tags.append(
            Compound({
                "name": String(f"minecraft:{name}"),
                "states": _states_to_nbt(states),
                "val": Short(val),
                "version": Int(18090528),
            })
        )

    position_data = Compound()
    for cb in src.command_blocks():
        rx, ry, rz = cb.x - ox, cb.y - oy, cb.z - oz
        idx = rz + ry * L2 + rx * (H2 * L2)
        position_data[str(idx)] = Compound({"block_entity_data": _mcstructure_entity(cb)})

    structure = Compound({
        "format_version": Int(1),
        "size": IntArray([W2, H2, L2]),
        "structure_world_origin": IntArray([ox, oy, oz]),
        "structure": Compound({
            "palette": Compound({
                "default": Compound({
                    "block_palette": List(palette_tags),
                    "block_position_data": position_data,
                }),
            }),
            "block_indices": List([
                IntArray(block_data),
                IntArray(np.full(n_cells, -1, dtype=np.int32)),
            ]),
        }),
    })
    buf = io.BytesIO()
    nbtlib.File(structure).write(buf, byteorder="little")
    return _write_output(output, buf.getvalue())


def schematic_to_schem(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 写为 Sponge .schem（不建 Building 模型）。"""
    from nbtlib.tag import ByteArray, Compound, Int, IntArray, List, String, Byte

    src = SchematicSource(source)
    trimmed = _trimmed_source(src)
    if trimmed is None:
        return _write_output(output, _empty_schem())
    (min_x, min_y, min_z), (W2, H2, L2), palette, lookup, region = trimmed
    ox, oy, oz = src.ox + min_x, src.oy + min_y, src.oz + min_z

    # Sponge BlockData 索引：x + z*W + y*W*L（x 最快）=> region.ravel()（x 最快）
    # Sponge 调色板索引 0 固定为 air：air 单元 -> 0，其余索引 +1
    if len(palette) + 1 > 256:
        raise ValueError("方块种类超过 Sponge .schem 调色板上限 256")
    block_data = lookup[region.ravel()]
    block_data = np.where(block_data == -1, 0, block_data + 1)
    # Sponge 用字节存索引（0-255），>127 转补码
    block_int8 = block_data.astype(np.int8)

    palette_compound = Compound({"minecraft:air": Int(0)})
    for idx, (name, states) in enumerate(palette, start=1):
        palette_compound[_format_blockstate(name, states)] = Int(idx)

    entities = [_schem_entity(cb) for cb in src.command_blocks()]

    root = Compound({
        "Version": Int(2),
        "DataVersion": Int(2975),
        "Width": Int(W2),
        "Height": Int(H2),
        "Length": Int(L2),
        "Offset": IntArray([ox, oy, oz]),
        "Palette": palette_compound,
        "BlockData": ByteArray(block_int8),
        "BlockEntities": List(entities),
        "Entities": List([]),
    })
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        nbtlib.File(root).write(gz, byteorder="big")
    return _write_output(output, buf.getvalue())


def _format_blockstate(name: str, states: dict) -> str:
    """(方块名, 模型状态) -> Sponge 方块状态串（Bedrock->Java）。"""
    from .utils import bedrock_to_java_states

    java = bedrock_to_java_states(states)
    s = f"minecraft:{name}"
    if java:
        parts = []
        for key, val in java.items():
            val_str = "true" if isinstance(val, bool) and val else (
                "false" if isinstance(val, bool) else str(val))
            parts.append(f"{key}={val_str}")
        s += "[" + ",".join(parts) + "]"
    return s


def _mcstructure_entity(cb: CommandBlock):
    from nbtlib.tag import Byte, Compound, Int, String

    return Compound({
        "id": String("CommandBlock"),
        "x": Int(cb.x), "y": Int(cb.y), "z": Int(cb.z),
        "Command": String(cb.command),
        "CustomName": String(cb.custom_name),
        "auto": Byte(int(not cb.needs_redstone)),
        "conditionalMode": Byte(int(cb.conditional)),
        "TickDelay": Int(cb.tick_delay),
        "TrackOutput": Byte(int(cb.track_output)),
    })


def _schem_entity(cb: CommandBlock):
    from nbtlib.tag import Byte, Compound, Int, String

    return Compound({
        "id": String(f"minecraft:{COMMAND_BLOCK_IDS[cb.mode]}"),
        "x": Int(cb.x), "y": Int(cb.y), "z": Int(cb.z),
        "Command": String(cb.command),
        "CustomName": String(cb.custom_name),
        "auto": Byte(int(not cb.needs_redstone)),
        "TrackOutput": Byte(int(cb.track_output)),
        "conditionMet": Byte(0),
    })


def _empty_mcstructure() -> bytes:
    from .writers.mcstructure import McStructureWriter
    from .model import Building

    return McStructureWriter().render(Building())


def _empty_schem() -> bytes:
    from .writers.schem import SchemWriter
    from .model import Building

    return SchemWriter().render(Building())


def _command_block_from_entity(x, y, z, name, states, be) -> CommandBlock:
    """方块名 + BlockEntity NBT -> CommandBlock（兼容 Sponge/经典两种 NBT）。"""
    auto = bool(be.get("auto", False))
    return CommandBlock(
        x=x, y=y, z=z,
        mode=COMMAND_BLOCK_MODES.get(name, MODE_IMPULSE),
        command=str(be.get("Command", "") or ""),
        custom_name=str(be.get("CustomName", "") or ""),
        tick_delay=int(be.get("TickDelay", 0) or 0),
        conditional=bool(states.get("conditional_bit", False)),
        needs_redstone=not auto,
        execute_on_first_tick=bool(be.get("ExecuteOnFirstTick", False)),
        track_output=bool(be.get("TrackOutput", True)),
    )


# ---------------------------------------------------------------------------
# 经典 .schematic（Blocks/Data + 映射表）
# ---------------------------------------------------------------------------
def _iter_classic_lines(
    schem,
    *,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
    peak=None,
) -> Iterator[str]:
    X, Y, Z = int(schem["Width"]), int(schem["Height"]), int(schem["Length"])
    total = X * Y * Z
    if total <= 0:
        return

    blocks = _as_uint8(schem["Blocks"])
    data = _as_uint8(schem["Data"]) if "Data" in schem else None

    def _pad_arr(arr):
        arr = arr[:total]
        if arr.size < total:
            arr = np.pad(arr, (0, total - arr.size), constant_values=0)
        return arr.reshape(Y, Z, X)

    blocks3 = _pad_arr(blocks)
    data3 = _pad_arr(data) if data is not None else None

    # 预计算映射表：id*16+data -> (方块名, 状态)
    parsed = {}
    for key, spec in load_schematic_table().items():
        try:
            parsed[int(key)] = _parse_spec(spec)
        except (TypeError, ValueError):
            continue

    anycol = np.any(blocks3 != 0, axis=0)
    zs, xs = np.nonzero(anycol)
    if len(zs) == 0:
        return
    min_x, max_x = int(xs.min()), int(xs.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)
    # 进度条总数 = 非空气**方块数**（不是列数）
    total_blocks = 0
    if progress:
        counts = np.bincount(blocks)
        total_blocks = int(blocks.size - counts[0])

    def get_region(sx, ex, sz, ez):
        rx0, rx1 = max(sx, 0), min(ex, X - 1)
        rz0, rz1 = max(sz, 0), min(ez, Z - 1)
        if rx0 > rx1 or rz0 > rz1:
            return {}
        colb = blocks3[:, rz0:rz1 + 1, rx0:rx1 + 1]
        mask = colb != 0
        ys, zs, xs = np.nonzero(mask)
        if len(ys) == 0:
            return {}
        cold = data3[:, rz0:rz1 + 1, rx0:rx1 + 1] if data3 is not None else None
        region = {}
        for y, zl, xl in zip(ys.tolist(), zs.tolist(), xs.tolist()):
            bid = int(colb[y, zl, xl]) & 0xFF
            dval = int(cold[y, zl, xl]) & 0xFF if cold is not None else 0
            item = parsed.get((bid << 4) | dval)
            if item is None:
                continue
            name, states = item
            region[(xl, y, zl)] = (
                name, format_block_states(states, strip_states) if states else "",
            )
        return region

    yield from _iter_chunk_lines(
        chunks, chunk_size, fill_merge, strip_states, progress, total_blocks, get_region, peak
    )
