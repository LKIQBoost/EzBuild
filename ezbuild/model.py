"""中立数据模型。

所有 Reader（输入格式）都把建筑解析成 :class:`Building`，
所有 Writer（输出格式）都从 :class:`Building` 渲染内容。
这样新增一种输入格式，所有输出格式自动可用，反之亦然。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 命令方块模式（mode）常量
#   0 = 脉冲（impulse）   → command_block
#   1 = 连锁（chain）     → chain_command_block
#   2 = 重复（repeat）    → repeating_command_block
# ---------------------------------------------------------------------------
MODE_IMPULSE = 0
MODE_CHAIN = 1
MODE_REPEAT = 2

# mode → 方块 ID（不带 minecraft: 前缀）
COMMAND_BLOCK_IDS: dict[int, str] = {
    MODE_IMPULSE: "command_block",
    MODE_CHAIN: "chain_command_block",
    MODE_REPEAT: "repeating_command_block",
}

# 方块 ID → mode（反向查找）
COMMAND_BLOCK_MODES: dict[str, int] = {
    v: k for k, v in COMMAND_BLOCK_IDS.items()
}

# 命令方块方块 ID 集合
COMMAND_BLOCK_NAMES: frozenset[str] = frozenset(COMMAND_BLOCK_IDS.values())


@dataclass(slots=True)
class Block:
    """一个方块：位置 + 方块名 + 方块状态 + （可选的）方块实体 NBT。

    ``name`` 统一为小写、不带 ``minecraft:`` 前缀（如 ``stone`` / ``chest``）。
    ``states`` 为普通 Python 字典（int/bool/str 值）。
    ``nbt`` 保留原始方块实体 NBT 的普通 Python 结构（命令方块、箱子等）。
    """

    x: int
    y: int
    z: int
    name: str
    states: dict[str, Any] = field(default_factory=dict)
    nbt: dict[str, Any] | None = None


@dataclass(slots=True)
class CommandBlock:
    """命令方块：从建筑中抽取出的命令方块语义数据。

    这是多种输出格式（命令方块 JSON / IBI / DSB）共用的中间表示。
    ``mode`` 见上方常量；``needs_redstone`` 为 True 表示需要红石激活（auto=false）。
    """

    x: int
    y: int
    z: int
    mode: int = MODE_IMPULSE
    command: str = ""
    custom_name: str = ""
    tick_delay: int = 0
    conditional: bool = False
    needs_redstone: bool = True
    execute_on_first_tick: bool = False
    track_output: bool = True

    @property
    def block_id(self) -> str:
        """该命令方块对应的方块 ID（不带 minecraft: 前缀）。"""
        return COMMAND_BLOCK_IDS[self.mode]


@dataclass(slots=True)
class Building:
    """一个完整的建筑（中立表示）。

    - ``blocks``：全部方块（含命令方块），用于 setblock / fill 类输出。
    - ``command_blocks``：仅命令方块，用于命令方块 JSON / IBI / DSB 输出。
    - ``size`` / ``author`` / ``source_format``：来源信息，仅供统计与展示。
    """

    size: tuple[int, int, int] = (0, 0, 0)
    blocks: list[Block] = field(default_factory=list)
    command_blocks: list[CommandBlock] = field(default_factory=list)
    author: str = ""
    source_format: str = ""

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def command_block_count(self) -> int:
        return len(self.command_blocks)
