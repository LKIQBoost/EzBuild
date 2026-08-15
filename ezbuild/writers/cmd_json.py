"""命令方块 JSON 输出器（lemon 格式）。

输出类似 ``[zx-093]lemon.json`` 的格式：一个命令方块对象数组，每个对象：:

    {
        "posx": "~0",
        "posy": "~0",
        "posz": "~0",
        "BlockMode": "chain_command_block",
        "name": "",
        "Command": "execute as @a at @s run ...",
        "TickDelay": 0,
        "IsRedStoneMode": 0,
        "IsConditional": 0
    }

坐标使用相对坐标（``~N``，相对建筑原点），
``IsRedStoneMode``/``IsConditional`` 以 0/1 整数表示。
"""

from __future__ import annotations

import json

from ..model import Building, CommandBlock, COMMAND_BLOCK_IDS
from .base import Writer


class CommandBlockJsonWriter(Writer):
    format_name = "cmd_json"
    extensions = (".json",)
    description = "命令方块 JSON（lemon 格式）：posx/posy/posz + BlockMode + Command"

    def render(self, building: Building) -> str:
        data = [self._entry(cb) for cb in building.command_blocks]
        # indent=4 与 lemon.json 一致；ensure_ascii=False 保留中文等非 ASCII 字符
        return json.dumps(data, ensure_ascii=False, indent=4)

    @staticmethod
    def _entry(cb: CommandBlock) -> dict:
        """把一个 CommandBlock 转为 lemon.json 的单条记录。"""
        return {
            "posx": f"~{cb.x}",
            "posy": f"~{cb.y}",
            "posz": f"~{cb.z}",
            "BlockMode": COMMAND_BLOCK_IDS[cb.mode],
            "name": cb.custom_name,
            "Command": cb.command,
            "TickDelay": cb.tick_delay,
            "IsRedStoneMode": 1 if cb.needs_redstone else 0,
            "IsConditional": 1 if cb.conditional else 0,
        }
