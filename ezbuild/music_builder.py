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
from .song import Song, is_drum_sound

# 游戏刻：1 秒 = 20 刻
TICKS_PER_SECOND = 20

# MIDI 音符 -> playsound pitch 的基准（note 66 = F#4 -> pitch 1.0）
PITCH_BASE = 66
MIN_PITCH = 0.5
MAX_PITCH = 2.0

# Bedrock facing_direction: 0 下 1 上 2 北(-z) 3 南(+z) 4 西(-x) 5 东(+x)
_FACING_UP = 1
_FACING_DOWN = 0
_FACING_SOUTH = 3
_FACING_NORTH = 2


def _build_playsound(note, edition: str) -> str:
    """生成一条 /playsound 命令（不含前导斜杠）。

    基岩版：``execute as @a at @s run playsound <sound> @s <pos> <vol> <pitch> <vol>``
    Java 版：中间多一个 ``record`` 音源参数。
    """
    if is_drum_sound(note.sound):
        pitch = 1.0
    else:
        pitch = 2.0 ** ((note.note - PITCH_BASE) / 12.0)
    pitch = max(MIN_PITCH, min(MAX_PITCH, pitch))
    vol = max(0.0, min(1.0, note.velocity))

    if note.panning:
        left = -note.panning if note.panning < 0 else 0.0
        right = note.panning if note.panning > 0 else 0.0
        pos = f"^{left:.2f} ^ ^{right:.2f}"
    else:
        pos = "~ ~ ~"

    v = "%.2f" % vol
    p = "%.3f" % pitch
    if edition == "java":
        return f"execute as @a at @s run playsound {note.sound} record @s {pos} {v} {p} {v}"
    return f"execute as @a at @s run playsound {note.sound} @s {pos} {v} {p} {v}"


def _serpentine(n: int, depth: int, max_height: int):
    """生成 n 个位置的蛇形路径与朝向。

    布局：x=0 单列，沿 z 往返（depth 深），行间沿 +y 上升；
    每块的朝向指向下一块。返回 (positions, facings, height, depth)。
    超出 ``max_height`` 时自动加深 ``depth``。
    """
    height = max(1, math.ceil(n / depth))
    if height > max_height:
        depth = max(1, math.ceil(n / max_height))
        height = max_height

    positions = []
    for i in range(n):
        y = i // depth
        z = i % depth
        if y % 2 == 1:
            z = depth - 1 - z
        positions.append((0, y, z))

    facings = []
    for i in range(n - 1):
        _, y1, z1 = positions[i]
        _, y2, z2 = positions[i + 1]
        if y2 > y1:
            facings.append(_FACING_UP)
        elif y2 < y1:
            facings.append(_FACING_DOWN)
        elif z2 > z1:
            facings.append(_FACING_SOUTH)
        elif z2 < z1:
            facings.append(_FACING_NORTH)
        else:
            facings.append(_FACING_SOUTH)
    facings.append(_FACING_UP)  # 末块朝向任意

    return positions, facings, height, depth


def song_to_building(
    song: Song,
    *,
    max_height: int = 320,
    depth: int = 16,
    edition: str = "bedrock",
) -> Building:
    """把一首歌转成命令方块音乐机建筑。

    - ``edition``: ``"bedrock"`` / ``"java"``，决定 /playsound 语法。
    - 音符按 1/20 秒量化到游戏刻，同一刻的多个音符延迟为 0（同时播放）。
    """
    if edition not in ("bedrock", "java"):
        raise ValueError(f"未知游戏版本 {edition!r}，用 bedrock 或 java")

    # 1. 音符 -> 事件（tick, 命令）
    events = []
    for note in song.notes:
        if note.velocity <= 0:
            continue
        events.append((round(note.time * TICKS_PER_SECOND), _build_playsound(note, edition)))
    if not events:
        return Building(size=(0, 0, 0), source_format="music")
    events.sort(key=lambda e: e[0])

    # 2. 延迟序列：首个 delay = tick，其余 = 距上一音符的刻数
    cmd_list = []
    last = None
    for tick, cmd in events:
        if last is None:
            delay = tick
        else:
            delay = tick - last
        cmd_list.append((delay, cmd))
        last = tick

    # 3. 蛇形布局 + 4. 写 Building
    positions, facings, height, depth_used = _serpentine(len(cmd_list), depth, max_height)
    building = Building(size=(1, height, depth_used), source_format="music")
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
