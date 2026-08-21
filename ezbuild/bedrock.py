"""Minecraft 基岩版世界文件夹导出：按 xyz 包围盒流式提取建筑。

存档结构：``level.dat`` + ``db/``（LevelDB：``*.ldb`` / ``*.log``）。
区块数据按 LevelDB 键存放（键 = ``[chunk_x: i32 LE][chunk_z: i32 LE][标签]``）：

- 子区块 ``0x2F``：``[0x2F][子区块 Y: i8]``，值为 v1/v8/v9 格式
  （版本字节 + 层数 + （v9 的 Y） + 每层「位宽字节 + 位压缩词 + 小端 NBT 调色板」）。
- 版本 ``0x2C``（新）/ ``0x76``（旧）：值为 1 字节 chunk 版本号。
- 方块实体 ``0x31``：值为连续拼接的小端 NBT 根。

调色板条目是标准小端 NBT：``{name: String, states: Compound, version: Int}``；
方块状态已是 Bedrock 风格（``facing_direction`` / ``conditional_bit``），直接用。

性能与内存：只读包围盒相关 chunk 的 LevelDB 键（leveldb.read_db 选择性解压），
逐子区块解包，内存 ≈ 范围内数据，与整个存档大小无关。
"""

from __future__ import annotations

import io
import struct
from collections import OrderedDict
from pathlib import Path

import nbtlib
import numpy as np

from . import leveldb
from .model import CommandBlock, COMMAND_BLOCK_MODES
from .utils import normalize_block_name, tag_to_python
from .world import Box


# ---------------------------------------------------------------------------
# 子区块位压缩词解码
# ---------------------------------------------------------------------------
def _unpack_section(bits: int, data: bytes) -> np.ndarray:
    """把子区块 packed words 解包为 ``(16,16,16)`` [y][z][x] 调色板索引数组。

    on-disk 字节 = 大端 32 位词字节序列整体反转；词内按低位起逐 ``bits`` 位取值。
    线性序 y 最快（i = x*256 + z*16 + y）→ 解码得 (x,y,z)，转置成 [y][z][x]。
    """
    if bits <= 0 or not data:
        return np.zeros((16, 16, 16), dtype=np.int32)
    values_per_word = 32 // bits
    word_count = -(-4096 // values_per_word)
    arr = np.packbits(
        np.pad(
            np.unpackbits(
                np.frombuffer(bytes(reversed(data[: 4 * word_count])), dtype="uint8")
            ).reshape(-1, 32)[:, -values_per_word * bits:].reshape(-1, bits)[-4096:, :],
            [(0, 0), (16 - bits, 0)],
            "constant",
        )
    ).view(dtype=">i2")[::-1]
    return np.ascontiguousarray(arr.reshape(16, 16, 16).swapaxes(1, 2).transpose(1, 2, 0))


# ---------------------------------------------------------------------------
# 小端 NBT 调色板 / 方块实体读取
# ---------------------------------------------------------------------------
def _read_nbt_roots(data: bytes, count: int | None = None) -> list:
    """顺序读取连续的小端 NBT 根，返回 nbtlib Compound 列表。

    ``count``：读固定个数（子区块调色板）；None = 读到数据耗尽（方块实体列表）。
    """
    buf = io.BytesIO(data)
    out = []
    while count is None or len(out) < count:
        try:
            root = nbtlib.File.parse(buf, byteorder="little")
        except Exception:
            break
        out.append(root)
    return out


def _palette_entry(root) -> tuple[str, dict]:
    """子区块调色板 NBT 条目 → (方块名, Bedrock 状态 dict)。"""
    name = normalize_block_name(str(root.get("name", "")))
    states = root.get("states")
    states = tag_to_python(states) if states is not None else {}
    return name, states


def _decode_subchunk_palette(value: bytes):
    """解析一个子区块值，返回 ``(y_index, idx[16,16,16], palette)``。

    palette: ``[(name, states)]``（索引对应 idx 值，0 默认空气）。
    仅取第 0 层（主方块层）。v1/v8/v9 之外抛错。
    """
    version = value[0]
    if version == 1:
        storage_count = 1
        data = value[1:]
        cy = None
    elif version == 8:
        storage_count = value[1]
        data = value[2:]
        cy = None
    elif version == 9:
        storage_count = value[1]
        cy = struct.unpack("b", value[2:3])[0]
        data = value[3:]
    else:
        raise ValueError(
            f"不支持的 Bedrock 子区块版本 {version}（支持 v1/v8/v9，即 1.18+ 存档）"
        )
    if storage_count < 1:
        return cy, np.zeros((16, 16, 16), dtype=np.int32), [("air", {})]

    # 第 0 层
    storage = data[0]
    bits = storage >> 1  # 低 1 位为标志位（0=NBT 调色板）
    data = data[1:]
    if bits > 0:
        values_per_word = 32 // bits
        word_count = (4096 + values_per_word - 1) // values_per_word
        packed = data[: 4 * word_count]
        data = data[4 * word_count:]
        if len(data) < 4:
            return cy, np.zeros((16, 16, 16), dtype=np.int32), [("air", {})]
        palette_len = struct.unpack("<I", data[:4])[0]
        data = data[4:]
        palette = [_palette_entry(r) for r in _read_nbt_roots(data[:], count=palette_len)]
        palette = (palette + [("air", {})] * palette_len)[:max(palette_len, 1)]
        idx = _unpack_section(bits, packed)
        return cy, idx, palette
    else:
        # 无位压缩（存储字节 0/1，8/16 位无调色板的旧式）：整个子区块是单一方块
        palette = [("air", {})]
        if len(data) >= 4:
            pal_count = struct.unpack("<I", data[:4])[0]
            roots = _read_nbt_roots(data[4:], count=pal_count)
            if roots:
                palette = [_palette_entry(r) for r in roots]
        idx = np.zeros((16, 16, 16), dtype=np.int32)
        return cy, idx, palette


# ---------------------------------------------------------------------------
# BedrockWorldSource：与 Java WorldSource 相同的对外接口
# ---------------------------------------------------------------------------
class BedrockWorldSource:
    """流式方块源：从 Bedrock 世界文件夹按包围盒逐子区块提取方块。

    与 :class:`~ezbuild.world.WorldSource` 接口一致（get_region / iter_all_blocks /
    command_blocks / build_box_array），供 world_to_* 复用。
    """

    def __init__(self, world_path, box, strip_states: frozenset | None = None):
        world = Path(world_path)
        db_dir = world / "db"
        if not db_dir.is_dir():
            raise ValueError(f"不是有效的 Bedrock 版世界文件夹（缺少 db/ 目录）: {world}")
        self.world = world
        self.box = box if isinstance(box, Box) else Box(*box)
        self.strip_states = strip_states
        self._db_dir = db_dir

        # 流式 LevelDB：只解析索引块，数据块按需读取（内存有界）
        self._ldb = leveldb.LevelDB(db_dir)

        # 按 chunk 缓存解码结果（LRU 上限，避免大包围盒占满内存）
        self._chunk_cache: "OrderedDict[tuple[int, int], object]" = OrderedDict()
        self._fmt_cache: dict[tuple, str] = {}
        self._max_cached_chunks = 128

        # box 数组（惰性，mcstructure/schem 用；裁剪后只含实际内容）
        self.array3: np.ndarray | None = None
        self.parsed: dict[int, tuple[str, dict]] = {}
        self.air = 0
        self.ox, self.oy, self.oz = self.box.x1, self.box.y1, self.box.z1
        self.W = self.box.x2 - self.box.x1 + 1
        self.H = self.box.y2 - self.box.y1 + 1
        self.L = self.box.z2 - self.box.z1 + 1

    # ------------------------------------------------------------------ chunk 数据
    def _chunk_data(self, cx: int, cz: int) -> dict[bytes, bytes]:
        """返回 (cx, cz) 的子键 → 值（键去掉 8 字节 chunk 前缀）。"""
        prefix = struct.pack("<ii", cx, cz)
        out = {}
        for k, v in self._ldb.collect(prefix).items():
            if len(k) <= len(prefix) + 2:
                out[k[len(prefix):]] = v
        return out

    def _decode_chunk(self, cx: int, cz: int):
        """解码一个 chunk 的所有子区块，返回 ``{cy: (idx[16,16,16], palette)}``。"""
        cache_key = (cx, cz)
        cached = self._chunk_cache.get(cache_key)
        if cached is not None:
            self._chunk_cache.move_to_end(cache_key)
            return cached
        data = self._chunk_data(cx, cz)
        subchunks: dict[int, tuple[np.ndarray, list]] = {}
        for k, v in data.items():
            if len(k) == 2 and k[0] == 0x2F:
                cy = struct.unpack("b", k[1:2])[0]
                try:
                    y_idx, idx, palette = _decode_subchunk_palette(v)
                except (ValueError, IndexError):
                    continue
                if y_idx is not None and y_idx != cy:
                    cy = y_idx  # v9 内嵌 Y 优先
                subchunks[cy] = (idx, palette)
        self._chunk_cache[cache_key] = subchunks
        if len(self._chunk_cache) > self._max_cached_chunks:
            self._chunk_cache.popitem(last=False)  # 淘汰最旧，内存有界
        return subchunks

    def _block_at(self, cx: int, cz: int, x: int, y: int, z: int) -> tuple[str, dict]:
        """取世界坐标所在方块的 (name, states)；查不到返回 ("air", {})。"""
        subchunks = self._decode_chunk(cx, cz)
        cy = y >> 4
        info = subchunks.get(cy)
        if info is None:
            return "air", {}
        idx, palette = info
        v = int(idx[y & 15, z & 15, x & 15])
        if 0 <= v < len(palette):
            return palette[v]
        return "air", {}

    def _state_str(self, name: str, states: dict) -> str:
        from .utils import format_block_states

        key = (name, tuple(sorted(states.items())))
        s = self._fmt_cache.get(key)
        if s is None:
            s = format_block_states(states, self.strip_states)
            self._fmt_cache[key] = s
        return s

    # ------------------------------------------------------------------ 对外迭代
    def get_region(self, sx: int, ex: int, sz: int, ez: int) -> dict:
        """一个 16×16 区块列的方块 dict：``{(列内x, 绝对y, 列内z): (name, 状态串)}``。"""
        cx, cz = sx >> 4, sz >> 4
        subchunks = self._decode_chunk(cx, cz)
        ylen = self.box.y2 - self.box.y1 + 1
        col = np.zeros((ylen, 16, 16), dtype=np.int32)
        entries: dict[int, tuple[str, dict]] = {}
        key_to_id: dict[tuple, int] = {}
        for cy, (idx, palette) in subchunks.items():
            ly0, ly1 = max(cy * 16, self.box.y1), min(cy * 16 + 15, self.box.y2)
            if ly0 > ly1:
                continue
            local_to_col = np.zeros(len(palette), dtype=np.int32)
            for li, (name, states) in enumerate(palette):
                if not name or name == "air":
                    continue
                key = (name, tuple(sorted(states.items())))
                eid = key_to_id.get(key)
                if eid is None:
                    eid = len(entries) + 1
                    key_to_id[key] = eid
                    entries[eid] = (name, states)
                local_to_col[li] = eid
            mapped = local_to_col[idx[(ly0 & 15):(ly1 & 15) + 1, :, :]]
            col[ly0 - self.box.y1:ly1 - self.box.y1 + 1, :, :] = mapped

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
        subchunks = self._decode_chunk(cx, cz)
        if not subchunks:
            return
        ylen = self.box.y2 - self.box.y1 + 1
        col = np.zeros((ylen, 16, 16), dtype=np.int32)
        entries: dict[int, tuple[str, dict]] = {}
        key_to_id: dict[tuple, int] = {}
        for cy, (idx, palette) in subchunks.items():
            ly0, ly1 = max(cy * 16, self.box.y1), min(cy * 16 + 15, self.box.y2)
            if ly0 > ly1:
                continue
            local_to_col = np.zeros(len(palette), dtype=np.int32)
            for li, (name, states) in enumerate(palette):
                if not name or name == "air":
                    continue
                key = (name, tuple(sorted(states.items())))
                eid = key_to_id.get(key)
                if eid is None:
                    eid = len(entries) + 1
                    key_to_id[key] = eid
                    entries[eid] = (name, states)
                local_to_col[li] = eid
            mapped = local_to_col[idx[(ly0 & 15):(ly1 & 15) + 1, :, :]]
            col[ly0 - self.box.y1:ly1 - self.box.y1 + 1, :, :] = mapped
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

    def _iter_chunk_blocks(self, cx: int, cz: int):
        """yield 一个区块列内、包围盒裁剪后的方块 ``(绝对x, 绝对y, 绝对z, name, 状态串)``。"""
        for x, y, z, name, states in self._iter_chunk_block_states(cx, cz):
            yield (x, y, z, name, self._state_str(name, states))

    def iter_all_blocks(self):
        """yield ``(绝对x, 绝对y, 绝对z, name, 格式化状态串)``。"""
        for cx in range(self.box.cx1, self.box.cx2 + 1):
            for cz in range(self.box.cz1, self.box.cz2 + 1):
                yield from self._iter_chunk_blocks(cx, cz)

    def command_blocks(self):
        """yield 包围盒内的命令方块（从 0x31 方块实体键提取）。"""
        from .streaming import _command_block_from_entity

        for cx in range(self.box.cx1, self.box.cx2 + 1):
            for cz in range(self.box.cz1, self.box.cz2 + 1):
                data = self._chunk_data(cx, cz)
                raw = data.get(b"\x31")
                if not raw:
                    continue
                for root in _read_nbt_roots(raw):
                    be = tag_to_python(root)
                    try:
                        x, y, z = int(be["x"]), int(be["y"]), int(be["z"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not self.box.contains(x, y, z):
                        continue
                    name, states = self._block_at(cx, cz, x, y, z)
                    if name not in COMMAND_BLOCK_MODES:
                        continue
                    yield _command_block_from_entity(x, y, z, name, states, be)

    # ------------------------------------------------------------------ box 数组（mcstructure/schem）
    def _scan_bounds(self, progress: bool = False):
        """第一遍扫描：返回非空气的世界坐标边界 ``(min_x, min_y, min_z, max_x, max_y, max_z)``
        或 None（整个包围盒全空气）。逐子区块 numpy 找边界，不分配盒数组。"""
        from ._progress import make_progress, refresh_memory_postfix

        INF = 1 << 60
        min_x = min_y = min_z = INF
        max_x = max_y = max_z = -INF
        total = (self.box.cx2 - self.box.cx1 + 1) * (self.box.cz2 - self.box.cz1 + 1)
        bar = make_progress(total, "扫描边界", "区块") if progress else None
        n = 0
        for cx in range(self.box.cx1, self.box.cx2 + 1):
            for cz in range(self.box.cz1, self.box.cz2 + 1):
                n += 1
                if bar is not None and n % 200 == 0:
                    bar.update(200)
                    refresh_memory_postfix(bar)
                for cy, (idx, _pal) in self._decode_chunk(cx, cz).items():
                    nz = np.argwhere(idx != 0)
                    if len(nz) == 0:
                        continue
                    wx0 = cx * 16 + int(nz[:, 2].min())
                    wx1 = cx * 16 + int(nz[:, 2].max())
                    wy0 = cy * 16 + int(nz[:, 0].min())
                    wy1 = cy * 16 + int(nz[:, 0].max())
                    wz0 = cz * 16 + int(nz[:, 1].min())
                    wz1 = cz * 16 + int(nz[:, 1].max())
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
        # 裁剪到包围盒
        min_x = max(min_x, self.box.x1); max_x = min(max_x, self.box.x2)
        min_y = max(min_y, self.box.y1); max_y = min(max_y, self.box.y2)
        min_z = max(min_z, self.box.z1); max_z = min(max_z, self.box.z2)
        return min_x, min_y, min_z, max_x, max_y, max_z

    def build_box_array(self, progress: bool = False) -> None:
        """构建包围盒内**实际内容**的 ``array3``（两遍：先扫边界，只分配裁剪后数组）。

        避免为超大包围盒一次性分配整盒数组（如 2000×2000×2000 会要 30GB+）。
        ``array3`` 即最终内容体积，``ox/oy/oz`` 更新为内容最小世界坐标。
        """
        from ._progress import make_progress, refresh_memory_postfix

        if self.array3 is not None:
            return
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

        # 第二遍：只填裁剪后的内容区域
        total = (self.box.cx2 - self.box.cx1 + 1) * (self.box.cz2 - self.box.cz1 + 1)
        bar = make_progress(total, "填充数组", "区块") if progress else None
        n = 0
        for cx in range(self.box.cx1, self.box.cx2 + 1):
            for cz in range(self.box.cz1, self.box.cz2 + 1):
                n += 1
                if bar is not None and n % 200 == 0:
                    bar.update(200)
                    refresh_memory_postfix(bar)
                subchunks = self._decode_chunk(cx, cz)
                if not subchunks:
                    continue
                ylen = self.box.y2 - self.box.y1 + 1
                col = np.zeros((ylen, 16, 16), dtype=np.int32)
                entries: dict[int, tuple[str, dict]] = {}
                key_to_id: dict[tuple, int] = {}
                for cy, (idx, palette_) in subchunks.items():
                    ly0, ly1 = max(cy * 16, self.box.y1), min(cy * 16 + 15, self.box.y2)
                    if ly0 > ly1:
                        continue
                    local_to_col = np.zeros(len(palette_), dtype=np.int32)
                    for li, (name, states) in enumerate(palette_):
                        if not name or name == "air":
                            continue
                        key = (name, tuple(sorted(states.items())))
                        eid = key_to_id.get(key)
                        if eid is None:
                            eid = len(entries) + 1
                            key_to_id[key] = eid
                            entries[eid] = (name, states)
                        local_to_col[li] = eid
                    mapped = local_to_col[idx[(ly0 & 15):(ly1 & 15) + 1, :, :]]
                    col[ly0 - self.box.y1:ly1 - self.box.y1 + 1, :, :] = mapped

                x0 = max(cx * 16, min_x)
                x1 = min(cx * 16 + 15, max_x)
                z0 = max(cz * 16, min_z)
                z1 = min(cz * 16 + 15, max_z)
                if x0 > x1 or z0 > z1:
                    continue
                max_id = max(entries, default=0)
                local_to_global = np.zeros(max_id + 1, dtype=np.int32)
                for eid, (name, states) in entries.items():
                    key = (name, tuple(sorted(states.items())))
                    gid = palette_map.get(key)
                    if gid is None:
                        gid = len(palette)
                        palette_map[key] = gid
                        palette.append((name, dict(states)))
                    local_to_global[eid] = gid
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
        self._chunk_cache.clear()
        self._ldb.close()
