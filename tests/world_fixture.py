"""世界导出测试夹具：在内存中生成合成的 Java Anvil 世界文件夹（region/*.mca）。

1.18+ chunk NBT（DataVersion 3465）：``sections[].block_states``（palette + 位压缩 data）。

世界布局（用于坐标断言）：
- region ``r.0.0.mca``
  - chunk (0,0)：
    - section Y=-1（y -16..-1）：stone @ (1, -1, 2)
    - section Y=0（y 0..15）：stone @ (3, 1, 4)；red_wool @ (10, 2, 9)
    - section Y=1（y 16..31）：command_block @ (2, 17, 3)（带方块实体）
  - chunk (0,1)（z 16..31）：
    - section Y=0：stone @ (5, 1, 20)
- region ``r.-1.0.mca``
  - chunk (-1,0)（x -16..-1）：
    - section Y=0：stone @ (-2, 1, 3)
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import nbtlib
import numpy as np


def _nbt_value(v):
    if isinstance(v, bool):
        return nbtlib.Byte(int(v))
    if isinstance(v, int):
        return nbtlib.Int(v)
    return nbtlib.String(str(v))


def _pack_bits(values: np.ndarray, bits: int) -> list[int]:
    """把 count 个调色板索引按 bits 位/格压缩进 long 数组（逆操作，供夹具用）。"""
    count = int(values.size)
    n_longs = (count * bits + 63) // 64
    longs = [0] * n_longs
    mask = (1 << bits) - 1
    mask64 = (1 << 64) - 1
    for i, v in enumerate(values):
        bit_start = i * bits
        long_idx = bit_start >> 6
        bit_off = bit_start & 63
        longs[long_idx] = (longs[long_idx] | ((int(v) & mask) << bit_off)) & mask64
        if bit_off + bits > 64:
            longs[long_idx + 1] = (longs[long_idx + 1] | (int(v) >> (64 - bit_off))) & mask64
    return [x if x < (1 << 63) else x - (1 << 64) for x in longs]


def _section(sy: int, blocks: dict) -> nbtlib.Compound:
    """构建一个 1.18+ section。

    ``blocks``: ``{(lx, ly, lz): (name, states)}``，坐标相对 section 原点（0..15）。
    调色板索引 0 固定为 air（与 Minecraft 一致），其余格默认空气。
    """
    palette_entries: list[tuple[str, dict]] = [("air", {})]
    pmap: dict[tuple, int] = {("air", ()): 0}
    values = np.zeros(4096, dtype=np.int64)
    for (lx, ly, lz), (name, states) in blocks.items():
        key = (name, tuple(sorted(states.items())))
        if key not in pmap:
            pmap[key] = len(palette_entries)
            palette_entries.append((name, states))
        values[(ly << 8) | (lz << 4) | lx] = pmap[key]
    bits = max(4, (len(palette_entries) - 1).bit_length())

    palette_tag = nbtlib.List(
        [
            nbtlib.Compound(
                {
                    "Name": nbtlib.String(f"minecraft:{name}"),
                    **(
                        {
                            "Properties": nbtlib.Compound(
                                {k: nbtlib.String(str(v)) for k, v in states.items()}
                            )
                        }
                        if states
                        else {}
                    ),
                }
            )
            for name, states in palette_entries
        ]
    )
    return nbtlib.Compound(
        {
            "Y": nbtlib.Byte(sy),
            "block_states": nbtlib.Compound(
                {
                    "palette": palette_tag,
                    "data": nbtlib.List([nbtlib.Long(v) for v in _pack_bits(values, bits)]),
                }
            ),
        }
    )


def _chunk(cx: int, cz: int, sections: dict, block_entities: list | None = None) -> nbtlib.Compound:
    """构建 1.18+ chunk 根 Compound。

    ``sections``: ``{sy: {(lx, ly, lz): (name, states)}}``。
    """
    return nbtlib.Compound(
        {
            "DataVersion": nbtlib.Int(3465),
            "xPos": nbtlib.Int(cx),
            "yPos": nbtlib.Int(min(sections) if sections else 0),
            "zPos": nbtlib.Int(cz),
            "Status": nbtlib.String("full"),
            "sections": nbtlib.List([_section(sy, b) for sy, b in sorted(sections.items())]),
            "block_entities": nbtlib.List(
                [
                    nbtlib.Compound({k: _nbt_value(v) for k, v in be.items()})
                    for be in (block_entities or [])
                ]
            ),
        }
    )


def _chunk_nbt_bytes(root: nbtlib.Compound) -> bytes:
    buf = io.BytesIO()
    nbtlib.File(root, byteorder="big").write(buf, byteorder="big")
    return buf.getvalue()


def _build_region(chunks: dict) -> bytes:
    """把 ``{(cx, cz): chunk root}`` 组装成一个 .mca 文件字节。"""
    header = bytearray(8192)
    body = bytearray()
    entries = []
    for (cx, cz), root in chunks.items():
        comp = zlib.compress(_chunk_nbt_bytes(root))
        rec = struct.pack(">I", len(comp) + 1) + bytes([2]) + comp  # length 含压缩类型字节
        pad = (-len(body)) % 4096
        body += b"\x00" * pad
        sector = (len(header) + len(body)) // 4096
        body += rec
        size = (len(rec) + 4095) // 4096
        entries.append(((cx & 31) + ((cz & 31) * 32), sector, size))
    for idx, sector, size in entries:
        header[idx * 4:idx * 4 + 3] = bytes(((sector >> 16) & 0xFF, (sector >> 8) & 0xFF, sector & 0xFF))
        header[idx * 4 + 3] = size
    return bytes(header) + bytes(body)


def _make_chunk_00():
    return _chunk(
        0, 0,
        sections={
            -1: {(1, 15, 2): ("stone", {})},  # world (1, -1, 2)
            0: {
                (3, 1, 4): ("stone", {}),  # world (3, 1, 4)
                (10, 2, 9): ("red_wool", {"color": "red"}),  # world (10, 2, 9)
            },
            1: {
                (2, 1, 3): (  # world (2, 17, 3)
                    "command_block",
                    {"facing": "north", "conditional": "true"},
                ),
            },
        },
        block_entities=[
            {
                "id": "minecraft:command_block",
                "x": 2, "y": 17, "z": 3,
                "Command": "say hi",
                "CustomName": '{"text":"入口"}',
                "auto": 1,
                "TrackOutput": 1,
                "conditionMet": 0,
            },
        ],
    )


def _make_chunk_01():
    return _chunk(0, 1, sections={0: {(5, 1, 4): ("stone", {})}})  # world (5, 1, 20)


def _make_chunk_minus_1_0():
    return _chunk(-1, 0, sections={0: {(14, 1, 3): ("stone", {})}})  # world (-2, 1, 3)


def make_world(tmp_path: Path) -> Path:
    """在 ``tmp_path`` 下创建合成世界文件夹，返回世界路径。"""
    world = Path(tmp_path) / "world"
    region_dir = world / "region"
    region_dir.mkdir(parents=True, exist_ok=True)
    (region_dir / "r.0.0.mca").write_bytes(
        _build_region({(0, 0): _make_chunk_00(), (0, 1): _make_chunk_01()})
    )
    (region_dir / "r.-1.0.mca").write_bytes(
        _build_region({(-1, 0): _make_chunk_minus_1_0()})
    )
    return world


def make_world_many_types(tmp_path: Path) -> Path:
    """一个 2×2 区块世界，共 260 种不同方块（>Sponge 调色板 256 上限），供 -s 拆分测试。"""
    names = [f"block_{i}" for i in range(260)]
    chunks = {}
    for ci in range(4):
        cx, cz = ci % 2, ci // 2
        blocks = {}
        for i in range(65):
            idx = ci * 65 + i
            lx, ly, lz = i % 16, (i // 16) % 2, i // 32  # 坐标互不重复
            blocks[(lx, ly, lz)] = (names[idx], {})
        chunks[(cx, cz)] = _chunk(cx, cz, {0: blocks})
    world = Path(tmp_path) / "many_world"
    region_dir = world / "region"
    region_dir.mkdir(parents=True, exist_ok=True)
    (region_dir / "r.0.0.mca").write_bytes(_build_region(chunks))
    return world
