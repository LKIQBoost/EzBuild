"""mid 输出器：把 Song 渲染为 MIDI（SMF format 0）二进制。

- division = 480，tempo 由 ``Song.tempo``（bpm）决定，tick = time_sec × 8 × bpm，
  与 NBS 的 16 分音符网格（NBS tempo = bpm/15）互逆，保证 round-trip 精确。
- 每个通道在 note_on 前调和程序号 / CC10 声像 / pitch bend 状态。
- 同 tick 事件顺序：control < note_off < note_on（先关后开）。
"""

from __future__ import annotations

import struct

from ..model import Building
from ..song import Song, sound_to_program
from .base import Writer

# SMF 每四分音符 tick 数（固定）
DIVISION = 480
# 无时长音符（NBS 来源）写 MIDI 时的默认时长
DEFAULT_NOTE_SECONDS = 0.1


def _encode_vlq(value: int) -> bytes:
    """SMF 可变长度量编码（7 位分组，MSB 续位）。"""
    out = bytearray()
    out.append(value & 0x7F)
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.reverse()
    return bytes(out)


class MidWriter(Writer):
    format_name = "mid"
    extensions = (".mid", ".midi")
    description = "MIDI (SMF) 音乐文件"

    def render(self, model: Song | Building) -> bytes:
        if not isinstance(model, Song):
            raise TypeError(
                f"MidWriter 需要 Song 模型，收到 {type(model).__name__}"
            )
        tempo = model.tempo if model.tempo > 0 else 120.0
        spb = 8.0 * tempo  # 每秒 tick 数（division 480 / 每拍秒数）

        # 收集事件：(tick, order, bytes)。order: 0=control, 1=off, 2=on
        events = []
        prog = {}  # ch -> 当前程序号
        pan = {}   # ch -> 当前声像 -1..1
        bend = {}  # ch -> 当前弯音（半音）
        for n in model.notes:
            ch = n.channel
            t_on = round(n.time * spb)
            t_off = max(t_on + 1, round((n.time + max(n.duration, DEFAULT_NOTE_SECONDS)) * spb))

            p = sound_to_program(n.sound)
            if prog.get(ch) != p:
                events.append((t_on, 0, bytes((0xC0 | ch, p))))
                prog[ch] = p
            if pan.get(ch) != n.panning:
                cc = max(0, min(127, round(n.panning * 64) + 64))
                events.append((t_on, 0, bytes((0xB0 | ch, 0x0A, cc))))
                pan[ch] = n.panning
            if bend.get(ch) != n.pitch_bend:
                raw = max(0, min(16383, round(8192 + n.pitch_bend * 4096)))
                events.append((t_on, 0, bytes((0xE0 | ch, raw & 0x7F, (raw >> 7) & 0x7F))))
                bend[ch] = n.pitch_bend

            vel = max(1, min(127, round(n.velocity * 127)))
            events.append((t_on, 2, bytes((0x90 | ch, n.note, vel))))
            events.append((t_off, 1, bytes((0x80 | ch, n.note, 64))))

        events.sort(key=lambda e: (e[0], e[1]))

        tempo_us = max(1, min(0xFFFFFF, round(60000000.0 / tempo)))
        tempo_evt = b"\xff\x51\x03" + bytes((tempo_us >> 16 & 0xFF, tempo_us >> 8 & 0xFF, tempo_us & 0xFF))
        timesig = b"\xff\x58\x04\x04\x02\x18\x08"  # 4/4

        body = bytearray(b"\x00" + tempo_evt + b"\x00" + timesig)
        prev = 0
        for tick, _order, data in events:
            body += _encode_vlq(tick - prev) + data
            prev = tick
        body += b"\x00\xff\x2f\x00"  # 轨结束

        header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, DIVISION)
        track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
        return header + track
