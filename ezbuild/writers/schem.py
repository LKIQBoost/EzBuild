"""schem 输出器：把 Building 渲染为 Java 版 Sponge .schem 结构文件。

gzip 压缩的大端 NBT，根结构：``Version/DataVersion/Width/Height/Length/
Offset/Palette/BlockData/BlockEntities/Entities``。方块状态写为
``minecraft:{name}[{k}={v},...]``，命令方块转为 BlockEntities。
"""

from __future__ import annotations

import gzip
import io
from typing import Any

import nbtlib
from nbtlib.tag import ByteArray, Byte, Compound, Int, IntArray, List, String

from ..model import Block, Building, CommandBlock, COMMAND_BLOCK_IDS, COMMAND_BLOCK_MODES
from ..utils import bedrock_to_java_states
from .base import Writer

# Sponge 结构格式版本 / 数据版本（1.18.2）
SCHEM_VERSION = 2
DATA_VERSION = 2975

# Sponge ByteArray 调色板索引上限（每字节一个索引）
MAX_PALETTE = 256


def _format_blockstate(name: str, states: Any) -> str:
    """把 (方块名, 模型状态) 格式化为 Sponge 方块状态串（Bedrock→Java 转换）。"""
    if not isinstance(states, dict):
        states = {}
    java = bedrock_to_java_states(states)
    s = f"minecraft:{name}"
    if java:
        parts = []
        for key, val in java.items():
            if isinstance(val, bool):
                val_str = "true" if val else "false"
            elif isinstance(val, int):
                val_str = str(val)
            else:
                val_str = str(val)
            parts.append(f"{key}={val_str}")
        s += "[" + ",".join(parts) + "]"
    return s


class SchemWriter(Writer):
    format_name = "schem"
    extensions = (".schem",)
    description = "Minecraft Java 版 Sponge .schem 结构文件"

    def render(self, building: Building) -> bytes:
        cells = self._collect_cells(building)
        if not cells:
            W, H, L = 0, 0, 0
            offset = [0, 0, 0]
        else:
            xs = [c[0] for c in cells]
            ys = [c[1] for c in cells]
            zs = [c[2] for c in cells]
            min_x, min_y, min_z = min(xs), min(ys), min(zs)
            W, H, L = max(xs) - min_x + 1, max(ys) - min_y + 1, max(zs) - min_z + 1
            offset = [min_x, min_y, min_z]

        # 调色板（索引 0 = air）
        palette_names = ["minecraft:air"]
        palette_map = {"minecraft:air": 0}

        def pal_index(blockstate: str) -> int:
            if blockstate not in palette_map:
                palette_map[blockstate] = len(palette_names)
                palette_names.append(blockstate)
            return palette_map[blockstate]

        block_data = [0] * (W * H * L)  # 默认 air
        block_entities = []
        cmd_by_pos = {(c.x, c.y, c.z): c for c in building.command_blocks}

        for (x, y, z), blk in cells.items():
            rx, ry, rz = x - offset[0], y - offset[1], z - offset[2]
            if not (0 <= rx < W and 0 <= ry < H and 0 <= rz < L):
                continue
            blockstate = _format_blockstate(blk.name, blk.states)
            idx = pal_index(blockstate)
            block_data[rx + rz * W + ry * W * L] = idx

            if blk.name in COMMAND_BLOCK_MODES:
                cb = cmd_by_pos.get((x, y, z))
                if cb is not None:
                    block_entities.append(_make_entity(cb, rx, ry, rz, offset))

        if len(palette_map) > MAX_PALETTE:
            raise ValueError(
                f"方块种类 {len(palette_map)} 超过 Sponge .schem 调色板上限 "
                f"{MAX_PALETTE}（ByteArray 编码）"
            )
        # nbtlib ByteArray 是 int8（有符号），调色板索引 >127 需转成补码字节
        # （0-255 -> -128..127），读取端按无符号重解释回来。
        block_data = [v if v < 128 else v - 256 for v in block_data]

        root = Compound(
            {
                "Version": Int(SCHEM_VERSION),
                "DataVersion": Int(DATA_VERSION),
                "Width": Int(W),
                "Height": Int(H),
                "Length": Int(L),
                "Offset": IntArray(offset),
                "Palette": Compound(
                    {name: Int(idx) for name, idx in palette_map.items()}
                ),
                "BlockData": ByteArray(block_data),
                "BlockEntities": List(block_entities),
                "Entities": List([]),
            }
        )

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            nbtlib.File(root).write(gz, byteorder="big")
        return buf.getvalue()

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _collect_cells(building: Building) -> dict:
        cells = {(blk.x, blk.y, blk.z): blk for blk in building.blocks}
        # 命令方块缺方块条目时补一个
        for cb in building.command_blocks:
            pos = (cb.x, cb.y, cb.z)
            if pos not in cells:
                cells[pos] = Block(
                    x=cb.x, y=cb.y, z=cb.z,
                    name=COMMAND_BLOCK_IDS[cb.mode],
                    states={"facing_direction": 3},
                )
        return cells


def _make_entity(
    cb: CommandBlock, rx: int, ry: int, rz: int, offset
) -> Compound:
    """CommandBlock → Java 命令方块 BlockEntity NBT。"""
    return Compound(
        {
            "id": String(f"minecraft:{COMMAND_BLOCK_IDS[cb.mode]}"),
            "x": Int(rx + offset[0]),
            "y": Int(ry + offset[1]),
            "z": Int(rz + offset[2]),
            "Command": String(cb.command),
            "CustomName": String(cb.custom_name),
            "auto": Byte(int(not cb.needs_redstone)),
            "TrackOutput": Byte(int(cb.track_output)),
            "conditionMet": Byte(0),
        }
    )
