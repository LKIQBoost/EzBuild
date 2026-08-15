"""IBI 读取器：把 IBI 导入包拆解为 Building。

IBI = ``"IBImport "`` 头 + varint(长度)+密钥+XOR 加密的两段数据：
    段1 = setblock 指令文本（放置所有方块，含命令方块外壳）
    段2 = 命令方块 JSON（posX/posY/posZ + base64 命令）

本读取器解包两段：txt 段解析为普通方块，JSON 段解析为命令方块，
随后可用任意 Writer 输出 —— 相当于把 IBI 拆分为 txt / cmd_json。
"""

from __future__ import annotations

import base64

from ..model import Building, CommandBlock
from ..writers.ibi import decode_ibi
from .base import Reader, Source
from .txt import TxtReader


class IbiReader(Reader):
    format_name = "ibi"
    extensions = (".ibi",)
    description = "IBI 导入包（setblock 文本 + 命令方块 JSON，XOR 加密）"

    def read(self, source: Source) -> Building:
        data = self._read_bytes(source)
        txt, entries = decode_ibi(data)

        building = Building(source_format=self.format_name)

        # 段1：setblock 文本 → 普通方块（含命令方块外壳）
        txt_building = TxtReader().read(txt.encode("utf-8"))
        building.blocks = txt_building.blocks
        building.size = txt_building.size

        # 段2：命令方块 JSON → 命令方块语义数据
        for entry in entries:
            building.command_blocks.append(_entry_to_command_block(entry))

        return building


def _entry_to_command_block(entry: dict) -> CommandBlock:
    """把 IBI 内部命令方块 JSON 转换为 CommandBlock。"""
    return CommandBlock(
        x=_coord(entry.get("posX", "~0")),
        y=_coord(entry.get("posY", "~0")),
        z=_coord(entry.get("posZ", "~0")),
        mode=int(entry.get("mode", 0) or 0),
        command=_decode_base64(entry.get("CommandMessage", "")),
        tick_delay=int(entry.get("isTime", 0) or 0),
        conditional=bool(entry.get("Conditional", False)),
        needs_redstone=bool(entry.get("isRedstone", True)),
    )


def _coord(token) -> int:
    return int(str(token).lstrip("~") or 0)


def _decode_base64(token) -> str:
    try:
        return base64.b64decode(str(token)).decode("utf-8", errors="replace")
    except Exception:
        return ""
