"""分区块优化 txt 输出器（操作名：txt）。

参考 ``分区块优化.py`` 的算法，直接作用于中立模型（Building.blocks）：
    1. 按 x/z 把建筑划分成 ``chunk_size``×``chunk_size`` 的区块，S 型（蛇形）排序；
    2. 每个区块内做**三维 fill 合并**：把连续的同方块区域合并成一条 ``fill`` 命令
       （超过 32367 块自动拆分成多个 fill），其余单方块保留 ``setblock``；
    3. 区块坐标相对化，并按 y 排序（fill 优先于 setblock）；
    4. 区块之间插入 ``tp ~Δx ~ ~Δz`` 导航命令，指向各区块原点。

输出示例::

    tp ~0 ~ ~0
    fill ~0 ~0 ~0 ~15 ~15 ~15 stone ["pillar_axis"=0]
    setblock ~0 ~16 ~0 command_block
    tp ~16 ~ ~0
    ...
"""

from __future__ import annotations

from typing import Any

from typing import Any

from ..model import Block, Building
from ..utils import format_block_states
from .base import Writer

# 单条 fill 命令允许的最大方块数（命令长度上限）
MAX_FILL_BLOCKS = 32367


def render_plain_setblock(
    building: Building, strip_states: frozenset[str] | None = None
) -> str:
    """渲染纯 setblock 文本（不分区块、不合并 fill）。

    供 IBI 打包等需要朴素 setblock 列表的场景使用。
    ``strip_states``: None = 默认规则（省略 *_bit 开关状态）。
    """
    lines = []
    for block in building.blocks:
        coord = f"~{block.x} ~{block.y} ~{block.z}"
        states = block.states
        if isinstance(states, str):
            states_str = states.strip().strip("{}").strip()
            if states_str:
                lines.append(f"setblock {coord} {block.name} [{states_str}]")
            else:
                lines.append(f"setblock {coord} {block.name}")
        elif states:
            lines.append(
                f"setblock {coord} {block.name} [{format_block_states(states, strip_states)}]"
            )
        else:
            lines.append(f"setblock {coord} {block.name}")
    return "\n".join(lines)


class TxtWriter(Writer):
    format_name = "txt"
    extensions = (".txt",)
    description = "分区块优化 txt（区块化 + tp 导航，可选 fill 三维合并）"

    def __init__(
        self,
        chunk_size: int = 16,
        insert_count: int = 0,
        strip_states: frozenset[str] | None = None,
        fill_merge: bool = True,
    ):
        self.chunk_size = chunk_size
        self.insert_count = insert_count  # 每个 tp 后额外插入的命令数（如 testfor @s）
        self.strip_states = strip_states
        self.fill_merge = fill_merge  # False = 不进行三维 fill 合并（--nofill）

    def render(self, building: Building) -> str:
        blocks: dict[tuple[int, int, int], tuple[str, str]] = {}
        for blk in building.blocks:
            states = blk.states
            state_str = (
                states.strip().strip("{}").strip()
                if isinstance(states, str)
                else format_block_states(states, self.strip_states)
            )
            blocks[(blk.x, blk.y, blk.z)] = (blk.name, state_str)
        return chunk_optimize(
            blocks, self.chunk_size, self.insert_count, self.fill_merge
        )


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def chunk_optimize(
    blocks: dict[tuple[int, int, int], tuple[str, str]],
    chunk_size: int = 16,
    insert_count: int = 0,
    fill_merge: bool = True,
) -> str:
    """对方块字典做分区块优化，返回指令文本。

    ``blocks``: {(x, y, z): (方块名, 状态字符串)}
    ``fill_merge``: False 时不进行三维 fill 合并（输出纯 setblock）。
    """
    if not blocks:
        return ""

    min_x, max_x, min_z, max_z = _bounds(blocks)
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)

    out: list[str] = []
    prev_x = prev_z = 0
    for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
        region: dict[tuple[int, int, int], tuple[str, str]] = {}
        for (x, y, z), val in blocks.items():
            if sx <= x <= ex and sz <= z <= ez:
                region[(x - sx, y, z - sz)] = val
        if not region:
            continue

        # tp 到本区块原点（相对上一区块）
        rel_x, rel_z = sx - prev_x, sz - prev_z
        out.append(f"tp ~{rel_x} ~ ~{rel_z}")
        if insert_count:
            out.extend(["testfor @s"] * insert_count)
        prev_x, prev_z = sx, sz

        fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
        out.extend(sorted(fill_cmds + setblock_cmds, key=_y_sort_key))

    return "\n".join(out)


def _bounds(blocks) -> tuple[int, int, int, int]:
    xs = [p[0] for p in blocks]
    zs = [p[2] for p in blocks]
    return min(xs), max(xs), min(zs), max(zs)


def _divide_chunks(
    min_x: int, max_x: int, min_z: int, max_z: int, size: int
) -> list[tuple[int, int, int, int]]:
    chunks = []
    for sx in range(min_x, max_x + 1, size):
        ex = min(sx + size - 1, max_x)
        for sz in range(min_z, max_z + 1, size):
            ez = min(sz + size - 1, max_z)
            chunks.append((sx, ex, sz, ez))
    return chunks


def _s_sort(chunks: list[tuple[int, int, int, int]], size: int) -> list[tuple[int, int, int, int]]:
    """按 x 分组，奇数 x 组 z 反向，形成 S 型遍历顺序。"""
    groups: dict[int, list[tuple[int, int, int, int]]] = {}
    for c in chunks:
        groups.setdefault(c[0] // size, []).append(c)
    ordered: list[tuple[int, int, int, int]] = []
    for xg in sorted(groups):
        if xg % 2 == 0:
            ordered.extend(sorted(groups[xg], key=lambda c: c[2]))
        else:
            ordered.extend(sorted(groups[xg], key=lambda c: -c[2]))
    return ordered


# ---- 三维 fill 合并 ----
def _is_continuous(
    blocks, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, name: str, states: str
) -> bool:
    for x in range(x1, x2 + 1):
        for y in range(y1, y2 + 1):
            for z in range(z1, z2 + 1):
                if blocks.get((x, y, z)) != (name, states):
                    return False
    return True


def _grow_region(blocks, start, name: str, states: str):
    """从起点向 x/y/z 三个方向扩展，找到最大连续同方块区域。"""
    x, y, z = start
    xe, ye, ze = x, y, z
    while _is_continuous(blocks, x, y, z, xe + 1, ye, ze, name, states):
        xe += 1
    while _is_continuous(blocks, x, y, z, xe, ye + 1, ze, name, states):
        ye += 1
    while _is_continuous(blocks, x, y, z, xe, ye, ze + 1, name, states):
        ze += 1
    return (x, y, z), (xe, ye, ze)


def _split_fill(start, end, name: str, states: str) -> list[str]:
    """生成 fill 命令；超限则按立方体拆分为多条。"""
    x1, y1, z1 = start
    x2, y2, z2 = end
    count = (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
    cmd = f"fill ~{x1} ~{y1} ~{z1} ~{x2} ~{y2} ~{z2} {name}"
    if states:
        cmd += f" [{states}]"

    if count <= MAX_FILL_BLOCKS:
        return [cmd]

    step = max(1, int(round(count ** (1 / 3))))
    cmds = []
    for i in range(x1, x2 + 1, step):
        for j in range(y1, y2 + 1, step):
            for k in range(z1, z2 + 1, step):
                cmds.extend(
                    _split_fill(
                        (i, j, k),
                        (min(i + step - 1, x2), min(j + step - 1, y2), min(k + step - 1, z2)),
                        name,
                        states,
                    )
                )
    return cmds


def _optimize_region(blocks, fill_merge: bool = True) -> tuple[list[str], list[str]]:
    """把一个区块内的方块合并为 fill / setblock 命令。

    ``fill_merge`` 为 False 时不做三维合并，全部输出 setblock。
    """
    if not fill_merge:
        cmds = []
        for (x, y, z), (name, states) in blocks.items():
            cmd = f"setblock ~{x} ~{y} ~{z} {name}"
            if states:
                cmd += f" [{states}]"
            cmds.append(cmd)
        return [], cmds

    fill_cmds: list[str] = []
    setblock_cmds: list[str] = []
    visited: set[tuple[int, int, int]] = set()

    for start, (name, states) in blocks.items():
        if start in visited:
            continue
        (x, y, z), (xe, ye, ze) = _grow_region(blocks, start, name, states)
        region_size = (xe - x + 1) * (ye - y + 1) * (ze - z + 1)
        if region_size > 1:
            fill_cmds.extend(_split_fill((x, y, z), (xe, ye, ze), name, states))
        else:
            cmd = f"setblock ~{x} ~{y} ~{z} {name}"
            if states:
                cmd += f" [{states}]"
            setblock_cmds.append(cmd)
        for vx in range(x, xe + 1):
            for vy in range(y, ye + 1):
                for vz in range(z, ze + 1):
                    visited.add((vx, vy, vz))

    return fill_cmds, setblock_cmds


def _y_sort_key(line: str) -> tuple[int, int]:
    """按 y 排序，fill 优先于 setblock（同一层先铺满再放单方块）。"""
    parts = line.split()
    if parts and parts[0] in ("fill", "setblock") and len(parts) >= 3:
        try:
            y = int(float(parts[2][1:]))
        except ValueError:
            y = 0
        return (y, 0 if parts[0] == "fill" else 1)
    return (0, 2)
