"""测试夹具：在内存中生成合成 mcstructure / bdx 建筑文件字节。

不依赖真实建筑文件即可测试读取/输出全链路。
"""

from __future__ import annotations

import io
import struct
from typing import Any

import nbtlib


# ---------------------------------------------------------------------------
# mcstructure
# ---------------------------------------------------------------------------
def _nbt_value(v: Any) -> nbtlib.tag.Base:
    if isinstance(v, bool):
        return nbtlib.Byte(int(v))
    if isinstance(v, int):
        return nbtlib.Int(v)
    if isinstance(v, str):
        return nbtlib.String(v)
    raise TypeError(f"不支持的 NBT 值类型: {type(v)!r}")


def make_mcstructure_bytes() -> bytes:
    """一个 3×3×1 的小结构：3 个命令方块（脉冲/连锁/重复）+ 2 个普通方块。"""
    size = (3, 3, 1)
    X, Y, Z = size
    blocks: dict[tuple[int, int, int], tuple[str, dict, dict | None]] = {
        (0, 0, 0): (
            "command_block",
            {},
            {"Command": "say hello", "CustomName": "入口", "TickDelay": 0,
             "Conditional": 0, "auto": 1, "TrackOutput": 1},
        ),
        (1, 1, 0): (
            "chain_command_block",
            {"conditional_bit": 1},
            {"Command": "execute as @a run say hi", "TickDelay": 4,
             "Conditional": 1, "auto": 0, "TrackOutput": 1},
        ),
        (2, 2, 0): (
            "repeating_command_block",
            {},
            {"Command": "say loop", "TickDelay": 10, "Conditional": 0, "auto": 0},
        ),
        (2, 0, 0): ("stone", {"pillar_axis": 0}, None),
        (0, 2, 0): ("red_wool", {"color": "red"}, None),
    }

    palette: list[nbtlib.Compound] = []
    pmap: dict[tuple, int] = {}
    indices: list[int] = [-1] * (X * Y * Z)
    posdata: dict[str, nbtlib.Compound] = {}

    for (x, y, z), (name, states, entity) in blocks.items():
        idx = z + y * Z + x * Y * Z
        key = (name, tuple(sorted(states.items())))
        if key not in pmap:
            pmap[key] = len(palette)
            palette.append(
                nbtlib.Compound(
                    {
                        "name": nbtlib.String(name),
                        "states": nbtlib.Compound(
                            {k: _nbt_value(v) for k, v in states.items()}
                        ),
                    }
                )
            )
        indices[idx] = pmap[key]
        if entity:
            posdata[str(idx)] = nbtlib.Compound(
                {"block_entity_data": nbtlib.Compound({k: _nbt_value(v) for k, v in entity.items()})}
            )

    root = nbtlib.Compound(
        {
            "format_version": nbtlib.Int(1),
            "size": nbtlib.List([nbtlib.Int(v) for v in size]),
            "structure": nbtlib.Compound(
                {
                    "block_indices": nbtlib.List(
                        [
                            nbtlib.List([nbtlib.Int(v) for v in indices]),
                            nbtlib.List([nbtlib.Int(-1) for _ in indices]),
                        ]
                    ),
                    "entities": nbtlib.List([]),
                    "palette": nbtlib.Compound(
                        {
                            "default": nbtlib.Compound(
                                {
                                    "block_palette": nbtlib.List(palette),
                                    "block_position_data": nbtlib.Compound(posdata),
                                }
                            )
                        }
                    ),
                    "size": nbtlib.List([nbtlib.Int(v) for v in size]),
                }
            ),
        }
    )
    f = nbtlib.File(root, byteorder="little")
    buf = io.BytesIO()
    f.write(buf, byteorder="little")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# schematic（经典 Java 版）
# ---------------------------------------------------------------------------
def make_schematic_bytes() -> bytes:
    """一个 3×3×1 的 classic .schematic（gzip + 大端 NBT）。

    方块：stone(1)、command_block(137)、chain_command_block(211)。
    """
    import gzip

    X, Y, Z = 3, 3, 1
    n = X * Y * Z
    # Java 方块 ID（命令方块 137，连锁 211）
    ids = [0] * n
    data = [0] * n

    def idx(x, y, z):
        return (y * Z + z) * X + x

    ids[idx(0, 0, 0)] = 1  # stone
    ids[idx(1, 1, 0)] = 137  # command_block
    data[idx(1, 1, 0)] = 4  # 朝西
    ids[idx(2, 2, 0)] = 211  # chain_command_block
    data[idx(2, 2, 0)] = 3  # 朝南

    # classic schematic 的 Blocks 用有符号字节存储，>127 需转补码
    def _signed(v: int) -> int:
        return v if v < 128 else v - 256

    tile_entities = nbtlib.List(
        [
            nbtlib.Compound(
                {
                    "id": nbtlib.String("Control"),
                    "x": nbtlib.Int(1),
                    "y": nbtlib.Int(1),
                    "z": nbtlib.Int(0),
                    "Command": nbtlib.String("say hello"),
                    "CustomName": nbtlib.String("入口"),
                    "auto": nbtlib.Byte(1),
                    "Conditional": nbtlib.Byte(0),
                    "TickDelay": nbtlib.Int(0),
                    "TrackOutput": nbtlib.Byte(1),
                }
            ),
            nbtlib.Compound(
                {
                    "id": nbtlib.String("Control"),
                    "x": nbtlib.Int(2),
                    "y": nbtlib.Int(2),
                    "z": nbtlib.Int(0),
                    "Command": nbtlib.String("execute as @a run say hi"),
                    "auto": nbtlib.Byte(0),
                    "Conditional": nbtlib.Byte(1),
                    "TickDelay": nbtlib.Int(4),
                    "TrackOutput": nbtlib.Byte(1),
                }
            ),
        ]
    )

    root = nbtlib.Compound(
        {
            "Width": nbtlib.Short(X),
            "Height": nbtlib.Short(Y),
            "Length": nbtlib.Short(Z),
            "Materials": nbtlib.String("Alpha"),
            "Blocks": nbtlib.ByteArray([_signed(v) for v in ids]),
            "Data": nbtlib.ByteArray([_signed(v) for v in data]),
            "Entities": nbtlib.List([]),
            "TileEntities": tile_entities,
        }
    )
    f = nbtlib.File(root, byteorder="big")
    buf = io.BytesIO()
    f.write(buf, byteorder="big")
    return gzip.compress(buf.getvalue())


# ---------------------------------------------------------------------------
# bdx
# ---------------------------------------------------------------------------
def _var_str(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def _cmd_data(
    mode: int,
    command: str,
    custom: str = "",
    last: str = "",
    delay: int = 0,
    eoft: int = 0,
    track: int = 1,
    cond: int = 0,
    rs: int = 1,
) -> bytes:
    return (
        struct.pack(">I", mode)
        + _var_str(command)
        + _var_str(custom)
        + _var_str(last)
        + struct.pack(">I", delay)
        + bytes([eoft, track, cond, rs])
    )


def make_bdx_bytes() -> bytes:
    """一个小 BDX：1 个 stone + 2 个命令方块（脉冲 / 连锁）。"""
    import brotli

    ops = bytearray()
    # 常量字符串池：0=stone, 1=command_block
    ops += bytes([1]) + _var_str("tile.stone")
    ops += bytes([1]) + _var_str("tile.command_block")
    # (0,0,0) 放 stone（op5 PlaceBlockWithBlockStates）
    ops += bytes([5]) + struct.pack(">HH", 0, 0)
    # (1,0,0) 放脉冲命令方块（op36，mode=0，data=4 → 朝西）
    ops += bytes([14])
    ops += bytes([36]) + struct.pack(">H", 4) + _cmd_data(0, "say impulse", "impulseCB", delay=2, rs=0)
    # (2,0,0) 放连锁命令方块（op36，mode=2，data=3 → 朝南）
    ops += bytes([14])
    ops += bytes([36]) + struct.pack(">H", 3) + _cmd_data(2, "say chain", cond=1)
    # 结束标记
    ops += bytes([88])

    payload = b"BDX\x00" + _var_str("TestAuthor") + bytes(ops)
    return b"BD@" + brotli.compress(payload)
