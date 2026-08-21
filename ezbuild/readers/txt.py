"""txt 读取器：把 setblock / fill 指令文本解析为 Building。

支持的行：
    setblock ~x ~y ~z 方块名 [状态]
    fill ~x1 ~y1 ~z1 ~x2 ~y2 ~z2 方块名 [状态]
    tp ...   （忽略）

fill 按参考脚本的 ``convert_fill_to_setblock`` 展开为逐方块。
主要用于把未分区块的 txt 重新分区块（``main.py txt -i 未分区块.txt``），
也可作为 txt → cmd_json / txt 等其它输出的入口。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ..model import Block, Building
from ..utils import normalize_block_name, parse_block_states_string
from .base import Reader, Source


class TxtReader(Reader):
    format_name = "txt"
    extensions = (".txt",)
    description = "setblock/fill 指令文本（未分区块）"

    def read(self, source: Source) -> Building:
        if isinstance(source, (bytes, bytearray)):
            text = bytes(source).decode("utf-8", errors="replace")
        else:
            text = Path(source).read_text(encoding="utf-8")

        building = Building(source_format=self.format_name)
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("tp"):
                continue
            if line.startswith("setblock"):
                self._parse_setblock(line, building)
            elif line.startswith("fill"):
                self._parse_fill(line, building)
            # 其它行忽略

        self._update_size(building)
        return building

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _parse_setblock(line: str, building: Building) -> None:
        parts = line.split()
        if len(parts) < 4:
            return
        x, y, z = (_coord(p) for p in parts[1:4])
        name = normalize_block_name(parts[4])
        states = _states_from(parts[5:])
        building.blocks.append(Block(x=x, y=y, z=z, name=name, states=states))

    @staticmethod
    def _parse_fill(line: str, building: Building) -> None:
        parts = line.split()
        if len(parts) < 8:
            return
        x1, y1, z1, x2, y2, z2 = (_coord(p) for p in parts[1:7])
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        if z1 > z2:
            z1, z2 = z2, z1
        name = normalize_block_name(parts[7])
        states = _states_from(parts[8:])
        for x in range(x1, x2 + 1):
            for y in range(y1, y2 + 1):
                for z in range(z1, z2 + 1):
                    building.blocks.append(Block(x=x, y=y, z=z, name=name, states=states))

    @staticmethod
    def _update_size(building: Building) -> None:
        if not building.blocks:
            building.size = (0, 0, 0)
            return
        xs = [b.x for b in building.blocks]
        ys = [b.y for b in building.blocks]
        zs = [b.z for b in building.blocks]
        building.size = (
            max(xs) - min(xs) + 1,
            max(ys) - min(ys) + 1,
            max(zs) - min(zs) + 1,
        )


def _coord(token: str) -> int:
    """把 ``~x`` / ``~-1`` / ``~`` 解析为整数坐标。

    整数坐标直接 ``int()``（快路径），带小数点等再回退 ``int(float())``——
    setblock/fill 坐标几乎都是整数，省掉每次 float 解析开销。
    """
    if token.startswith("~"):
        token = token[1:]
    if not token:
        return 0
    try:
        return int(token)
    except ValueError:
        return int(float(token))


def _states_from(parts: list[str]) -> dict[str, Any]:
    """把 setblock/fill 行剩余部分解析为方块状态 dict。"""
    if not parts:
        return {}
    return parse_block_states_string(" ".join(parts))
