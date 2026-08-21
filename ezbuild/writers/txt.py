"""分区块优化 txt 输出器（操作名：txt）。

参考 ``分区块优化.py`` 的算法，直接作用于中立模型（Building.blocks）：
    1. 按 x/z 把建筑划分成 ``chunk_size``×``chunk_size`` 的区块，S 型（蛇形）排序；
    2. 每个区块内做**三维 fill 合并**：把连续的同方块区域合并成一条 ``fill`` 命令
       （超过 32367 块自动拆分成多个 fill），其余单方块保留 ``setblock``；
    3. 区块坐标相对化，并按 y 排序（fill 优先于 setblock）；
    4. 区块之间插入 ``tp ~Δx ~ ~Δz`` 导航命令，指向各区块原点。

三维 fill 合并移植 Rust 版 ``fill3d_rust``（D:\\下载\\main.rs）的算法：
按方块类型分组 → 每个 Z 平面做 2D 矩形合并（RLE）→ 相邻 Z 平面上的同矩形
缝合成立方体盒子。相比逐方块生长的旧算法（反复 ``_layer_continuous`` 整层检查），
对实心/镂空/非规则结构都快得多。

输出示例::

    tp ~0 ~ ~0
    fill ~0 ~0 ~0 ~15 ~15 ~15 stone ["pillar_axis"=0]
    setblock ~0 ~16 ~0 command_block
    tp ~16 ~ ~0
    ...
"""

from __future__ import annotations

from .._progress import make_progress, refresh_memory_postfix
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
        progress: bool = False,
    ):
        self.chunk_size = chunk_size
        self.insert_count = insert_count  # 每个 tp 后额外插入的命令数（如 testfor @s）
        self.strip_states = strip_states
        self.fill_merge = fill_merge  # False = 不进行三维 fill 合并（--nofill）
        self.progress = progress  # 是否显示转换进度条

    def render(self, building: Building) -> str:
        if not self.fill_merge:
            # --nofill：纯 setblock，无 tp、无分块，绝对坐标
            return render_plain_setblock(building, self.strip_states)
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
            blocks,
            self.chunk_size,
            self.insert_count,
            self.fill_merge,
            progress=self.progress,
        )


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def chunk_optimize(
    blocks: dict[tuple[int, int, int], tuple[str, str]],
    chunk_size: int = 16,
    insert_count: int = 0,
    fill_merge: bool = True,
    progress: bool = False,
) -> str:
    """对方块字典做分区块优化，返回指令文本。

    ``blocks``: {(x, y, z): (方块名, 状态字符串)}
    ``fill_merge``: False 时不进行三维 fill 合并（输出纯 setblock）。
    ``progress``: True 时显示转换进度条。
    """
    if not blocks:
        return ""

    min_x, max_x, min_z, max_z = _bounds(blocks)
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)

    # 按所属区块预分组：每个方块只扫描一次（O(blocks)），
    # 取代原来"每个区块全量遍历 blocks"的 O(chunks × blocks)。
    # 区块原点 sx = min_x + gx*size，故相对坐标 = (x-min_x) % size。
    groups: dict[tuple[int, int], dict[tuple[int, int, int], tuple[str, str]]] = {}
    for (x, y, z), val in blocks.items():
        gx, gz = (x - min_x) // chunk_size, (z - min_z) // chunk_size
        rel = ((x - min_x) % chunk_size, y, (z - min_z) % chunk_size)
        groups.setdefault((gx, gz), {})[rel] = val

    out: list[str] = []
    prev_x = prev_z = 0
    bar = None
    if progress:
        bar = make_progress(len(blocks), "转换方块", "个")
    try:
        for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
            gx = (sx - min_x) // chunk_size
            gz = (sz - min_z) // chunk_size
            region = groups.get((gx, gz), {})
            if not region:
                continue
            if bar is not None:
                bar.update(len(region))
                refresh_memory_postfix(bar)

            # tp 到本区块原点（相对上一区块）
            rel_x, rel_z = sx - prev_x, sz - prev_z
            out.append(f"tp ~{rel_x} ~ ~{rel_z}")
            if insert_count:
                out.extend(["testfor @s"] * insert_count)
            prev_x, prev_z = sx, sz

            fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
            out.extend(sorted(fill_cmds + setblock_cmds, key=_y_sort_key))
    finally:
        if bar is not None:
            refresh_memory_postfix(bar)
            bar.close()

    return "\n".join(out)


def _bounds(blocks) -> tuple[int, int, int, int]:
    min_x = min_z = 1 << 60
    max_x = max_z = -(1 << 60)
    for x, _, z in blocks:
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if z < min_z:
            min_z = z
        if z > max_z:
            max_z = z
    return min_x, max_x, min_z, max_z


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


# ---- 三维 fill 合并（Rust fill3d_rust 算法移植）----
def _merge_3d(coords: list[tuple[int, int, int]]) -> list[tuple[int, int, int, int, int, int]]:
    """把一组同方块坐标合并为最大 3D 盒子。

    Z 平面 RLE + 平面缝合：先每个 Z 平面做 2D 矩形合并（:func:`_merge_rectangles`），
    再把相邻 Z 平面上**矩形完全一致**的部分沿 Z 缝合成立方体盒子。
    返回 ``[(x1, y1, z1, x2, y2, z2), ...]``，盒子内部全为该方块且互不重叠、
    恰好覆盖全部坐标。参考 ``fill3d_rust`` 的 ``merge_rectangles + stitch_rectangles``。
    """
    if not coords:
        return []
    z_groups: dict[int, list[tuple[int, int]]] = {}
    for x, y, z in coords:
        z_groups.setdefault(z, []).append((x, y))
    rect_by_z = {z: _merge_rectangles(pts) for z, pts in z_groups.items()}
    return _stitch_rectangles(rect_by_z)


def _merge_rectangles(points: list[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Z 平面 2D 矩形合并（RLE）。

    按 (x, y) 排序 → 每个 x 列内的连续 y 段形成"宽 1 竖直矩形" → 按 y 区间分组，
    组内把相邻 x 列合并成水平矩形。参考 Rust ``fill3d_rust`` 的 merge_rectangles，
    但修复其单遍合并的缺陷：不同 y 区间的矩形交错时（如镂空壳的顶/底条带被
    竖边隔开），先按 y 区间分组再逐组横向合并，能合并且不会漏。输出
    ``[(x1, y1, x2, y2), ...]``（水平最大化的矩形，互不重叠）。
    """
    if not points:
        return []
    # 按 x 分组（排序后组内 y 升序）
    x_runs: dict[int, list[int]] = {}
    for x, y in sorted(points):
        x_runs.setdefault(x, []).append(y)

    # 每 x 列的连续 y 段 → 宽 1 矩形
    rects: list[tuple[int, int, int, int]] = []
    for x in x_runs:
        ys = x_runs[x]
        y_start = y_end = ys[0]
        for y in ys[1:]:
            if y == y_end + 1:
                y_end = y
            else:
                rects.append((x, y_start, x, y_end))
                y_start = y_end = y
        rects.append((x, y_start, x, y_end))

    # 按 (y1, y2) 分组，组内 x 已升序 → 相邻列合并成水平矩形
    merged: list[tuple[int, int, int, int]] = []
    by_row: dict[tuple[int, int], list[int]] = {}  # (y1, y2) -> 有序 x 列表
    for x1, y1, _, y2 in rects:
        by_row.setdefault((y1, y2), []).append(x1)
    for (y1, y2), xs in by_row.items():
        run_start = run_end = xs[0]
        for x in xs[1:]:
            if x == run_end + 1:
                run_end = x
            else:
                merged.append((run_start, y1, run_end, y2))
                run_start = run_end = x
        merged.append((run_start, y1, run_end, y2))
    return merged


def _stitch_rectangles(
    rect_by_z: dict[int, list[tuple[int, int, int, int]]],
) -> list[tuple[int, int, int, int, int, int]]:
    """把各 Z 平面的矩形沿 Z 缝合成立方体盒子。

    当前 Z 出现与上一层**同一矩形** → 盒子 z 范围延长；否则新开盒子。
    返回 ``[(x1, y1, z1, x2, y2, z2), ...]``。
    """
    boxes: list[tuple[int, int, int, int, int, int]] = []
    active: dict[tuple[int, int, int, int], tuple[int, int]] = {}  # rect -> (z_start, z_end)
    for z in sorted(rect_by_z):
        still_active: dict[tuple[int, int, int, int], tuple[int, int]] = {}
        for rect in rect_by_z[z]:
            prev = active.get(rect)
            if prev is not None and prev[1] == z - 1:
                still_active[rect] = (prev[0], z)  # 缝合成功：保持 z_start，延长 z_end
            else:
                still_active[rect] = (z, z)  # 新开启盒子
        # 关闭不再延续的 active 矩形
        for rect, (z_start, z_end) in active.items():
            if rect not in still_active:
                x1, y1, x2, y2 = rect
                boxes.append((x1, y1, z_start, x2, y2, z_end))
        active = still_active
    # 收尾：关闭最后剩余的所有 active 矩形
    for rect, (z_start, z_end) in active.items():
        x1, y1, x2, y2 = rect
        boxes.append((x1, y1, z_start, x2, y2, z_end))
    return boxes


def _split_fill(start, end, name: str, states: str) -> list[str]:
    """生成 fill 命令；超限则按最大轴二分拆分为多个（参考 Rust ``split_box``）。"""
    x1, y1, z1 = start
    x2, y2, z2 = end
    queue: list[tuple[int, int, int, int, int, int]] = [(x1, y1, z1, x2, y2, z2)]
    result: list[str] = []
    while queue:
        cx1, cy1, cz1, cx2, cy2, cz2 = queue.pop()
        width = cx2 - cx1 + 1
        height = cy2 - cy1 + 1
        depth = cz2 - cz1 + 1
        if width * height * depth <= MAX_FILL_BLOCKS:
            cmd = f"fill ~{cx1} ~{cy1} ~{cz1} ~{cx2} ~{cy2} ~{cz2} {name}"
            if states:
                cmd += f" [{states}]"
            result.append(cmd)
        else:
            if width >= height and width >= depth:
                mid = cx1 + width // 2 - 1
                queue.append((cx1, cy1, cz1, mid, cy2, cz2))
                queue.append((mid + 1, cy1, cz1, cx2, cy2, cz2))
            elif height >= width and height >= depth:
                mid = cy1 + height // 2 - 1
                queue.append((cx1, cy1, cz1, cx2, mid, cz2))
                queue.append((cx1, mid + 1, cz1, cx2, cy2, cz2))
            else:
                mid = cz1 + depth // 2 - 1
                queue.append((cx1, cy1, cz1, cx2, cy2, mid))
                queue.append((cx1, cy1, mid + 1, cx2, cy2, cz2))
    return result


def _optimize_region(blocks, fill_merge: bool = True) -> tuple[list[str], list[str]]:
    """把一个区块内的方块合并为 fill / setblock 命令。

    按方块类型分组后组内做三维合并（Rust fill3d_rust 算法）；单方块保留 setblock。
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
    by_type: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
    for (x, y, z), (name, states) in blocks.items():
        by_type.setdefault((name, states), []).append((x, y, z))

    for (name, states), coords in by_type.items():
        for x1, y1, z1, x2, y2, z2 in _merge_3d(coords):
            if x1 == x2 and y1 == y2 and z1 == z2:
                cmd = f"setblock ~{x1} ~{y1} ~{z1} {name}"
                if states:
                    cmd += f" [{states}]"
                setblock_cmds.append(cmd)
            else:
                fill_cmds.extend(_split_fill((x1, y1, z1), (x2, y2, z2), name, states))

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
