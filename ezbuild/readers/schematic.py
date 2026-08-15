"""schematic 读取器：经典 Java 版 .schematic 结构文件。

格式：大端序 NBT（通常 gzip 压缩），包含 ``Blocks``/``Data`` 字节数组与
``Width``/``Height``/``Length`` 尺寸。方块 ID 与 data 值通过映射表
（``ezbuild/data/schematic_blocks.json``，参考 ``schematic转换源码.py``）
换算为现代方块名与状态。命令方块数据从 ``TileEntities`` 中提取。
"""

from __future__ import annotations

import gzip
import io
from typing import Any

import nbtlib

from ..model import (
    MODE_IMPULSE,
    Block,
    Building,
    CommandBlock,
    COMMAND_BLOCK_MODES,
)
from ..utils import (
    load_schematic_table,
    normalize_block_name,
    parse_block_states_string,
    tag_to_python,
)
from .base import Reader, Source

# Java 版命令方块 TileEntity ID（经典 schematic 用 "Control"）
_COMMAND_TILE_IDS = {"control", "minecraft:command_block", "command_block"}


class SchematicReader(Reader):
    format_name = "schematic"
    extensions = (".schematic",)
    description = "Minecraft Java 版经典 .schematic 结构文件"

    def read(self, source: Source) -> Building:
        data = self._read_bytes(source)
        if data[:2] == b"\x1f\x8b":  # gzip 魔数
            data = gzip.decompress(data)
        schematic = nbtlib.File.parse(io.BytesIO(data), byteorder="big")

        X, Y, Z = self._size(schematic)
        table = load_schematic_table()

        building = Building(size=(X, Y, Z), source_format=self.format_name)
        self._parse_blocks(schematic, X, Y, Z, table, building)
        self._parse_tile_entities(schematic, building)
        return building

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _size(schematic) -> tuple[int, int, int]:
        return (
            int(schematic["Width"]),
            int(schematic["Height"]),
            int(schematic["Length"]),
        )

    @staticmethod
    def _parse_blocks(schematic, X: int, Y: int, Z: int, table, building) -> None:
        """解析 Blocks/Data 字节数组 → 普通方块。"""
        blocks_arr = schematic["Blocks"]
        data_arr = schematic["Data"]
        total = X * Y * Z
        if total <= 0:
            return

        for i in range(min(total, len(blocks_arr))):
            block_id = int(blocks_arr[i]) & 0xFF
            if block_id == 0:  # air
                continue
            data_val = int(data_arr[i]) & 0xFF if i < len(data_arr) else 0

            spec = table.get(str((block_id << 4) | data_val))
            if spec is None:
                continue  # 未映射的方块，跳过

            name, states = _parse_spec(spec)
            x = i % X
            z = (i // X) % Z
            y = i // (X * Z)
            building.blocks.append(Block(x=x, y=y, z=z, name=name, states=states))

    @staticmethod
    def _parse_tile_entities(schematic, building) -> None:
        """从 TileEntities 提取命令方块数据。"""
        te_list = schematic.get("TileEntities")
        if te_list is None:
            return
        for te_tag in te_list:
            te = tag_to_python(te_tag)
            te_id = str(te.get("id", "") or "").lower()
            if te_id not in _COMMAND_TILE_IDS:
                continue
            x, y, z = int(te["x"]), int(te["y"]), int(te["z"])
            mode = _mode_at(building.blocks, x, y, z)
            auto = bool(te.get("auto", False))

            cb = CommandBlock(
                x=x,
                y=y,
                z=z,
                mode=mode,
                command=str(te.get("Command", "") or ""),
                custom_name=str(te.get("CustomName", "") or ""),
                tick_delay=int(te.get("TickDelay", 0) or 0),
                conditional=bool(te.get("Conditional", False)),
                needs_redstone=not auto,
                execute_on_first_tick=bool(te.get("ExecuteOnFirstTick", False)),
                track_output=bool(te.get("TrackOutput", True)),
            )
            building.command_blocks.append(cb)

            # 把命令方块数据挂到对应 Block 的 nbt 上
            for blk in building.blocks:
                if (blk.x, blk.y, blk.z) == (x, y, z):
                    blk.nbt = te
                    break


def _parse_spec(spec: str) -> tuple[str, dict[str, Any]]:
    """把映射表值解析为 (方块名, 状态 dict)。

    如 ``minecraft:stone ["stone_type"="stone"]`` →
    ``("stone", {"stone_type": "stone"})``。
    """
    parts = spec.split(" ", 1)
    name = normalize_block_name(parts[0])
    states = parse_block_states_string(parts[1]) if len(parts) > 1 else {}
    return name, states


def _mode_at(blocks: list[Block], x: int, y: int, z: int) -> int:
    for blk in blocks:
        if (blk.x, blk.y, blk.z) == (x, y, z):
            return COMMAND_BLOCK_MODES.get(blk.name, MODE_IMPULSE)
    return MODE_IMPULSE
