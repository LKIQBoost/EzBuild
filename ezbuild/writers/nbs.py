"""nbs 输出器：把 Song 渲染为 NBS（OpenNoteBlockStudio v4）二进制。

- NBS tempo = bpm/15（16 分音符网格），与 MIDI 输出互逆，round-trip 精确。
- 音符用 tick / layer 跳转编码：首跳 = 值 + 1，逐跳累差，每组以 0 结束。
- 力度钳制到 [1,100]（NBS 的 0 会被读作 100）；key 钳制到字节范围。
"""

from __future__ import annotations

import struct

from ..model import Building
from ..song import (
    NBS_NOTE_OFFSET,
    SOUND_TO_NBS_INSTRUMENT,
    Song,
)
from .base import Writer

BYTE = struct.Struct('<B')
SHORT = struct.Struct('<H')
SSHORT = struct.Struct('<h')
INT = struct.Struct('<I')

# NBS tick 上限：首个 tick 跳 = tick + 1 且为单个 SHORT（最大 65535）
MAX_TICK = 65534

# 当前 NBS 格式版本（pynbs 的 CURRENT_NBS_VERSION；v4/v5 磁盘结构相同，
# 但 OpenNoteBlockStudio 新版把 v4 当旧版处理，必须写 5）
NBS_VERSION = 5

# 标准乐器数量（当前 ONBS 有 16 个默认乐器；写少了会让 ONBS 把
# 10-15 号乐器当成未定义的自定义乐器）
DEFAULT_INSTRUMENTS = 16

# Minecraft 音阶块可播放 key 范围（pitch 0.5-2.0，F#3-F#5）
MIN_PLAYABLE_KEY = 33
MAX_PLAYABLE_KEY = 57


def _enc_string(s: str) -> bytes:
    """NBS 字符串：INT 长度前缀 + cp1252 字节。

    NBS 格式（pynbs / OpenNoteBlockStudio）按 cp1252 严格解码，写入非
    cp1252 字节（如 UTF-8 中文）会让它崩溃；这里把无法编码的字符替换为
    ``?``。ASCII 名不受影响；nbslib 播放器读 cp1252 也会回退成功。
    """
    raw = s.encode('cp1252', errors='replace')
    return INT.pack(len(raw)) + raw


class NbsWriter(Writer):
    format_name = "nbs"
    extensions = (".nbs",)
    description = "OpenNoteBlockStudio NBS 音乐文件"

    def __init__(self, fold_range: bool = True):
        """``fold_range``: 把 key 八度折叠进可播放范围 [33,57]（pitch 0.5-2.0），
        保证在 Minecraft 音阶块内能发声；``False`` 保留原始音高（round-trip 精确）。"""
        self.fold_range = fold_range

    def render(self, model: Song | Building) -> bytes:
        if not isinstance(model, Song):
            raise TypeError(
                f"NbsWriter 需要 Song 模型，收到 {type(model).__name__}"
            )
        tempo = model.tempo if model.tempo > 0 else 120.0
        tps = tempo / 15.0  # 1 NBS tick = 1/4 拍

        # tick 溢出时按比例下调 tps，保持绝对时间不变
        ticks = [round(n.time * tps) for n in model.notes]
        max_tick = max(ticks) if ticks else 0
        if max_tick > MAX_TICK:
            tps = max(1.0, tps * MAX_TICK / max_tick)
            ticks = [round(n.time * tps) for n in model.notes]
            max_tick = max(ticks)
            if max_tick > MAX_TICK:
                raise ValueError("歌曲过长，无法写入 NBS（超出 tick 上限）")

        # 乐器号：标准表 + 自定义（id 16+i）
        sound_to_inst = dict(SOUND_TO_NBS_INSTRUMENT)
        custom_names = []
        for n in model.notes:
            if n.sound not in sound_to_inst and n.sound not in custom_names:
                custom_names.append(n.sound)
        for i, s in enumerate(custom_names):
            sound_to_inst[s] = 16 + i

        # (tick, layer) 唯一；冲突保留力度最大的
        notes_map = {}
        for n, t in zip(model.notes, ticks):
            vel = max(1, min(100, round(n.velocity * 100)))
            key = n.note - NBS_NOTE_OFFSET
            if self.fold_range:
                key = self._fold_key(key)
            else:
                key = max(0, min(255, key))
            entry = (
                sound_to_inst[n.sound],
                key,
                vel,
                max(0, min(200, round(n.panning * 100) + 100)),
                max(-32768, min(32767, round(n.pitch_bend * 100))),
            )
            keypos = (t, n.layer)
            if keypos not in notes_map or notes_map[keypos][2] < vel:
                notes_map[keypos] = entry

        sorted_keys = sorted(notes_map)
        max_ref_layer = max((k[1] for k in sorted_keys), default=-1)
        song_length = max_tick + 1 if sorted_keys else 0
        song_layers = max(max_ref_layer + 1, len(model.layers))

        out = bytearray()
        out += self._header(tps, song_length, song_layers, model)
        out += self._notes(sorted_keys, notes_map)
        out += self._layer_records(song_layers, model.layers)
        out += self._instruments(custom_names)
        return bytes(out)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _fold_key(key: int) -> int:
        """把 key 八度折叠进 [33,57]，保证可播放（音高超出范围时按八度搬移）。"""
        key = max(0, min(255, key))
        while key < MIN_PLAYABLE_KEY:
            key += 12
        while key > MAX_PLAYABLE_KEY:
            key -= 12
        return key

    @staticmethod
    def _header(tps: float, song_length: int, song_layers: int, model: Song) -> bytes:
        h = bytearray()
        h += SHORT.pack(0)                       # song_length=0 表示后面有 version
        h += BYTE.pack(NBS_VERSION)              # version（当前 v5）
        h += BYTE.pack(DEFAULT_INSTRUMENTS)      # default_instruments
        h += SHORT.pack(song_length)             # 实际长度
        h += SHORT.pack(song_layers)
        h += _enc_string(model.name)
        h += _enc_string(model.author)
        h += _enc_string("")                     # original_author
        h += _enc_string(model.description)
        h += SHORT.pack(max(1, min(65535, round(tps * 100))))  # tempo ×100
        h += BYTE.pack(1)                        # auto_save
        h += BYTE.pack(10)                       # auto_save_duration
        h += BYTE.pack(4)                        # time_signature
        h += INT.pack(0) * 5                     # minutes/left/right/added/removed
        h += _enc_string("")                     # song_origin
        h += BYTE.pack(0)                        # loop
        h += BYTE.pack(0)                        # max_loop_count
        h += SHORT.pack(0)                       # loop_start
        return bytes(h)

    @staticmethod
    def _notes(sorted_keys, notes_map) -> bytes:
        buf = bytearray()
        prev_tick = -1
        prev_layer = -1
        first = True
        for tick, layer in sorted_keys:
            if tick != prev_tick:
                if not first:
                    buf += SHORT.pack(0)         # 结束上一 tick 的层跳
                buf += SHORT.pack(tick - prev_tick)
                prev_tick = tick
                prev_layer = -1
                first = False
            buf += SHORT.pack(layer - prev_layer)
            prev_layer = layer
            inst, key, vel, pan, pitch = notes_map[(tick, layer)]
            buf += BYTE.pack(inst)
            buf += BYTE.pack(key)
            buf += BYTE.pack(vel)
            buf += BYTE.pack(pan)
            buf += SSHORT.pack(pitch)
        if sorted_keys:
            buf += SHORT.pack(0)                 # 结束最后一 tick 的层跳
        buf += SHORT.pack(0)                     # 结束 tick 跳（空歌也需要）
        return bytes(buf)

    @staticmethod
    def _layer_records(song_layers: int, layers) -> bytes:
        name_by_id = {layer.id: layer.name for layer in layers}
        buf = bytearray()
        for i in range(song_layers):
            buf += _enc_string(name_by_id.get(i, ""))
            buf += BYTE.pack(0)                  # lock
            buf += BYTE.pack(100)                # volume（力度已折入音符）
            buf += BYTE.pack(100)                # panning（居中）
        return bytes(buf)

    @staticmethod
    def _instruments(custom_names) -> bytes:
        buf = bytearray()
        buf += BYTE.pack(len(custom_names))
        for name in custom_names:
            buf += _enc_string(name)
            buf += _enc_string("")               # sound file
            buf += BYTE.pack(0)                  # pitch
            buf += BYTE.pack(0)                  # press_key
        return bytes(buf)
