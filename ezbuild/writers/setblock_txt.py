"""setblock 指令文本输出器。

每行一条 ``setblock ~x ~y ~z 方块名 [状态]`` 指令，
方块状态为 SNBT 风格（``"key"=值``，逗号分隔）。
"""

from __future__ import annotations

from typing import Any

from ..model import Block, Building
from ..utils import format_block_states
from .base import Writer


class SetblockTxtWriter(Writer):
    format_name = "setblock_txt"
    extensions = (".txt",)
    description = "setblock 指令文本（每行一条）"

    def __init__(self, strip_states: frozenset[str] | None = None):
        # None = 默认规则（省略 *_bit 开关状态）；frozenset() = 保留全部
        self.strip_states = strip_states

    def render(self, building: Building) -> str:
        lines = []
        for block in building.blocks:
            lines.append(self._line(block))
        return "\n".join(lines)

    def _line(self, block: Block) -> str:
        coord = f"~{block.x} ~{block.y} ~{block.z}"
        states = block.states
        if isinstance(states, str):
            # 原始状态字符串（如 "{}"），去掉花括号直接用
            states_str = states.strip().strip("{}").strip()
            if states_str:
                return f"setblock {coord} {block.name} [{states_str}]"
            return f"setblock {coord} {block.name}"
        if states:
            return f"setblock {coord} {block.name} [{format_block_states(states, self.strip_states)}]"
        return f"setblock {coord} {block.name}"
