"""音乐 → 建筑：把 Song 转成命令方块"音乐机"（Building）。

参照 MIDI-MCSTRUCTURE_NEXT 的做法：一串命令方块沿蛇形排列，
每块的 ``facing_direction`` 指向下一块；第 0 块为**脉冲**命令方块
（需红石触发启动），其余为**连锁**命令方块（auto，被上一块激活）；
每块写一条 ``/execute ... playsound``，``TickDelay`` 为距上一音符的
游戏刻数，总时间 = 各块 TickDelay 累加。

音高约定与 NBS 一致：``pitch = 2 ** ((note - 66) / 12)``
（MIDI 66 = F#4 → pitch 1.0 = 音阶块基准，即 NBS key 45 对应的音）。
"""

from __future__ import annotations

import math

from .model import (
    COMMAND_BLOCK_IDS,
    Block,
    Building,
    CommandBlock,
    MODE_CHAIN,
    MODE_IMPULSE,
)
from .song import InstrumentPart, Song

# 游戏刻：1 秒 = 20 刻
TICKS_PER_SECOND = 20

# MIDI 音符 -> playsound pitch 的基准（note 66 = F#4 -> pitch 1.0）
PITCH_BASE = 66
MIN_PITCH = 0.5
MAX_PITCH = 2.0

# 非音阶块音效（random.fizz 镲片等）不钳到 2.0 —— 镲片靠 +19/+31/+38
# 半音偏移出的高 pitch 区分（参照参考项目），这里只做极端值防护
_MIN_NONNOTE_PITCH = 0.01
_MAX_NONNOTE_PITCH = 64.0

# Bedrock facing_direction: 0 下 1 上 2 北(-z) 3 南(+z) 4 西(-x) 5 东(+x)
_FACING_UP = 1
_FACING_DOWN = 0
_FACING_SOUTH = 3
_FACING_NORTH = 2
_FACING_EAST = 5
_FACING_WEST = 4


def _fold_semis(semis: float) -> float:
    """把半音折进 [-12, +12]（pitch 0.5-2.0，音阶块可播放范围）。

    与 NBS 写出的 fold_range 一致：超范围音符按八度搬移，**保住相对音准**
    —— 钳制会把极端音符全压成同一音高（旋律被压平），折叠则保留音程关系。
    """
    while semis < -12.0:
        semis += 12.0
    while semis > 12.0:
        semis -= 12.0
    return semis


def _build_playsound(note, part: InstrumentPart, edition: str, fold: bool = True) -> str:
    """生成一条 /playsound 命令（不含前导斜杠）。

    基岩版：``execute as @a at @s run playsound <sound> @s <pos> <vol> <pitch> <vol>``
    Java 版：中间多一个 ``record`` 音源参数。

    音色/音量/音高来自 ``part``（乐器部件）：音效名、响度补偿（乘力度）、
    半音偏移（加到 pitch 公式）。旋律音高 = ``2 ** ((note - 66 + 偏移 + 弯音)/12)``，
    并叠加 ``note.pitch_bend``（MIDI 弯音 / NBS 微调）；打击乐（channel 9）
    以基准音 66 起算，即 ``2 ** ((偏移 + 弯音)/12)``。
    ``fold=True`` 时 ``note.*`` 音效把半音八度折叠进可播放范围 [0.5, 2.0]
    保证音准；``fold=False`` 保留原始八度（极端音符被钳到两端）。
    非 ``note.*`` 音效（fizz 镲片等）不折叠也不钳到 2.0，保留偏移区分度。
    """
    if note.channel == 9:
        semis = part.pitch_offset + note.pitch_bend
    else:
        semis = note.note - PITCH_BASE + part.pitch_offset + note.pitch_bend
    if part.sound.startswith("note."):
        if fold:
            semis = _fold_semis(semis)
            pitch = 2.0 ** (semis / 12.0)
        else:
            pitch = max(MIN_PITCH, min(MAX_PITCH, 2.0 ** (semis / 12.0)))
    else:
        pitch = max(_MIN_NONNOTE_PITCH, min(_MAX_NONNOTE_PITCH, 2.0 ** (semis / 12.0)))
    vol = max(0.0, min(1.0, note.velocity * part.volume))

    if note.panning:
        left = -note.panning if note.panning < 0 else 0.0
        right = note.panning if note.panning > 0 else 0.0
        pos = f"^{left:.2f} ^ ^{right:.2f}"
    else:
        pos = "~ ~ ~"

    v = "%.2f" % vol
    p = "%.3f" % pitch
    if edition == "java":
        return f"execute as @a at @s run playsound {part.sound} record @s {pos} {v} {p} {v}"
    return f"execute as @a at @s run playsound {part.sound} @s {pos} {v} {p} {v}"


def _serpentine(n: int, width: int, depth: int, max_height: int):
    """生成 n 个位置的 3D 蛇形路径与朝向。

    布局与 midi-mcstructure_next 推荐大模板（16×96×16）一致：
    每层在 width×depth 平面上水平蛇形（沿 z 往返、行间 +x），
    层间沿 +y 上升；每块朝向指向下一块。
    高度超过 ``max_height`` 时按比例增大足迹（保持方形）。

    返回 (positions, facings, width, height, depth)。
    """
    per_floor = width * depth
    height = max(1, math.ceil(n / per_floor))
    if height > max_height:
        # 增大足迹：side² >= ceil(n / max_height)
        side = max(width, depth, math.ceil(math.sqrt(math.ceil(n / max_height))))
        width = depth = side
        per_floor = width * depth
        height = max(1, math.ceil(n / per_floor))

    positions = []
    for i in range(n):
        y = i // per_floor
        rem = i % per_floor
        row = rem // depth
        z = rem % depth
        if row % 2 == 1:
            z = depth - 1 - z
        # 层间 x 方向交替（偶数层 x 0→width-1，奇数层反向），与参考模板一致
        x = row if y % 2 == 0 else width - 1 - row
        positions.append((x, y, z))

    facings = []
    for i in range(n - 1):
        x1, y1, z1 = positions[i]
        x2, y2, z2 = positions[i + 1]
        if y2 > y1:
            facings.append(_FACING_UP)
        elif y2 < y1:
            facings.append(_FACING_DOWN)
        elif x2 > x1:
            facings.append(_FACING_EAST)
        elif x2 < x1:
            facings.append(_FACING_WEST)
        elif z2 > z1:
            facings.append(_FACING_SOUTH)
        elif z2 < z1:
            facings.append(_FACING_NORTH)
        else:
            facings.append(_FACING_SOUTH)
    facings.append(_FACING_UP)  # 末块朝向任意

    return positions, facings, width, height, depth


def song_to_building(
    song: Song,
    *,
    width: int = 16,
    depth: int = 16,
    max_height: int = 96,
    edition: str = "bedrock",
    fold: bool = True,
) -> Building:
    """把一首歌转成命令方块音乐机建筑。

    - ``width``/``depth``: 水平足迹（默认 16×16，接近正方体，与
      midi-mcstructure_next 推荐大模板一致）；``max_height`` 超过时自动增大足迹。
    - ``edition``: ``"bedrock"`` / ``"java"``，决定 /playsound 语法。
    - ``fold``: ``True`` 把超范围音符八度折叠进可播放范围（pitch 0.5-2.0）
      保住相对音准（默认）；``False`` 保留原始八度（极端音符钳到两端）。
    - 音符按 1/20 秒量化到游戏刻，同一刻的多个音符延迟为 0（同时播放）。
    """
    if edition not in ("bedrock", "java"):
        raise ValueError(f"未知游戏版本 {edition!r}，用 bedrock 或 java")

    # 1. 音符 -> 事件（tick, note, part）。每个音符按乐器部件展开：
    #    多部件（orchestra_hit 同刻双音、telephone_ring 间隔 50ms 等）
    #    各占一个命令方块；无 instrument_parts（NBS/手写 Song）用单部件默认。
    events = []
    for note in song.notes:
        if note.velocity <= 0:
            continue
        parts = note.instrument_parts or (InstrumentPart(note.sound),)
        acc = 0.0
        for part in parts:
            acc += part.delay_sec
            events.append((round((note.time + acc) * TICKS_PER_SECOND), note, part))
    if not events:
        return Building(size=(0, 0, 0), source_format="music")
    events.sort(key=lambda e: e[0])

    # 2. 延迟序列：首个 delay = tick，其余 = 距上一音符的刻数
    cmd_list = []
    last = None
    for tick, note, part in events:
        if last is None:
            delay = tick
        else:
            delay = tick - last
        cmd_list.append((delay, _build_playsound(note, part, edition, fold)))
        last = tick

    # 3. 蛇形布局 + 4. 写 Building
    positions, facings, width_used, height, depth_used = _serpentine(
        len(cmd_list), width, depth, max_height
    )
    building = Building(size=(width_used, height, depth_used), source_format="music")
    for i, (delay, cmd) in enumerate(cmd_list):
        x, y, z = positions[i]
        mode = MODE_IMPULSE if i == 0 else MODE_CHAIN
        building.command_blocks.append(
            CommandBlock(
                x=x, y=y, z=z,
                mode=mode,
                command=cmd,
                tick_delay=delay,
                conditional=False,
                needs_redstone=(i == 0),  # 首块需红石触发，其余 auto
            )
        )
        building.blocks.append(
            Block(
                x=x, y=y, z=z,
                name=COMMAND_BLOCK_IDS[mode],
                states={"facing_direction": facings[i]},
            )
        )
    return building
