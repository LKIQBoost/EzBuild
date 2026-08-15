"""nbs 读取器：把 NBS（OpenNoteBlockStudio）二进制解析为 Song。

NBS 二进制解析（``_Parser`` 及其辅助类）从 musicplayer 项目的
``nbslib.py`` 精简而来（保留全部版本分支 v<4），这里改为输出中立音符，
而不是 /playsound 命令。
"""

from __future__ import annotations

import os
import struct

from ..song import (
    DRUM_SOUND_TO_PERCUSSION_NOTE,
    NBS_NOTE_OFFSET,
    Note,
    Layer,
    Song,
    is_drum_sound,
)
from .base import Reader, Source

BYTE = struct.Struct('<B')
SHORT = struct.Struct('<H')
SSHORT = struct.Struct('<h')
INT = struct.Struct('<I')

# NBS 标准乐器映射（instrument id -> Minecraft 音效）
INSTRUMENT_MAP = {
    0: 'note.harp',
    1: 'note.bass',
    2: 'note.bd',
    3: 'note.snare',
    4: 'note.hat',
    5: 'note.guitar',
    6: 'note.flute',
    7: 'note.bell',
    8: 'note.chime',
    9: 'note.xylophone',
    10: 'note.iron_xylophone',
    11: 'note.cow_bell',
    12: 'note.didgeridoo',
    13: 'note.bit',
    14: 'note.banjo',
    15: 'note.pling',
}


# ---------------------------------------------------------------------------
# NBS 二进制解析（移植自 nbslib.py）
# ---------------------------------------------------------------------------

def _decode_string(raw):
    """NBS 字符串先按 UTF-8 解，失败再按 cp1252（兼容英文旧文件）。"""
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp1252', 'replace')


def _valid_sound_name(name):
    """自定义乐器名仅保留纯 ASCII 字母/数字/下划线/点，避免发出非法命令。"""
    for ch in name:
        o = ord(ch)
        if not ((48 <= o <= 57) or (65 <= o <= 90) or (97 <= o <= 122) or o == 95 or o == 46):
            return False
    return True


class _Header:
    def __init__(self):
        self.version = 0
        self.default_instruments = 10
        self.song_length = 0
        self.song_layers = 0
        self.song_name = ''
        self.tempo = 10.0
        self.loop = False


class _Note:
    __slots__ = ('tick', 'layer', 'instrument', 'key', 'velocity', 'panning', 'pitch')

    def __init__(self, tick, layer, instrument, key, velocity, panning, pitch):
        self.tick = tick
        self.layer = layer
        self.instrument = instrument
        self.key = key
        self.velocity = velocity
        self.panning = panning
        self.pitch = pitch


class _Layer:
    __slots__ = ('id', 'name', 'volume')

    def __init__(self, i, name, volume):
        self.id = i
        self.name = name
        self.volume = volume


class _Instrument:
    __slots__ = ('id', 'name')

    def __init__(self, i, name):
        self.id = i
        self.name = name


class _Song:
    def __init__(self, header, notes, layers, instruments):
        self.header = header
        self.notes = notes
        self.layers = layers
        self.instruments = instruments


class _Parser:
    def __init__(self, data):
        self.data = data
        self.off = 0

    def _read(self, fmt):
        val = fmt.unpack_from(self.data, self.off)[0]
        self.off += fmt.size
        return val

    def _read_string(self):
        length = self._read(INT)
        raw = self.data[self.off:self.off + length]
        self.off += length
        return _decode_string(raw)

    def _jump(self):
        value = -1
        while True:
            j = self._read(SHORT)
            if not j:
                break
            value += j
            yield value

    def parse(self):
        song_length = self._read(SHORT)
        if song_length == 0:
            version = self._read(BYTE)
        else:
            version = 0

        header = _Header()
        header.version = version
        header.default_instruments = self._read(BYTE) if version > 0 else 10
        header.song_length = self._read(SHORT) if version >= 3 else song_length
        header.song_layers = self._read(SHORT)
        header.song_name = self._read_string()
        self._read_string()  # song_author
        self._read_string()  # original_author
        self._read_string()  # description
        header.tempo = self._read(SHORT) / 100.0
        self._read(BYTE)     # auto_save
        self._read(BYTE)     # auto_save_duration
        self._read(BYTE)     # time_signature
        self._read(INT)      # minutes_spent
        self._read(INT)      # left_clicks
        self._read(INT)      # right_clicks
        self._read(INT)      # blocks_added
        self._read(INT)      # blocks_removed
        self._read_string()  # song_origin
        header.loop = self._read(BYTE) == 1 if version >= 4 else False
        self._read(BYTE)     # max_loop_count
        self._read(SHORT)    # loop_start

        notes = []
        for current_tick in self._jump():
            for current_layer in self._jump():
                instrument = self._read(BYTE)
                key = self._read(BYTE)
                velocity = self._read(BYTE) if version >= 4 else 100
                panning = self._read(BYTE) - 100 if version >= 4 else 0
                pitch = self._read(SSHORT) if version >= 4 else 0
                notes.append(_Note(current_tick, current_layer, instrument, key,
                                   velocity, panning, pitch))

        layers = []
        for i in range(header.song_layers):
            name = self._read_string()       # layer name
            if version >= 4:
                self._read(BYTE)             # lock
            volume = self._read(BYTE)
            if version >= 2:
                self._read(BYTE)             # panning
            layers.append(_Layer(i, name, volume))

        instruments = []
        num_instruments = self._read(BYTE)
        for i in range(num_instruments):
            name = self._read_string()
            self._read_string()              # sound file
            self._read(BYTE)                 # pitch
            self._read(BYTE)                 # press_key
            instruments.append(_Instrument(i, name))

        return _Song(header, notes, layers, instruments)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class NbsReader(Reader):
    format_name = "nbs"
    extensions = (".nbs",)
    description = "OpenNoteBlockStudio NBS 音乐文件"

    def read(self, source: Source) -> Song:
        data = self._read_bytes(source)
        song = _Parser(data).parse()
        header = song.header
        tps = header.tempo if header.tempo > 0 else 10.0

        # 乐器表：标准 + 自定义（id 16+i，名字为合法音效名）
        inst_map = dict(INSTRUMENT_MAP)
        for inst in song.instruments:
            name = (inst.name or '').strip()
            if name and _valid_sound_name(name):
                inst_map[16 + inst.id] = name

        layer_vol = {}
        for layer in song.layers:
            layer_vol[layer.id] = (layer.volume if layer.volume else 100) / 100.0

        out = Song(
            name=(header.song_name or '').strip() or self._display_name(source),
            tempo=tps * 15.0,  # NBS tempo = bpm/15
        )

        layer_names = {layer.id: layer.name for layer in song.layers}

        for note in song.notes:
            sound = inst_map.get(note.instrument)
            if not sound:
                continue
            vel = (note.velocity if note.velocity else 100) / 100.0
            vel *= layer_vol.get(note.layer, 1.0)
            if is_drum_sound(sound):
                # 鼓走 MIDI 通道 9，必须用标准 GM 打击乐音符
                midi_note = DRUM_SOUND_TO_PERCUSSION_NOTE[sound]
            else:
                midi_note = note.key + NBS_NOTE_OFFSET
                if midi_note > 127:
                    midi_note = 127
                elif midi_note < 0:
                    midi_note = 0
            channel = self._channel_for(note.layer, sound)
            out.notes.append(
                Note(
                    time=round(note.tick / tps, 4),
                    note=midi_note,
                    velocity=round(vel, 4),
                    sound=sound,
                    channel=channel,
                    layer=note.layer,
                    panning=round(note.panning / 100.0, 4),
                    pitch_bend=round(note.pitch / 100.0, 4),
                )
            )

        ids = sorted({n.layer for n in out.notes})
        for i in ids:
            out.layers.append(Layer(id=i, name=layer_names.get(i, '')))
        return out

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _display_name(source: Source) -> str:
        if isinstance(source, (bytes, bytearray)):
            return "nbs"
        return os.path.basename(str(source))

    @staticmethod
    def _channel_for(layer: int, sound: str) -> int:
        """NBS 层 -> MIDI 通道：鼓走 9；旋律层重映射避开通道 9。"""
        if is_drum_sound(sound):
            return 9
        ch = layer if layer < 9 else layer + 1
        return min(ch, 15)
