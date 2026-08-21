"""Bedrock 世界导出测试夹具：在内存中生成合成 Bedrock 世界（db/*.ldb）。

- 写入一个标准 LevelDB .ldb 文件（数据块 + 索引块 + footer，zlib 压缩），
  键为「用户键 + 8 字节内部后缀」。
- 子区块值按 Bedrock v9 格式编码（版本字节 + 层数 + Y + 位压缩词 + 小端 NBT 调色板），
  位压缩词用与 amulet（真实世界往返验证过的编码器）等价的算法。

世界布局（与 tests/world_fixture.py 的 Java 世界一致，便于对照）：
- chunk (0,0)：子区块 Y=-1（stone @ (1,-1,2)）、Y=0（stone @ (3,1,4)、
  red_wool @ (10,2,9)）、Y=1（command_block @ (2,17,3)，带方块实体）
- chunk (0,1)：Y=0（stone @ (5,1,20)）
- chunk (-1,0)：Y=0（stone @ (-2,1,3)）
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import nbtlib
import numpy as np


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _nbt_bytes(root: nbtlib.Compound) -> bytes:
    buf = io.BytesIO()
    nbtlib.File(root, byteorder="little").write(buf, byteorder="little")
    return buf.getvalue()


def _nbt_value(v):
    if isinstance(v, bool):
        return nbtlib.Byte(int(v))
    if isinstance(v, int):
        return nbtlib.Int(v)
    return nbtlib.String(str(v))


# ---------------------------------------------------------------------------
# 位压缩词编码（移植 amulet._encode_packed_array，真实世界往返验证过的字节布局）
# ---------------------------------------------------------------------------
def _pack_palette_indices(linear_values: np.ndarray, min_bit_size: int = 1) -> tuple[int, bytes]:
    """把 4096 个调色板索引（线性序 i = x*256+z*16+y，y 最快）编码为 on-disk 字节。

    返回 ``(bits, packed)``——bits 是实际使用的位宽（含 7→8、9-15→16 取整）。
    """
    arr_xyz = linear_values.reshape(16, 16, 16).transpose(0, 2, 1)  # [x][y][z]
    bits = max(int(np.amax(arr_xyz)).bit_length(), min_bit_size)
    if bits == 7:
        bits = 8
    elif 9 <= bits <= 15:
        bits = 16
    values_per_word = 32 // bits
    word_count = -(-4096 // values_per_word)
    a = arr_xyz.swapaxes(1, 2).ravel()
    packed = bytes(
        reversed(
            np.packbits(
                np.pad(
                    np.pad(
                        np.unpackbits(
                            np.ascontiguousarray(a[::-1], dtype=">i").view(dtype="uint8")
                        ).reshape(4096, -1)[:, -bits:],
                        [(word_count * values_per_word - 4096, 0), (0, 0)],
                        "constant",
                    ).reshape(-1, values_per_word * bits),
                    [(0, 0), (32 - values_per_word * bits, 0)],
                    "constant",
                )
            ).view(dtype=">i4").tobytes()
        )
    )
    return bits, packed


def _encode_subchunk(blocks: dict, sub_y: int | None = None, version: int = 9) -> bytes:
    """编码一个子区块值（v9 默认）。

    ``blocks``: ``{(lx, ly, lz): (name, states)}``，坐标相对子区块原点（0..15）。
    调色板索引 0 固定为 air（与 Bedrock 一致）。
    """
    palette: list[tuple[str, dict]] = [("air", {})]
    pmap: dict[tuple, int] = {("air", ()): 0}
    values = np.zeros(4096, dtype=np.int64)
    for (lx, ly, lz), (name, states) in blocks.items():
        key = (name, tuple(sorted(states.items())))
        if key not in pmap:
            pmap[key] = len(palette)
            palette.append((name, states))
        values[(lx << 8) | (lz << 4) | ly] = pmap[key]  # Bedrock 线性序（y 最快）

    bits, packed = _pack_palette_indices(values)
    layer = bytes([bits << 1]) + packed
    layer += struct.pack("<I", len(palette))
    layer += b"".join(
        _nbt_bytes(
            nbtlib.Compound(
                {
                    "name": nbtlib.String(f"minecraft:{name}"),
                    **(
                        {
                            "states": nbtlib.Compound(
                                {k: _nbt_value(v) for k, v in states.items()}
                            )
                        }
                        if states
                        else {}
                    ),
                    "version": nbtlib.Int(18032817),
                }
            )
        )
        for name, states in palette
    )

    header = bytes([version])
    if version == 9:
        header += bytes([1]) + struct.pack("b", sub_y)
    elif version == 8:
        header += bytes([1])
    # version 1：无层数/Y 字节
    return header + layer


def _internal_key(user_key: bytes, seq: int = 1, ktype: int = 1) -> bytes:
    """用户键 → 内部键（用户键 + [type:1][seq:7] 小端 u64 后缀）。"""
    return user_key + bytes([ktype]) + struct.pack("<Q", seq)[:7]


def _subchunk_key(cx: int, cz: int, cy: int) -> bytes:
    return struct.pack("<ii", cx, cz) + bytes([0x2F]) + struct.pack("b", cy)


def _block_entity_key(cx: int, cz: int) -> bytes:
    return struct.pack("<ii", cx, cz) + bytes([0x31])


def _version_key(cx: int, cz: int) -> bytes:
    return struct.pack("<ii", cx, cz) + bytes([0x2C])


# ---------------------------------------------------------------------------
# LevelDB .ldb 写入
# ---------------------------------------------------------------------------
def _write_block(entries: list, compress: int = 4) -> tuple[bytes, int]:
    """把一个数据块编码为完整块，返回 ``(块字节, 内容长度)``。

    Bedrock LevelDB 块尾 = ``[内容][type:1][crc:4]``，handle.size = 内容长度。
    ``compress``：4 = raw deflate（Bedrock 常用）、2 = zlib、0 = 无压缩。
    """
    body = bytearray()
    restarts = []
    for key, value in entries:
        restarts.append(len(body))
        # 简化：每项独立（shared=0），每项都是重启点（合法且便于测试）
        body += _varint(0) + _varint(len(key)) + _varint(len(value))
        body += key + value
    body += b"".join(struct.pack("<I", r) for r in restarts)
    body += struct.pack("<I", len(restarts))
    data = bytes(body)
    if compress == 0:
        content = data
    elif compress == 2:
        content = zlib.compress(data)
    else:  # 4 = raw deflate（无 zlib 头）
        co = zlib.compressobj(level=6, wbits=-15)
        content = co.compress(data) + co.flush()
    block = content + bytes([compress]) + b"\x00\x00\x00\x00"  # [内容][type][crc(假)]
    return block, len(content)


def _build_ldb(chunk_data: dict, compress: int = 4) -> bytes:
    """把 ``{用户键: 值}`` 写入一个标准 Bedrock LevelDB .ldb 文件字节。"""
    items = sorted((_internal_key(k), v) for k, v in chunk_data.items())
    blocks = [items[i:i + 16] for i in range(0, len(items), 16)] or [[]]

    body = bytearray()
    index_entries = []
    for blk in blocks:
        block_bytes, content_len = _write_block(blk, compress)
        offset = len(body)
        body += block_bytes
        if blk:
            handle = _varint(offset) + _varint(content_len)
            index_entries.append((blk[-1][0], handle))

    meta_off = len(body)
    meta_block, meta_len = _write_block([], compress)
    body += meta_block
    index_off = len(body)
    index_block, index_len = _write_block(index_entries, compress)
    body += index_block

    meta_handle = _varint(meta_off) + _varint(meta_len)
    index_handle = _varint(index_off) + _varint(index_len)
    pad = 48 - len(meta_handle) - len(index_handle) - 8
    footer = meta_handle + index_handle + b"\x00" * pad + b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
    body += footer
    return bytes(body)


# ---------------------------------------------------------------------------
# 世界构建
# ---------------------------------------------------------------------------
def _chunk_00():
    be = _nbt_bytes(
        nbtlib.Compound(
            {
                "id": nbtlib.String("CommandBlock"),
                "x": nbtlib.Int(2), "y": nbtlib.Int(17), "z": nbtlib.Int(3),
                "Command": nbtlib.String("say hi"),
                "CustomName": nbtlib.String("入口"),
                "auto": nbtlib.Byte(1),
                "TrackOutput": nbtlib.Byte(1),
                "ExecuteOnFirstTick": nbtlib.Byte(0),
                "TickDelay": nbtlib.Int(0),
            }
        )
    )
    return {
        _subchunk_key(0, 0, -1): _encode_subchunk({(1, 15, 2): ("stone", {})}, sub_y=-1),
        _subchunk_key(0, 0, 0): _encode_subchunk({
            (3, 1, 4): ("stone", {}),
            (10, 2, 9): ("red_wool", {"color": "red"}),
        }, sub_y=0),
        _subchunk_key(0, 0, 1): _encode_subchunk({
            (2, 1, 3): ("command_block", {"facing_direction": 2, "conditional_bit": 1}),
        }, sub_y=1),
        _block_entity_key(0, 0): be,
        _version_key(0, 0): bytes([21]),  # 1.18+ chunk version
    }


def _chunk_01():
    return {
        _subchunk_key(0, 1, 0): _encode_subchunk({(5, 1, 4): ("stone", {})}, sub_y=0),
        _version_key(0, 1): bytes([21]),
    }


def _chunk_minus_1_0():
    return {
        _subchunk_key(-1, 0, 0): _encode_subchunk({(14, 1, 3): ("stone", {})}, sub_y=0),
        _version_key(-1, 0): bytes([21]),
    }


def make_bedrock_world(tmp_path: Path) -> Path:
    """在 ``tmp_path`` 下创建合成 Bedrock 世界文件夹，返回世界路径。"""
    world = Path(tmp_path) / "bedrock_world"
    db_dir = world / "db"
    db_dir.mkdir(parents=True, exist_ok=True)

    chunk_data = {}
    chunk_data.update(_chunk_00())
    chunk_data.update(_chunk_01())
    chunk_data.update(_chunk_minus_1_0())
    (db_dir / "000001.ldb").write_bytes(_build_ldb(chunk_data))

    # level.dat：只是存在性标记（导出不解析内容）
    (world / "level.dat").write_bytes(b"\x08\x00\x00\x00")
    (world / "levelname.txt").write_text("Test Bedrock World", encoding="utf-8")
    return world
