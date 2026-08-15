"""MCStructure 读取器：把 Minecraft 基岩版 .mcstructure 结构文件解析为 Building。

这里输出为中立模型，供所有 Writer 使用。
"""

from __future__ import annotations

import io
from typing import Any

import nbtlib

from ..model import Block, Building, CommandBlock, COMMAND_BLOCK_MODES
from ..utils import tag_to_python
from .base import Reader, Source


def get_str(compound: Any, key: str, default: str = "") -> str:
    val = compound.get(key)
    if val is None:
        return default
    return str(tag_to_python(val) or default)


def get_int(compound: Any, key: str, default: int = 0) -> int:
    val = compound.get(key)
    if val is None:
        return default
    try:
        return int(tag_to_python(val) or default)
    except (TypeError, ValueError):
        return default


def get_bool(compound: Any, key: str, default: bool = False) -> bool:
    val = compound.get(key)
    if val is None:
        return default
    return bool(tag_to_python(val))


class MCStructureReader(Reader):
    format_name = "mcstructure"
    extensions = (".mcstructure",)
    description = "Minecraft 基岩版 .mcstructure 结构文件"

    def read(self, source: Source) -> Building:
        if isinstance(source, (bytes, bytearray)):
            structure = nbtlib.File.parse(io.BytesIO(bytes(source)), byteorder="little")
        else:
            structure = nbtlib.load(source, byteorder="little")

        size_tag = structure["size"]
        size = tuple(int(v) for v in tag_to_python(size_tag))  # (X, Y, Z)
        X, Y, Z = size

        palette = structure["structure"]["palette"]["default"]
        block_palette = palette["block_palette"]
        block_position_data = palette.get("block_position_data", nbtlib.tag.Compound({}))
        block_indices = structure["structure"]["block_indices"][0]

        building = Building(size=size, source_format=self.format_name)
        indices = tag_to_python(block_indices)

        for x in range(X):
            for y in range(Y):
                for z in range(Z):
                    idx = z + y * Z + x * Y * Z  # mcstructure 索引公式
                    if idx >= len(indices):
                        continue
                    palette_idx = indices[idx]
                    if palette_idx == -1:
                        continue  # 空气
                    block_tag = block_palette[palette_idx]
                    block_name = get_str(block_tag, "name").replace("minecraft:", "")
                    if not block_name or block_name == "air":
                        continue

                    states = tag_to_python(block_tag.get("states", nbtlib.tag.Compound({})))
                    entity_tag = block_position_data.get(str(idx), nbtlib.tag.Compound({})).get(
                        "block_entity_data"
                    )
                    entity = tag_to_python(entity_tag) if entity_tag is not None else None

                    building.blocks.append(
                        Block(x=x, y=y, z=z, name=block_name, states=states, nbt=entity)
                    )

                    # 命令方块
                    if block_name in COMMAND_BLOCK_MODES:
                        cb = self._make_command_block(x, y, z, block_name, entity)
                        if cb is not None:
                            building.command_blocks.append(cb)

        return building

    @staticmethod
    def _make_command_block(
        x: int, y: int, z: int, block_name: str, entity: dict | None
    ) -> CommandBlock | None:
        """从方块实体 NBT 生成 CommandBlock；缺数据时返回 None。"""
        if not entity:
            return None
        command = str(entity.get("Command", "") or "")
        if not command:
            # 命令为空仍保留（lemon.json 第一个命令方块 Command 就是空）
            pass
        auto = bool(entity.get("auto", False))
        return CommandBlock(
            x=x,
            y=y,
            z=z,
            mode=COMMAND_BLOCK_MODES[block_name],
            command=command,
            custom_name=str(entity.get("CustomName", "") or ""),
            tick_delay=int(entity.get("TickDelay", 0) or 0),
            conditional=bool(entity.get("conditionalMode", entity.get("Conditional", False))),
            needs_redstone=not auto,
            execute_on_first_tick=bool(entity.get("ExecuteOnFirstTick", False)),
            track_output=bool(entity.get("TrackOutput", True)),
        )
