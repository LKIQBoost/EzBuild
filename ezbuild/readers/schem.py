"""schem 读取器：Java 版 Sponge .schem 结构文件。

Sponge 格式（gzip 大端 NBT）：``Palette``(方块状态串→索引) + ``BlockData``(索引
字节数组) + ``Offset``([min_x,min_y,min_z])，索引公式 ``x=i%W, y=i//(W*L)%H,
z=i//W%L``。方块状态串形如 ``minecraft:stone[waterlogged=false]``，读取时
把 Java 状态转为模型（Bedrock 风格）的关键字段（facing→facing_direction、
conditional→conditional_bit）。命令方块从 ``BlockEntities`` 提取。
"""

from __future__ import annotations

import gzip
import io

import nbtlib
import numpy as np

from ..model import Block, Building, CommandBlock, COMMAND_BLOCK_MODES, MODE_IMPULSE
from ..utils import (
    java_to_bedrock_states,
    normalize_block_name,
    parse_block_states_string,
    tag_to_python,
)
from .base import Reader, Source


def _as_uint8(block_data) -> np.ndarray:
    """把 BlockData 转为普通无符号字节 ndarray（0-255）。

    nbtlib 的 ByteArray 是有符号 int8 子类（索引 >127 变负号导致丢方块，
    且覆盖 ``__getitem__`` 不支持多维切片）；``np.asarray(..., uint8)``
    转为普通 ndarray 并按位重解释索引。
    """
    return np.asarray(block_data, dtype=np.uint8)


def _parse_blockstate(s: str) -> tuple[str, dict]:
    """把 Sponge 方块状态串解析为 (方块名, 模型状态)。

    ``minecraft:chain_command_block[facing=north,conditional=true]`` →
    (``chain_command_block``, ``{'facing_direction': 2, 'conditional_bit': 1}``)。
    """
    if "[" in s:
        name, _, states_str = s.partition("[")
        states_str = states_str.rstrip("]")
    else:
        name, states_str = s, ""
    name = normalize_block_name(name)
    states = parse_block_states_string(states_str) if states_str else {}
    return name, java_to_bedrock_states(states)


class SchemReader(Reader):
    format_name = "schem"
    extensions = (".schem",)
    description = "Minecraft Java 版 Sponge .schem 结构文件"

    def read(self, source: Source) -> Building:
        data = self._read_bytes(source)
        if data[:2] == b"\x1f\x8b":  # gzip 魔数
            data = gzip.decompress(data)
        schem = nbtlib.File.parse(io.BytesIO(data), byteorder="big")

        W, H, L = int(schem["Width"]), int(schem["Height"]), int(schem["Length"])
        offset = tag_to_python(schem.get("Offset", [0, 0, 0]))

        # 预计算调色板：索引 -> (方块名, 状态)。只解析唯一方块状态（通常几百个），
        # 每个非空气格共用同一个 (name, states) 对象，避免重复解析与内存膨胀。
        parsed: dict[int, tuple[str, dict]] = {}
        for blockstate, idx in schem["Palette"].items():
            parsed[int(idx)] = _parse_blockstate(blockstate)
        air_indices = {i for i, (n, _) in parsed.items() if not n or n == "air"}

        building = Building(size=(W, H, L), source_format=self.format_name)
        total = W * H * L
        bd = _as_uint8(schem["BlockData"])
        if bd.size <= 0 or total <= 0:
            return building

        # numpy 向量化找出非空气格的索引，避免 Python 逐格循环 1.8 亿次
        if len(air_indices) == 1:
            mask = bd != next(iter(air_indices))
        elif air_indices:
            mask = ~np.isin(bd, list(air_indices))
        else:
            mask = np.ones(bd.size, dtype=bool)
        non_air = np.nonzero(mask)[0]
        del mask

        # 有 BlockEntities 才构建位置字典（大型建筑可省几 GB）
        blocks_by_pos: dict[tuple[int, int, int], Block] | None = (
            {} if schem.get("BlockEntities") is not None else None
        )

        for i in non_air:
            if i >= total:
                break
            item = parsed.get(int(bd[i]))
            if item is None:
                continue
            name, states = item
            # 相对坐标 + Offset = 绝对世界坐标（与 buildtool 一致）
            x = (i % W) + offset[0]
            z = ((i // W) % L) + offset[2]
            y = ((i // (W * L)) % H) + offset[1]
            blk = Block(x=x, y=y, z=z, name=name, states=states)
            building.blocks.append(blk)
            if blocks_by_pos is not None:
                blocks_by_pos[(x, y, z)] = blk

        if blocks_by_pos is not None:
            self._parse_block_entities(schem, building, blocks_by_pos)
        return building

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _parse_block_entities(
        schem,
        building: Building,
        blocks_by_pos: dict[tuple[int, int, int], Block],
    ) -> None:
        """从 BlockEntities 提取命令方块（其余实体 NBT 挂到对应 Block 保留）。"""
        be_list = schem.get("BlockEntities")
        if be_list is None:
            return
        for be_tag in be_list:
            be = tag_to_python(be_tag)
            # 真实 Sponge 文件用 Pos(IntArray)；兼容 x/y/z 与 Id/id 两种写法
            pos = be.get("Pos")
            if pos is not None:
                x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
            elif "x" in be and "y" in be and "z" in be:
                x, y, z = int(be["x"]), int(be["y"]), int(be["z"])
            else:
                continue
            blk = blocks_by_pos.get((x, y, z))
            if blk is None:
                continue
            blk.nbt = be
            if blk.name in COMMAND_BLOCK_MODES:
                building.command_blocks.append(
                    _make_command_block(blk, be)
                )


def _make_command_block(blk: Block, be: dict) -> CommandBlock:
    """Java 命令方块 BlockEntity → CommandBlock（Java 无 TickDelay，取 0）。"""
    auto = bool(be.get("auto", False))
    return CommandBlock(
        x=blk.x,
        y=blk.y,
        z=blk.z,
        mode=COMMAND_BLOCK_MODES.get(blk.name, MODE_IMPULSE),
        command=str(be.get("Command", "") or ""),
        custom_name=str(be.get("CustomName", "") or ""),
        tick_delay=0,
        conditional=bool(blk.states.get("conditional_bit", False)),
        needs_redstone=not auto,
        execute_on_first_tick=bool(be.get("ExecuteOnFirstTick", False)),
        track_output=bool(be.get("TrackOutput", True)),
    )
