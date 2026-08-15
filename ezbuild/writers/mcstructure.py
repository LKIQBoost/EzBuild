"""mcstructure 输出器：把 Building 渲染为 Minecraft 基岩版 .mcstructure 结构文件。

结构文件为**未压缩**的 little-endian NBT（头字节 ``0a 00 00 03``，同官方模板），
根结构：:

    {
        "format_version": Int(1),
        "size": [X, Y, Z],
        "structure_world_origin": [ox, oy, oz],
        "structure": {
            "palette": {"default": {"block_palette": [...], "block_position_data": {...}}},
            "block_indices": [[...], [...]]
        }
    }

与读取器（``readers/mcstructure.py``）互逆，索引公式 ``idx = z + y*Z + x*Y*Z``。
"""

from __future__ import annotations

import io
from typing import Any

import nbtlib
from nbtlib.tag import Byte, Compound, Int, IntArray, List, Short, String

from ..model import Building, CommandBlock, COMMAND_BLOCK_MODES
from .base import Writer

# 与官方模板一致的方块版本号
BLOCK_VERSION = 18090528


def _states_to_nbt(states: Any) -> Compound:
    """方块状态 dict -> nbtlib Compound（int/bool/str 值）。"""
    out = Compound()
    if not isinstance(states, dict):
        return out
    for key, val in states.items():
        if isinstance(val, bool):
            out[key] = Byte(int(val))
        elif isinstance(val, int):
            out[key] = Int(val)
        else:
            out[key] = String(str(val))
    return out


class McStructureWriter(Writer):
    format_name = "mcstructure"
    extensions = (".mcstructure",)
    description = "Minecraft 基岩版 .mcstructure 结构文件"

    def render(self, building: Building) -> bytes:
        cells = self._collect_cells(building)
        if not cells:
            X, Y, Z = 0, 0, 0
            size = IntArray([0, 0, 0])
            origin = IntArray([0, 0, 0])
        else:
            xs = [c[0] for c in cells]
            ys = [c[1] for c in cells]
            zs = [c[2] for c in cells]
            min_x, min_y, min_z = min(xs), min(ys), min(zs)
            max_x, max_y, max_z = max(xs), max(ys), max(zs)
            X, Y, Z = max_x - min_x + 1, max_y - min_y + 1, max_z - min_z + 1
            size = IntArray([X, Y, Z])
            origin = IntArray([min_x, min_y, min_z])

        n_cells = X * Y * Z
        indices0 = [-1] * n_cells
        palette_map: dict[tuple, int] = {}
        palette: list[dict] = []
        position_data: dict[str, Any] = {}
        cmd_by_pos = {(c.x, c.y, c.z): c for c in building.command_blocks}

        for (x, y, z), blk in cells.items():
            idx = z + y * Z + x * Y * Z
            if not (0 <= idx < n_cells):
                continue

            key = (blk.name, tuple(sorted(blk.states.items())) if isinstance(blk.states, dict) else ())
            if key not in palette_map:
                palette_map[key] = len(palette)
                states = blk.states if isinstance(blk.states, dict) else {}
                val = states.get("facing_direction", 0) if blk.name in COMMAND_BLOCK_MODES else 0
                palette.append(
                    {
                        "name": f"minecraft:{blk.name}",
                        "states": states,
                        "val": int(val),
                    }
                )
            indices0[idx] = palette_map[key]

            if blk.name in COMMAND_BLOCK_MODES:
                cb = cmd_by_pos.get((x, y, z))
                if cb is not None:
                    position_data[str(idx)] = Compound(
                        {"block_entity_data": self._entity(cb)}
                    )

        structure = Compound(
            {
                "format_version": Int(1),
                "size": size,
                "structure_world_origin": origin,
                "structure": Compound(
                    {
                        "palette": Compound(
                            {
                                "default": Compound(
                                    {
                                        "block_palette": List(self._palette_entries(palette)),
                                        "block_position_data": Compound(position_data),
                                    }
                                )
                            }
                        ),
                        "block_indices": List(
                            [IntArray(indices0), IntArray([-1] * n_cells)]
                        ),
                    }
                ),
            }
        )

        buf = io.BytesIO()
        nbtlib.File(structure).write(buf, byteorder="little")
        return buf.getvalue()

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _collect_cells(building: Building) -> dict:
        """收集 (x, y, z) -> Block；缺失的命令方块补一个块条目。"""
        cells = {(blk.x, blk.y, blk.z): blk for blk in building.blocks}
        for cb in building.command_blocks:
            pos = (cb.x, cb.y, cb.z)
            if pos not in cells:
                from ..model import Block

                cells[pos] = Block(
                    x=cb.x, y=cb.y, z=cb.z,
                    name=COMMAND_BLOCK_MODES[cb.mode],
                    states={"facing_direction": 3},
                )
        return cells

    @staticmethod
    def _palette_entries(palette: list) -> list:
        return [
            Compound(
                {
                    "name": String(entry["name"]),
                    "states": _states_to_nbt(entry["states"]),
                    "val": Short(entry["val"]),
                    "version": Int(BLOCK_VERSION),
                }
            )
            for entry in palette
        ]

    @staticmethod
    def _entity(cb: CommandBlock) -> Compound:
        return Compound(
            {
                "id": String("CommandBlock"),
                "x": Int(cb.x),
                "y": Int(cb.y),
                "z": Int(cb.z),
                "Command": String(cb.command),
                "CustomName": String(cb.custom_name),
                "auto": Byte(int(not cb.needs_redstone)),
                "conditionalMode": Byte(int(cb.conditional)),
                "TickDelay": Int(cb.tick_delay),
                "TrackOutput": Byte(int(cb.track_output)),
            }
        )
