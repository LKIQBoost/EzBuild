"""Minecraft Java 版世界文件夹导出：按 xyz 包围盒流式提取建筑。

支持 Anvil 格式（``region/*.mca``）：
- 1.18+（DataVersion ≥ 2844）：``sections[].block_states``（palette + 位压缩 data）
- 1.13–1.17：``Level.Sections[].Palette`` / ``BlockStates``

坐标约定：世界方块坐标 → 区块 ``cx = x // 16``（向下取整，负坐标正确）；
区块 32×32 组成一个 region 文件 ``r.<rx>.<rz>.mca``。

性能与内存设计（同 streaming.py 的思路，只是源从结构文件换成世界）：
- **流式**：只读包围盒相交的 region 文件，按 16×16 区块逐块解包写出，
  内存 ≈ 单个区块（+ 少量 LRU 缓存），与包围盒总方块数无关。
- **numpy 向量化位解包**：BlockStates 的长整型数组按 ``max(4, ceil(log2(palette)))``
  位压缩，用向量化移位批量解包，避免逐格 Python 循环。
- ``world_to_mcstructure`` / ``world_to_schem`` 需要整块数组（格式使然），
  内存 ≈ 包围盒体积；超大范围建议用 txt / ibi / cmd_json 流式路径。

主要入口（均以 ``box`` = (x1, y1, z1, x2, y2, z2) 世界坐标，含端点）：
    world_to_txt / world_to_ibi / world_to_cmd_json / world_to_mcstructure / world_to_schem
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union

import nbtlib
import numpy as np

from .model import CommandBlock, COMMAND_BLOCK_MODES
from .utils import (
    format_block_states,
    java_to_bedrock_states,
    normalize_block_name,
    tag_to_python,
)
from ._progress import refresh_memory_postfix
from .streaming import (
    _command_block_from_entity,
    _iter_chunk_lines,
    _pack_ibi,
    _write_output,
)

Output = Union[str, "os.PathLike", "io.TextIOBase"]

# region 文件名匹配：r.<rx>.<rz>.mca（负号坐标）
_REGION_NAME = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")

# 常驻缓存上限：同时打开的 region 文件 / 解析后的区块 NBT 数
MAX_OPEN_REGIONS = 16
MAX_CHUNKS = 64


# ---------------------------------------------------------------------------
# 包围盒
# ---------------------------------------------------------------------------
@dataclass
class Box:
    """xyz 包围盒（世界坐标，含端点）。构造时自动归一化 min/max。"""

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int

    def __post_init__(self) -> None:
        self.x1, self.x2 = min(self.x1, self.x2), max(self.x1, self.x2)
        self.y1, self.y2 = min(self.y1, self.y2), max(self.y1, self.y2)
        self.z1, self.z2 = min(self.z1, self.z2), max(self.z1, self.z2)

    @property
    def cx1(self) -> int:
        return self.x1 >> 4

    @property
    def cx2(self) -> int:
        return self.x2 >> 4

    @property
    def cz1(self) -> int:
        return self.z1 >> 4

    @property
    def cz2(self) -> int:
        return self.z2 >> 4

    def contains(self, x: int, y: int, z: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2 and self.z1 <= z <= self.z2


# ---------------------------------------------------------------------------
# 位压缩解包（numpy 向量化）
# ---------------------------------------------------------------------------
def _unpack_bits(data: np.ndarray, bits: int, count: int) -> np.ndarray:
    """把位压缩的 long 数组解包为 ``count`` 个非负整数。

    每格 ``bits`` 位，索引 ``i`` 占据位 ``[i*bits, (i+1)*bits)``（值可跨 long 边界）。
    纯 numpy 向量化：一次性生成索引 → 批量移位/取位，避免逐格 Python 循环。
    """
    if bits <= 0 or count <= 0:
        return np.zeros(count, dtype=np.int32)
    if bits >= 64:
        return data[:count].astype(np.int32)
    n_longs = (count * bits + 63) // 64
    if data.size < n_longs:
        data = np.pad(data, (0, n_longs - data.size))
    arr = data[:n_longs].astype(np.uint64)

    i = np.arange(count, dtype=np.int64)
    bit_start = i * bits
    long_idx = (bit_start >> 6).astype(np.intp)
    bit_off = (bit_start & 63).astype(np.uint64)
    mask = np.uint64((1 << bits) - 1)

    v = (arr[long_idx] >> bit_off) & mask
    # 跨 long 边界：取下一个 long 的低 (bits-(64-bit_off)) 位移到高位补上
    need_next = (bit_off + bits) > 64
    nxt = np.minimum(long_idx + 1, arr.size - 1)
    shift = (np.uint64(64) - bit_off) % np.uint64(64)  # bit_off=0 时 shift=0，避免左移 64
    v_next = (arr[nxt] << shift) & mask
    v |= np.where(need_next, v_next, np.uint64(0))
    return v.astype(np.int32)


def _unpack_section(palette_len: int, data: np.ndarray | None) -> np.ndarray:
    """解包一个 section 的 BlockStates 为 ``(16, 16, 16)`` int32 调色板索引数组。"""
    count = 4096
    if palette_len == 0:
        return np.zeros((16, 16, 16), dtype=np.int32)
    if data is None:
        return np.zeros((16, 16, 16), dtype=np.int32)
    bits = max(4, (palette_len - 1).bit_length())
    return _unpack_bits(data, bits, count).reshape(16, 16, 16)


# ---------------------------------------------------------------------------
# region 文件
# ---------------------------------------------------------------------------
def _decompress(payload: bytes, comp: int) -> bytes | None:
    """按压缩类型解压区块数据。"""
    try:
        if comp == 1:
            return gzip.decompress(payload)
        if comp == 2:
            return zlib.decompress(payload)
        if comp == 3:
            return payload
    except Exception:
        return None
    return None


class RegionFile:
    """单个 .mca region 文件：解析 8KB 头部，按需读取某个区块。"""

    __slots__ = ("path", "_f", "_locations")

    def __init__(self, path):
        self.path = path
        self._f = open(path, "rb")
        self._locations = self._read_locations()

    def _read_locations(self):
        self._f.seek(0)
        header = self._f.read(4096)
        locs: list[tuple[int, int] | None] = []
        for i in range(1024):
            b = header[i * 4:i * 4 + 4]
            if len(b) < 4:
                break
            offset = (b[0] << 16) | (b[1] << 8) | b[2]
            locs.append((offset, b[3]) if offset else None)
        return locs

    def get_chunk(self, cx: int, cz: int):
        """读取区块 NBT 根 Compound；无此区块返回 None。"""
        idx = (cx & 31) + ((cz & 31) << 5)
        loc = self._locations[idx]
        if loc is None:
            return None
        offset, _size = loc
        self._f.seek(offset << 12)  # 扇区偏移 × 4096
        hdr = self._f.read(5)
        if len(hdr) < 5:
            return None
        length = int.from_bytes(hdr[:4], "big")
        comp = hdr[4]
        payload = self._f.read(length - 1)  # length 含压缩类型字节
        if len(payload) < length - 1:
            return None
        data = _decompress(payload, comp)
        if data is None:
            return None
        try:
            return nbtlib.File.parse(io.BytesIO(data), byteorder="big")
        except Exception:
            return None

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WorldSource：按包围盒流式读世界
# ---------------------------------------------------------------------------
class WorldSource:
    """流式方块源：从 Java 世界文件夹按包围盒逐区块提取方块。

    与 :class:`~ezbuild.streaming.SchematicSource` 类似，但数据分散在 region 文件，
    采用按区块惰性加载 + LRU 缓存，内存与包围盒总方块数无关。

    属性（供 mcstructure/schem 路径复用）：
    - ``array3``：整个包围盒的 ``(H, L, W)`` 全局调色板索引数组（惰性构建）
    - ``parsed``：``{全局索引: (方块名, 状态)}``
    - ``air``：空气全局索引（恒 0）
    - ``ox/oy/oz``：包围盒最小世界坐标
    """

    def __init__(self, world_path, box, strip_states: frozenset | None = None):
        world = Path(world_path)
        region_dir = world / "region"
        if not region_dir.is_dir():
            raise ValueError(f"不是有效的 Java 版世界文件夹（缺少 region/ 目录）: {world}")
        self.world = world
        self.box = box if isinstance(box, Box) else Box(*box)
        self.strip_states = strip_states

        # 扫描与包围盒相交的 region 文件
        self._regions: dict[tuple[int, int], Path] = {}
        for mca in region_dir.glob("r.*.*.mca"):
            m = _REGION_NAME.match(mca.name)
            if not m:
                continue
            rx, rz = int(m.group(1)), int(m.group(2))
            if self._region_intersects(rx, rz):
                self._regions[(rx, rz)] = mca
        self._region_coords = frozenset(self._regions)
        if not self._regions:
            raise ValueError(
                f"包围盒内没有找到任何区块（无相交的 region 文件）: "
                f"({self.box.x1},{self.box.y1},{self.box.z1})→({self.box.x2},{self.box.y2},{self.box.z2})"
            )

        self._open_regions: "OrderedDict[tuple[int, int], RegionFile]" = OrderedDict()
        self._chunk_cache: "OrderedDict[tuple[int, int], object]" = OrderedDict()
        self._fmt_cache: dict[tuple, str] = {}  # (name, states 排序元组) -> 状态串

        # mcstructure/schem 需要的 box 数组（惰性）
        self.array3: np.ndarray | None = None
        self.parsed: dict[int, tuple[str, dict]] = {}
        self.air = 0
        self.ox, self.oy, self.oz = self.box.x1, self.box.y1, self.box.z1
        self.W = self.box.x2 - self.box.x1 + 1
        self.H = self.box.y2 - self.box.y1 + 1
        self.L = self.box.z2 - self.box.z1 + 1

    # ------------------------------------------------------------------ region / 区块
    def _region_intersects(self, rx: int, rz: int) -> bool:
        rx0, rz0 = rx * 512, rz * 512
        rx1, rz1 = rx0 + 511, rz0 + 511
        return not (
            self.box.x2 < rx0 or self.box.x1 > rx1
            or self.box.z2 < rz0 or self.box.z1 > rz1
        )

    def _load_region(self, rx: int, rz: int) -> RegionFile | None:
        key = (rx, rz)
        rf = self._open_regions.get(key)
        if rf is not None:
            self._open_regions.move_to_end(key)
            return rf
        path = self._regions.get(key)
        if path is None:
            return None
        rf = RegionFile(path)
        self._open_regions[key] = rf
        if len(self._open_regions) > MAX_OPEN_REGIONS:
            old_k, old_v = self._open_regions.popitem(last=False)
            old_v.close()
        return rf

    def _load_chunk(self, cx: int, cz: int):
        key = (cx, cz)
        chunk = self._chunk_cache.get(key)
        if chunk is not None:
            self._chunk_cache.move_to_end(key)
            return chunk
        rf = self._load_region(cx >> 5, cz >> 5)
        if rf is None:
            return None
        chunk = rf.get_chunk(cx, cz)
        if chunk is None:
            return None
        status = chunk.get("Status")
        if status is not None and str(status) != "full":
            return None  # 未生成完全的区块跳过，不缓存
        self._chunk_cache[key] = chunk
        if len(self._chunk_cache) > MAX_CHUNKS:
            self._chunk_cache.popitem(last=False)
        return chunk

    def _iter_chunk_coords(self) -> Iterator[tuple[int, int]]:
        for cx in range(self.box.cx1, self.box.cx2 + 1):
            for cz in range(self.box.cz1, self.box.cz2 + 1):
                if (cx >> 5, cz >> 5) in self._region_coords:
                    yield cx, cz

    # ------------------------------------------------------------------ section / 调色板
    @staticmethod
    def _iter_sections(chunk):
        """yield ``(sy, section_tag)`` 按 y 升序，兼容 1.13+ 两种布局。"""
        if "Level" in chunk:
            secs = chunk["Level"].get("Sections")
        else:
            secs = chunk.get("sections")
        if secs is None:
            return
        out = []
        for s in secs:
            try:
                sy = int(s.get("Y", 0))
            except (TypeError, ValueError):
                continue
            out.append((sy, s))
        out.sort(key=lambda t: t[0])
        for item in out:
            yield item

    @staticmethod
    def _section_palette_and_data(section):
        """返回 ``(palette, data)``——palette: ``[(name, 模型状态)]``，data 为 uint64 数组或 None。

        1.18+ 用 ``block_states.palette / data``，1.13–1.17 用 ``Palette / BlockStates``。
        全空气 section（无 block_states / 空调色板）返回 ``(None, None)``；
        1.13 之前（Blocks/Data 调色板前的旧格式）抛错提示。
        """
        if "block_states" in section:
            bs = section["block_states"]
            palette_tag = bs.get("palette")
            data_tag = bs.get("data")
        elif "Palette" in section:
            palette_tag = section.get("Palette")
            data_tag = section.get("BlockStates")
        else:
            if "Blocks" in section:
                raise ValueError(
                    "不支持的存档版本：区块使用 1.13 之前的 Blocks/Data 方块格式，"
                    "请用 1.13+ 世界（至少 1.13，建议 1.18+）"
                )
            return None, None
        if palette_tag is None:
            return None, None
        palette = []
        for p in palette_tag:
            name = normalize_block_name(str(p.get("Name", "")))
            props = p.get("Properties")
            states = tag_to_python(props) if props is not None else {}
            palette.append((name, java_to_bedrock_states(states)))
        data = None
        if data_tag is not None and len(data_tag) > 0:
            # nbtlib Long 是有符号 int64；位压缩数据可 ≥2^63（负值），按无符号掩码还原
            data = np.asarray([int(v) & ((1 << 64) - 1) for v in data_tag], dtype=np.uint64)
        return palette, data

    def _state_str(self, name: str, states: dict) -> str:
        key = (name, tuple(sorted(states.items())))
        s = self._fmt_cache.get(key)
        if s is None:
            s = format_block_states(states, self.strip_states)
            self._fmt_cache[key] = s
        return s

    def _column(self, cx: int, cz: int):
        """解包一个区块列，返回 ``(col, entries)`` 或 None。

        - ``col``: ``(Ybox, 16, 16)`` int32，0 = 空气；值是该列内的全局索引
        - ``entries``: ``{列索引: (方块名, 状态 dict)}``（不含空气）
        只解包与包围盒 y 相交的 section；缺失 section 视为空气。
        """
        chunk = self._load_chunk(cx, cz)
        if chunk is None:
            return None
        ylen = self.box.y2 - self.box.y1 + 1
        col = np.zeros((ylen, 16, 16), dtype=np.int32)
        entries: dict[int, tuple[str, dict]] = {}
        key_to_id: dict[tuple, int] = {}

        for sy, section in self._iter_sections(chunk):
            pal, data = self._section_palette_and_data(section)
            if pal is None:
                continue
            idx = _unpack_section(len(pal), data)  # (16,16,16) 局部调色板索引
            ly0, ly1 = max(sy * 16, self.box.y1), min(sy * 16 + 15, self.box.y2)
            if ly0 > ly1:
                continue
            local_to_col = np.zeros(len(pal), dtype=np.int32)
            for li, (name, states) in enumerate(pal):
                if not name or name == "air":
                    continue  # 空气 -> 0
                key = (name, tuple(sorted(states.items())))
                eid = key_to_id.get(key)
                if eid is None:
                    eid = len(entries) + 1  # 列索引从 1 起，0 保留给空气
                    key_to_id[key] = eid
                    entries[eid] = (name, states)
                local_to_col[li] = eid
            mapped = local_to_col[idx[(ly0 & 15):(ly1 & 15) + 1, :, :]]
            col[ly0 - self.box.y1:ly1 - self.box.y1 + 1, :, :] = mapped
        return col, entries

    # ------------------------------------------------------------------ 对外迭代
    def get_region(self, sx: int, ex: int, sz: int, ez: int) -> dict:
        """一个 16×16 区块列的方块 dict：``{(列内x, 绝对y, 列内z): (name, 状态串)}``。

        供 streaming ``_iter_chunk_lines`` 逐区块输出（tp + fill/setblock）。
        ``sx..ex`` / ``sz..ez`` 为世界对齐的 16×16 区块原点范围（含端点）。
        """
        cx, cz = sx >> 4, sz >> 4
        info = self._column(cx, cz)
        if info is None:
            return {}
        col, entries = info
        lx0 = max(self.box.x1 - cx * 16, 0)
        lx1 = min(self.box.x2 - cx * 16, 15)
        lz0 = max(self.box.z1 - cz * 16, 0)
        lz1 = min(self.box.z2 - cz * 16, 15)
        if lx0 > lx1 or lz0 > lz1:
            return {}
        sub = col[:, lz0:lz1 + 1, lx0:lx1 + 1]
        mask = sub != 0
        ys, zs, xs = np.nonzero(mask)
        if len(ys) == 0:
            return {}
        vals = sub[ys, zs, xs]
        region = {}
        for y, z, x, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
            item = entries.get(int(v))
            if item is None:
                continue
            name, states = item
            region[(x + lx0, self.box.y1 + y, z + lz0)] = (name, self._state_str(name, states))
        return region

    def _iter_chunk_block_states(self, cx: int, cz: int):
        """yield 一个区块列内、包围盒裁剪后的方块 ``(绝对x, 绝对y, 绝对z, name, states)``。"""
        info = self._column(cx, cz)
        if info is None:
            return
        col, entries = info
        lx0 = max(self.box.x1 - cx * 16, 0)
        lx1 = min(self.box.x2 - cx * 16, 15)
        lz0 = max(self.box.z1 - cz * 16, 0)
        lz1 = min(self.box.z2 - cz * 16, 15)
        if lx0 > lx1 or lz0 > lz1:
            return
        sub = col[:, lz0:lz1 + 1, lx0:lx1 + 1]
        mask = sub != 0
        ys, zs, xs = np.nonzero(mask)
        vals = sub[ys, zs, xs]
        base_x, base_z = cx * 16 + lx0, cz * 16 + lz0
        for y, z, x, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
            item = entries.get(int(v))
            if item is None:
                continue
            name, states = item
            yield (base_x + x, self.box.y1 + y, base_z + z, name, states)

    def _iter_chunk_blocks(self, cx: int, cz: int) -> Iterator[tuple[int, int, int, str, str]]:
        """yield 一个区块列内、包围盒裁剪后的方块 ``(绝对x, 绝对y, 绝对z, name, 状态串)``。"""
        for x, y, z, name, states in self._iter_chunk_block_states(cx, cz):
            yield (x, y, z, name, self._state_str(name, states))

    def iter_all_blocks(self) -> Iterator[tuple[int, int, int, str, str]]:
        """yield ``(绝对x, 绝对y, 绝对z, name, 格式化状态串)``，逐区块、内存有界。"""
        for cx, cz in self._iter_chunk_coords():
            yield from self._iter_chunk_blocks(cx, cz)

    def command_blocks(self) -> Iterator[CommandBlock]:
        """yield 包围盒内的命令方块（从 block_entities / TileEntities 提取）。"""
        for cx, cz in self._iter_chunk_coords():
            chunk = self._load_chunk(cx, cz)
            if chunk is None:
                continue
            bes = chunk.get("block_entities")
            if bes is None and "Level" in chunk:
                bes = chunk["Level"].get("TileEntities")
            if bes is None:
                continue
            for be_tag in bes:
                be = tag_to_python(be_tag)
                try:
                    x, y, z = int(be["x"]), int(be["y"]), int(be["z"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not self.box.contains(x, y, z):
                    continue
                name = normalize_block_name(str(be.get("id", "")))
                if name not in COMMAND_BLOCK_MODES:
                    continue
                states = self._block_states_at(chunk, x, y, z)
                yield _command_block_from_entity(x, y, z, name, states, be)

    def _block_states_at(self, chunk, x: int, y: int, z: int) -> dict:
        """取世界坐标 (x,y,z) 所在方块的状态（模型/Bedrock 风格）；查不到返回 {}。"""
        sy = y >> 4
        for sec_sy, section in self._iter_sections(chunk):
            if sec_sy != sy:
                continue
            pal, data = self._section_palette_and_data(section)
            if pal is None:
                return {}
            idx = _unpack_section(len(pal), data)
            v = int(idx[y & 15, z & 15, x & 15])
            if 0 <= v < len(pal):
                return pal[v][1]
            return {}
        return {}

    # ------------------------------------------------------------------ box 数组（mcstructure/schem）
    def _scan_bounds(self, progress: bool = False):
        """第一遍扫描：返回非空气的世界坐标边界 ``(min_x, min_y, min_z, max_x, max_y, max_z)``
        或 None（整个包围盒全空气）。"""
        from ._progress import make_progress, refresh_memory_postfix

        INF = 1 << 60
        min_x = min_y = min_z = INF
        max_x = max_y = max_z = -INF
        total_chunks = (self.box.cx2 - self.box.cx1 + 1) * (self.box.cz2 - self.box.cz1 + 1)
        bar = make_progress(total_chunks, "扫描边界", "区块") if progress else None
        n = 0
        for cx, cz in self._iter_chunk_coords():
            n += 1
            if bar is not None and n % 200 == 0:
                bar.update(200)
                refresh_memory_postfix(bar)
            info = self._column(cx, cz)
            if info is None:
                continue
            col, _entries = info
            anycol = np.any(col != 0, axis=0)  # (z, x) 是否有非空气
            zs, xs = np.nonzero(anycol)
            if len(zs) == 0:
                continue
            z0, z1 = int(zs.min()), int(zs.max())
            x0, x1 = int(xs.min()), int(xs.max())
            # y 范围
            anyy = np.any(col != 0, axis=(1, 2))
            ys = np.nonzero(anyy)[0]
            y0, y1 = int(ys.min()), int(ys.max())
            wx0, wx1 = cx * 16 + x0, cx * 16 + x1
            wz0, wz1 = cz * 16 + z0, cz * 16 + z1
            wy0, wy1 = self.box.y1 + y0, self.box.y1 + y1
            if wx0 < min_x: min_x = wx0
            if wx1 > max_x: max_x = wx1
            if wy0 < min_y: min_y = wy0
            if wy1 > max_y: max_y = wy1
            if wz0 < min_z: min_z = wz0
            if wz1 > max_z: max_z = wz1
        if bar is not None:
            bar.update(n - bar.n)
            refresh_memory_postfix(bar)
            bar.close()
        if min_x == INF:
            return None
        min_x = max(min_x, self.box.x1); max_x = min(max_x, self.box.x2)
        min_y = max(min_y, self.box.y1); max_y = min(max_y, self.box.y2)
        min_z = max(min_z, self.box.z1); max_z = min(max_z, self.box.z2)
        return min_x, min_y, min_z, max_x, max_y, max_z

    def build_box_array(self, progress: bool = False) -> None:
        """构建包围盒内**实际内容**的 ``array3``（两遍：先扫边界，只分配裁剪后数组）。

        避免为超大包围盒一次性分配整盒数组。``ox/oy/oz`` 更新为内容最小世界坐标。
        """
        if self.array3 is not None:
            return
        from ._progress import make_progress, refresh_memory_postfix

        bounds = self._scan_bounds(progress)
        if bounds is None:
            self.array3 = np.zeros((1, 1, 1), dtype=np.int32)
            self.parsed = {0: ("air", {})}
            self.air = 0
            return
        min_x, min_y, min_z, max_x, max_y, max_z = bounds
        H2, L2, W2 = max_y - min_y + 1, max_z - min_z + 1, max_x - min_x + 1
        arr = np.zeros((H2, L2, W2), dtype=np.int32)  # 0 = 空气
        palette_map: dict[tuple, int] = {("air", ()): 0}
        palette: list[tuple[str, dict]] = [("air", {})]

        total_chunks = (self.box.cx2 - self.box.cx1 + 1) * (self.box.cz2 - self.box.cz1 + 1)
        bar = make_progress(total_chunks, "填充数组", "区块") if progress else None
        n = 0
        for cx, cz in self._iter_chunk_coords():
            n += 1
            if bar is not None and n % 200 == 0:
                bar.update(200)
                refresh_memory_postfix(bar)
            info = self._column(cx, cz)
            if info is None:
                continue
            col, entries = info
            max_id = max(entries, default=0)
            local_to_global = np.zeros(max_id + 1, dtype=np.int32)  # 0 -> 空气(0)
            for eid, (name, states) in entries.items():
                key = (name, tuple(sorted(states.items())))
                gid = palette_map.get(key)
                if gid is None:
                    gid = len(palette)
                    palette_map[key] = gid
                    palette.append((name, dict(states)))
                local_to_global[eid] = gid
            x0 = max(cx * 16, min_x)
            x1 = min(cx * 16 + 15, max_x)
            z0 = max(cz * 16, min_z)
            z1 = min(cz * 16 + 15, max_z)
            if x0 > x1 or z0 > z1:
                continue
            sub = col[min_y - self.box.y1:max_y - self.box.y1 + 1,
                      z0 - cz * 16:z1 - cz * 16 + 1,
                      x0 - cx * 16:x1 - cx * 16 + 1]
            arr[:, z0 - min_z:z1 - min_z + 1, x0 - min_x:x1 - min_x + 1] = local_to_global[sub]
        if bar is not None:
            bar.update(n - bar.n)
            refresh_memory_postfix(bar)
            bar.close()

        self.array3 = arr
        self.parsed = {i: v for i, v in enumerate(palette)}
        self.air = 0
        self.ox, self.oy, self.oz = min_x, min_y, min_z
        self.W, self.H, self.L = W2, H2, L2

    def close(self) -> None:
        for rf in self._open_regions.values():
            rf.close()
        self._open_regions.clear()


# ---------------------------------------------------------------------------
# 世界导出入口
# ---------------------------------------------------------------------------
def _coerce_box(box) -> Box:
    return box if isinstance(box, Box) else Box(*box)


def open_world_source(world_path, box, strip_states: frozenset | None = None):
    """按世界文件夹类型创建 Java 或 Bedrock 方块源（对外接口一致）。

    Java 存档含 ``region/``；Bedrock 存档含 ``db/`` + ``level.dat``。
    """
    p = Path(world_path)
    if (p / "region").is_dir():
        return WorldSource(world_path, box, strip_states=strip_states)
    if (p / "db").is_dir() and (p / "level.dat").is_file():
        from .bedrock import BedrockWorldSource

        return BedrockWorldSource(world_path, box, strip_states=strip_states)
    raise ValueError(
        f"不是有效的世界文件夹（需要 region/ 的 Java 存档或 db/ + level.dat 的 Bedrock 存档）: {world_path}"
    )


# ---------------------------------------------------------------------------
# 世界导出的 -t 并行（按 16×16 区块分片，多进程共享内存有界）
# ---------------------------------------------------------------------------
def _world_chunk_list(box: Box) -> list[tuple[int, int]]:
    """包围盒覆盖的全部 chunk 坐标列表。"""
    return [
        (cx, cz)
        for cx in range(box.cx1, box.cx2 + 1)
        for cz in range(box.cz1, box.cz2 + 1)
    ]


def _world_block_lines_worker(args):
    """子进程：把一组区块的方块写成 setblock 行到临时文件（每行带换行）。"""
    world_path, box, strip_states, chunks, tmp_path = args
    ws = open_world_source(world_path, box, strip_states=strip_states)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for cx, cz in chunks:
                for x, y, z, name, state_str in ws._iter_chunk_blocks(cx, cz):
                    f.write(f"setblock ~{x} ~{y} ~{z} {name}")
                    if state_str:
                        f.write(f" [{state_str}]")
                    f.write("\n")
    finally:
        ws.close()
    return tmp_path


def _world_txt_parallel(
    world_path, box: Box, strip_states, output, workers: int, progress: bool = False
) -> Output:
    """多进程并行生成 setblock 行到临时文件并合并（内存有界）。"""
    import multiprocessing as mp
    import tempfile

    from .streaming import _write_parallel_lines

    chunks = _world_chunk_list(box)
    n = workers if workers and workers > 0 else max(2, min(os.cpu_count() or 2, 8))
    n = min(n, len(chunks))
    if n <= 1:
        return _write_setblock_direct(world_path, box, strip_states, output, progress)
    bounds = [len(chunks) * i // n for i in range(n + 1)]
    tmp_files = []
    args_list = []
    for i in range(n):
        part = chunks[bounds[i]:bounds[i + 1]]
        if not part:
            continue
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmpf.close()
        tmp_files.append(tmpf.name)
        args_list.append((world_path, box, strip_states, part, tmpf.name))
    try:
        with mp.Pool(n) as pool:
            pool.map(_world_block_lines_worker, args_list)
        return _write_parallel_lines(output, tmp_files)
    finally:
        for f in tmp_files:
            try:
                os.remove(f)
            except OSError:
                pass


def _write_setblock_direct(world_path, box: Box, strip_states, output,
                           progress: bool = False) -> Output:
    """单进程直写纯 setblock txt（无 tp/分块）。"""
    from ._progress import make_progress, refresh_memory_postfix

    ws = open_world_source(world_path, box, strip_states=strip_states)
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    first = True
    bar = make_progress(0, "生成 setblock", "个") if progress else None
    n = 0
    try:
        for x, y, z, name, state_str in ws.iter_all_blocks():
            if not first:
                fileobj.write("\n")
            first = False
            fileobj.write(f"setblock ~{x} ~{y} ~{z} {name}")
            if state_str:
                fileobj.write(f" [{state_str}]")
            if bar is not None:
                bar.update(1)
                n += 1
                if n % 10000 == 0:
                    refresh_memory_postfix(bar)
    finally:
        if bar is not None:
            refresh_memory_postfix(bar)
            bar.close()
        ws.close()
        if close:
            fileobj.close()
    return output


def world_to_txt(
    world_path,
    output,
    box,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
    workers: int | None = None,
) -> Output:
    """流式把世界包围盒导出为 txt。

    ``fill_merge=True``：分区块（16×16 + tp 导航 + fill 合并，逐区块流式）；
    ``fill_merge=False``（--nofill）：纯 setblock，逐区块流式直写。
    ``workers``（-t）：纯 setblock 时按区块多进程并行。
    """
    box = _coerce_box(box)
    if workers is not None and not fill_merge:
        return _world_txt_parallel(world_path, box, strip_states, output, workers, progress)
    ws = open_world_source(world_path, box, strip_states=strip_states)
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    first = True
    try:
        if not fill_merge:
            return _write_setblock_direct(world_path, box, strip_states, output, progress)
        chunks = _world_chunks(ws.box)
        it = _iter_chunk_lines(
            chunks, chunk_size, True, strip_states, progress, None, ws.get_region
        )
        for line in it:
            if not first:
                fileobj.write("\n")
            first = False
            fileobj.write(line)
    finally:
        ws.close()
        if close:
            fileobj.close()
    return output


def world_to_cmd_json(world_path, output, box, *, strip_states=None, progress=False) -> Output:
    """把包围盒内的命令方块写为 lemon JSON。"""
    from .writers.cmd_json import CommandBlockJsonWriter

    ws = open_world_source(world_path, _coerce_box(box), strip_states=strip_states)
    try:
        entries = [CommandBlockJsonWriter._entry(cb) for cb in ws.command_blocks()]
        text = json.dumps(entries, ensure_ascii=False, indent=4)
        return _write_output(output, text)
    finally:
        ws.close()


def world_to_ibi(
    world_path,
    output,
    box,
    *,
    strip_states=None,
    progress: bool = False,
    workers: int | None = None,
) -> Output:
    """流式把包围盒导出为 IBI 包（setblock 文本 + 命令方块 JSON，XOR 加密）。

    ``workers``（-t）：setblock 文本按区块多进程并行生成。
    """
    import base64
    import tempfile

    box = _coerce_box(box)
    ws = open_world_source(world_path, box, strip_states=strip_states)
    tmp = None
    tmp_files = None
    try:
        # 阶段 1：setblock 文本流式写入临时文件（避免列表累积）
        if workers is not None:
            import multiprocessing as mp

            from .streaming import _MultiFile

            chunks = _world_chunk_list(box)
            n = workers if workers > 0 else max(2, min(os.cpu_count() or 2, 8))
            n = min(n, len(chunks))
            if n > 1:
                bounds = [len(chunks) * i // n for i in range(n + 1)]
                tmp_files = []
                args_list = []
                for i in range(n):
                    part = chunks[bounds[i]:bounds[i + 1]]
                    if not part:
                        continue
                    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                    tmpf.close()
                    tmp_files.append(tmpf.name)
                    args_list.append((world_path, box, strip_states, part, tmpf.name))
                if progress:
                    print("      并行生成 setblock（多进程）…")
                with mp.Pool(n) as pool:
                    pool.map(_world_block_lines_worker, args_list)
                tmp = _MultiFile(tmp_files)
                tmp_files = tmp_files
            else:
                tmp = tempfile.TemporaryFile()
                tmp_files = None
        else:
            tmp = tempfile.TemporaryFile()
            tmp_files = None

        if tmp_files is None:
            # 单进程：流式写 setblock 文本（带进度条，无总数即计数）
            tw = io.TextIOWrapper(tmp, encoding="utf-8")
            bar = None
            n = 0
            if progress:
                from tqdm.rich import tqdm

                bar = tqdm(desc="生成 setblock", unit="个")
                refresh_memory_postfix(bar)
            first = True
            try:
                for x, y, z, name, state_str in ws.iter_all_blocks():
                    tw.write(
                        ("" if first else "\n")
                        + f"setblock ~{x} ~{y} ~{z} {name}"
                        + (f" [{state_str}]" if state_str else "")
                    )
                    first = False
                    if bar is not None:
                        bar.update(1)
                        n += 1
                        if n % 10000 == 0:
                            refresh_memory_postfix(bar)
                tw.flush()
            finally:
                if bar is not None:
                    refresh_memory_postfix(bar)
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
            for i, cb in enumerate(ws.command_blocks(), start=1)
        ]
        json_bytes = json.dumps(json_content, ensure_ascii=False, indent=4).encode("utf-8")

        # 阶段 3：打包加密（流式 XOR）
        if tmp_files is not None:
            txt_len = sum(os.path.getsize(f) for f in tmp_files)
        else:
            tmp.seek(0, 2)
            txt_len = tmp.tell()
            tmp.seek(0)
        return _pack_ibi(output, tmp, txt_len, json_bytes, progress)
    finally:
        if tmp is not None:
            tmp.close()
        if tmp_files is not None:
            for f in tmp_files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        ws.close()


def world_to_mcstructure(world_path, output, box, *, progress=False) -> Output:
    """把包围盒导出为 .mcstructure 结构文件（世界坐标作为结构原点）。"""
    from .streaming import schematic_to_mcstructure

    ws = open_world_source(world_path, _coerce_box(box))
    try:
        ws.build_box_array(progress)
        return schematic_to_mcstructure(ws, output, progress=progress)
    finally:
        ws.close()


def _world_schem_streaming(ws, output, progress: bool = False) -> Output:
    """流式写 Sponge .schem：BlockData 写磁盘临时文件（memmap），内存有界。

    两遍：先扫描边界 + 收集调色板，再按区块把调色板索引写进临时文件，
    最后手动组装大端 NBT、BlockData 从临时文件分块流出。
    """
    import gzip
    import tempfile

    from ._progress import make_progress, refresh_memory_postfix
    from .streaming import _empty_schem, _format_blockstate, _schem_entity

    # ---- 遍 1：边界 + 调色板 ----
    palette_map: dict[tuple, int] = {}
    palette_list: list[tuple[str, dict]] = []
    INF = 1 << 60
    min_x = min_y = min_z = INF
    max_x = max_y = max_z = -INF
    total_chunks = (ws.box.cx2 - ws.box.cx1 + 1) * (ws.box.cz2 - ws.box.cz1 + 1)
    bar = make_progress(total_chunks, "扫描+调色板", "区块") if progress else None
    n = 0
    for cx in range(ws.box.cx1, ws.box.cx2 + 1):
        for cz in range(ws.box.cz1, ws.box.cz2 + 1):
            n += 1
            if bar is not None and n % 200 == 0:
                bar.update(200)
                refresh_memory_postfix(bar)
            for x, y, z, name, states in ws._iter_chunk_block_states(cx, cz):
                key = (name, tuple(sorted(states.items())))
                if key not in palette_map:
                    palette_map[key] = len(palette_list) + 1
                    palette_list.append((name, states))
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
                if z < min_z: min_z = z
                if z > max_z: max_z = z
    if bar is not None:
        bar.update(n - bar.n)
        refresh_memory_postfix(bar)
        bar.close()
    if min_x == INF:
        return _write_output(output, _empty_schem())
    if len(palette_list) + 1 > 256:
        raise ValueError(
            f"区域内方块种类 {len(palette_list) + 1} 超过 Sponge .schem 调色板上限 256，"
            f"无法导出 .schem。建议：改用 mcstructure（无调色板上限）、缩小 -pos1/-pos2 范围，"
            f"或用 txt / ibi 流式格式。"
        )
    W2, H2, L2 = max_x - min_x + 1, max_y - min_y + 1, max_z - min_z + 1
    n_cells = W2 * H2 * L2

    # ---- 遍 2：BlockData 写临时文件（memmap）----
    block_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as bf:
            block_path = bf.name
            bf.truncate(n_cells)
        block = np.memmap(block_path, dtype=np.uint8, mode="r+", shape=(n_cells,))
        bar = make_progress(total_chunks, "填充数组", "区块") if progress else None
        n = 0
        try:
            for cx in range(ws.box.cx1, ws.box.cx2 + 1):
                for cz in range(ws.box.cz1, ws.box.cz2 + 1):
                    n += 1
                    if bar is not None and n % 200 == 0:
                        bar.update(200)
                        refresh_memory_postfix(bar)
                    xs, ys, zs, pids = [], [], [], []
                    for x, y, z, name, states in ws._iter_chunk_block_states(cx, cz):
                        xs.append(x); ys.append(y); zs.append(z)
                        pids.append(palette_map[(name, tuple(sorted(states.items())))])
                    if xs:
                        idx = ((np.asarray(xs, dtype=np.int64) - min_x)
                               + (np.asarray(zs, dtype=np.int64) - min_z) * W2
                               + (np.asarray(ys, dtype=np.int64) - min_y) * (W2 * L2))
                        block[idx] = np.asarray(pids, dtype=np.uint8)
        finally:
            if bar is not None:
                bar.update(n - bar.n)
                refresh_memory_postfix(bar)
                bar.close()
            block.flush()
            del block

        # ---- 遍 3：手动组装大端 NBT，BlockData 分块流出 ----
        def _wstr(gz, s: str):
            b = s.encode("utf-8")
            gz.write(struct.pack(">H", len(b)))
            gz.write(b)

        close_out = not hasattr(output, "write")
        fileobj = open(output, "wb") if close_out else output
        try:
            with gzip.GzipFile(fileobj=fileobj, mode="wb") as gz:
                gz.write(b"\x0a"); _wstr(gz, "")  # 根 Compound（无名）
                gz.write(b"\x03"); _wstr(gz, "Version"); gz.write(struct.pack(">i", 2))
                gz.write(b"\x03"); _wstr(gz, "DataVersion"); gz.write(struct.pack(">i", 2975))
                gz.write(b"\x03"); _wstr(gz, "Width"); gz.write(struct.pack(">i", W2))
                gz.write(b"\x03"); _wstr(gz, "Height"); gz.write(struct.pack(">i", H2))
                gz.write(b"\x03"); _wstr(gz, "Length"); gz.write(struct.pack(">i", L2))
                gz.write(b"\x0b"); _wstr(gz, "Offset")
                gz.write(struct.pack(">i", 3))
                for v in (min_x, min_y, min_z):
                    gz.write(struct.pack(">i", v))
                # Palette
                gz.write(b"\x0a"); _wstr(gz, "Palette")
                gz.write(b"\x03"); _wstr(gz, "minecraft:air"); gz.write(struct.pack(">i", 0))
                for i, (name, states) in enumerate(palette_list, start=1):
                    s = _format_blockstate(name, states)
                    gz.write(b"\x03"); _wstr(gz, s); gz.write(struct.pack(">i", i))
                gz.write(b"\x00")
                # BlockData（分块流出）
                gz.write(b"\x07"); _wstr(gz, "BlockData")
                gz.write(struct.pack(">i", n_cells))
                bm = np.memmap(block_path, dtype=np.uint8, mode="r", shape=(n_cells,))
                try:
                    for i in range(0, n_cells, 1 << 20):
                        gz.write(bm[i:i + (1 << 20)].tobytes())
                finally:
                    del bm
                # BlockEntities（命令方块）
                entities = [_schem_entity(cb) for cb in ws.command_blocks()]
                gz.write(b"\x09"); _wstr(gz, "BlockEntities")
                gz.write(b"\x0a")
                gz.write(struct.pack(">i", len(entities)))
                for e in entities:
                    buf = io.BytesIO()
                    nbtlib.File(e, byteorder="big").write(buf, byteorder="big")
                    gz.write(buf.getvalue())
                # Entities（空）
                gz.write(b"\x09"); _wstr(gz, "Entities")
                gz.write(b"\x00"); gz.write(struct.pack(">i", 0))
                gz.write(b"\x00")  # 根 End
        finally:
            if close_out:
                fileobj.close()
        return output
    finally:
        if block_path is not None:
            try:
                os.remove(block_path)
            except OSError:
                pass


def _count_palette(ws, progress: bool = False) -> int:
    """统计 ws 包围盒内唯一方块状态数（流式）。"""
    from ._progress import make_progress, refresh_memory_postfix

    seen = set()
    total = (ws.box.cx2 - ws.box.cx1 + 1) * (ws.box.cz2 - ws.box.cz1 + 1)
    bar = make_progress(total, "统计方块种类", "区块") if progress else None
    n = 0
    for cx in range(ws.box.cx1, ws.box.cx2 + 1):
        for cz in range(ws.box.cz1, ws.box.cz2 + 1):
            n += 1
            if bar is not None and n % 200 == 0:
                bar.update(200)
                refresh_memory_postfix(bar)
            for _x, _y, _z, name, states in ws._iter_chunk_block_states(cx, cz):
                seen.add((name, tuple(sorted(states.items()))))
    if bar is not None:
        bar.update(n - bar.n)
        refresh_memory_postfix(bar)
        bar.close()
    return len(seen)


def _tile_path(output, index: int) -> str:
    """分块输出路径：``建筑.schem`` → ``建筑_1.schem``。"""
    p = Path(output)
    return str(p.with_name(f"{p.stem}_{index}{p.suffix}"))


def _export_tile_or_split(world_path, box: Box, output, progress, results: list):
    """导出单个分块；若调色板仍超 256 则沿最长的 x/z 轴递归拆半。"""
    ws = open_world_source(world_path, box)
    try:
        n = _count_palette(ws, progress)
        if n <= 256:
            path = _tile_path(output, len(results) + 1)
            _world_schem_streaming(ws, path, progress)
            results.append(path)
            if progress:
                print(f"      分块 {path}（{n} 种方块）")
            return
    finally:
        ws.close()
    # 超过 256：沿最长的 x/z 轴拆成两半，递归
    dx = box.x2 - box.x1 + 1
    dz = box.z2 - box.z1 + 1
    if dx >= dz and dx > 1:
        mid = (box.x1 + box.x2) // 2
        _export_tile_or_split(world_path, Box(box.x1, box.y1, box.z1, mid, box.y2, box.z2), output, progress, results)
        _export_tile_or_split(world_path, Box(mid + 1, box.y1, box.z1, box.x2, box.y2, box.z2), output, progress, results)
    elif dz > 1:
        mid = (box.z1 + box.z2) // 2
        _export_tile_or_split(world_path, Box(box.x1, box.y1, box.z1, box.x2, box.y2, mid), output, progress, results)
        _export_tile_or_split(world_path, Box(box.x1, box.y1, mid + 1, box.x2, box.y2, box.z2), output, progress, results)
    else:
        raise ValueError(f"单个区块列仍超过 256 种方块，无法再拆分: {box}")


def world_to_schem(world_path, output, box, *, progress=False, split=False):
    """把包围盒导出为 Java Sponge .schem（世界坐标作为 Offset），流式内存有界。

    ``split``（-s）：区域方块种类超过 Sponge 调色板上限 256 时，自动沿 x/z 轴
    递归拆分成多个 ``<名>_N.schem``，每个 ≤256 种。返回文件路径列表。
    """
    box = _coerce_box(box)
    if not split:
        ws = open_world_source(world_path, box)
        try:
            return _world_schem_streaming(ws, output, progress)
        finally:
            ws.close()
    results: list = []
    _export_tile_or_split(world_path, box, output, progress, results)
    return results


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _world_chunks(box: Box) -> list[tuple[int, int, int, int]]:
    """世界对齐的 16×16 区块列（一个区块一列，供 S 型遍历）。"""
    chunks = []
    for cx in range(box.cx1, box.cx2 + 1):
        for cz in range(box.cz1, box.cz2 + 1):
            chunks.append((cx * 16, cx * 16 + 15, cz * 16, cz * 16 + 15))
    return chunks
