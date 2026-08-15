"""中立音乐模型：Song / Note / Layer。

所有音乐 Reader（mid / nbs）把文件解析为 :class:`Song`，
所有音乐 Writer（mid / nbs）从 :class:`Song` 渲染输出。
``Note.sound`` 是规范乐器（Minecraft 音效名），MIDI 程序号与 NBS 乐器号
都先映射到 sound，再映射回对方，从而在两种格式间保持一致。

转换约定（参考 musicplayer 的 midilib.py / nbslib.py 与 midi2nbs.py）：
    - NBS key = MIDI note - 21（社区标准，key 0-87 对应钢琴 88 键）。
    - 节拍网格：1 NBS tick = 1/4 拍（16 分音符），NBS tempo = bpm/15。
    - ``Note.velocity`` 为有效音量 0-1：MIDI 读入时折入通道音量 CC7，
      NBS 读入时折入层音量；写出时直接用它计算音符力度。
    - 时间以秒存储，保证 NBS <-> MID <-> NBS round-trip 精确。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 中立模型
# ---------------------------------------------------------------------------

@dataclass
class Note:
    """一个音符（中立表示）。

    - ``time``：开始时间（秒）
    - ``note``：MIDI 音符号 0-127（规范音高）
    - ``velocity``：有效音量 0-1（已折入通道/层音量）
    - ``sound``：Minecraft 音效名（规范乐器，如 ``note.harp``）
    - ``channel``：MIDI 通道 0-15（9 = 打击乐）
    - ``layer``：NBS 层索引
    - ``panning``：声像 -1..1（0 = 居中）
    - ``pitch_bend``：微调半音数（NBS fine tune / MIDI pitch bend）
    - ``duration``：时长（秒）；0 = 瞬时音符（NBS 来源）
    """

    time: float
    note: int
    velocity: float
    sound: str
    channel: int = 0
    layer: int = 0
    panning: float = 0.0
    pitch_bend: float = 0.0
    duration: float = 0.0


@dataclass
class Layer:
    """NBS 层元数据（id 与名字；音量已折入音符 velocity）。"""

    id: int
    name: str = ""


@dataclass
class Song:
    """一首完整的歌（中立表示）。

    - ``tempo``：bpm（规范节拍；NBS tempo = bpm/15）
    - ``notes`` / ``layers``：音符与层
    - ``name`` / ``author`` / ``description``：元信息
    """

    name: str = ""
    notes: list[Note] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    tempo: float = 120.0
    author: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# 乐器映射表
# ---------------------------------------------------------------------------

# MIDI 音符 -> NBS key 的偏移（key = note - 21；社区标准，同 midi2nbs.py）
NBS_NOTE_OFFSET = 21

# NBS 标准乐器 0-15 -> Minecraft 音效（取自 nbslib.py INSTRUMENT_MAP）
NBS_INSTRUMENT_TO_SOUND = {
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

# Minecraft 音效 -> NBS 标准乐器（逆映射，互不相同）
SOUND_TO_NBS_INSTRUMENT = {v: k for k, v in NBS_INSTRUMENT_TO_SOUND.items()}

# 鼓类音效 -> MIDI 打击乐音符（NBS 鼓 -> 通道 9）
DRUM_SOUND_TO_PERCUSSION_NOTE = {
    'note.bd': 36,     # GM Bass Drum 1
    'note.snare': 38,  # GM Acoustic Snare
    'note.hat': 42,    # GM Closed Hi-hat
}


def is_drum_sound(sound: str) -> bool:
    """是否为标准鼓类音效（NBS 乐器 2/3/4 对应）。"""
    return sound in DRUM_SOUND_TO_PERCUSSION_NOTE


# GM 程序号 -> NBS 标准乐器号。
# 数据取自 midi2nbs.py（OctoFlare, MIT）midi_ins 表的 instrument 字段，
# 仅保留乐器号（其每乐器的 octave 位移本工具暂不采用）。
_PROGRAM_TO_NBS_INSTRUMENT = {
    0: 0, 1: 15, 2: 15, 3: 15, 4: 0, 5: 0, 6: 5, 7: 14,
    8: 7, 9: 7, 10: 7, 11: 10, 12: 10, 13: 9, 14: 7, 15: 5,
    16: 6, 17: 10, 18: 6, 19: 6, 20: 6, 21: 6, 22: 6, 23: 6,
    24: 5, 25: 5, 26: 0, 27: 5, 28: 1, 29: 12, 30: 12, 31: 5,
    32: 1, 33: 1, 34: 1, 35: 1, 36: 5, 37: 5, 38: 1, 39: 15,
    40: 6, 41: 6, 42: 6, 43: 6, 44: 6, 45: 1, 46: 0, 47: 3,
    48: 6, 49: 6, 50: 6, 51: 6, 52: 6, 53: 6, 54: 6, 55: 3,
    56: 6, 57: 6, 58: 6, 59: 12, 60: 6, 61: 12, 62: 12, 63: 6,
    64: 6, 65: 6, 66: 6, 67: 6, 68: 6, 69: 6, 70: 6, 71: 6,
    72: 6, 73: 6, 74: 6, 75: 6, 76: 6, 77: 6, 78: 6, 79: 6,
    80: 13, 81: 6, 82: 6, 83: 6, 84: 5, 85: 6, 86: 6, 87: 1,
    88: 7, 89: 6, 90: 6, 91: 6, 92: 6, 93: 6, 94: 6, 95: 8,
    96: 8, 97: 6, 98: 8, 99: 5, 100: 15, 101: 6, 102: 6, 103: 5,
    104: 14, 105: 14, 106: 14, 107: 5, 108: 10, 109: 6, 110: 6, 111: 6,
    112: 8, 113: 11, 114: 10, 115: 9, 116: 2, 117: 3, 118: 3, 119: 8,
    120: 4, 121: 6, 122: 8, 123: 6, 124: 7, 125: 2, 126: 3, 127: 3,
}

# GM 程序号 -> Minecraft 音效（旋律通道用）
PROGRAM_TO_SOUND = {
    prog: NBS_INSTRUMENT_TO_SOUND[ins]
    for prog, ins in _PROGRAM_TO_NBS_INSTRUMENT.items()
}

# Minecraft 音效 -> 代表程序号（每个 NBS 乐器取 midi_ins 中首个程序；
# 用于 NBS -> MIDI 还原程序号，round-trip 时音效保持不变）
SOUND_TO_PROGRAM = {}
for _prog, _ins in _PROGRAM_TO_NBS_INSTRUMENT.items():
    _sound = NBS_INSTRUMENT_TO_SOUND[_ins]
    SOUND_TO_PROGRAM.setdefault(_sound, _prog)
del _prog, _ins, _sound


# GM 打击乐（通道 9）音符 -> Minecraft 音效（取自 midilib.py PERCUSSION_MAP）
PERCUSSION_NOTE_TO_SOUND = {
    27: 'note.pling', 28: 'note.pling', 29: 'note.pling',
    30: 'note.pling', 31: 'note.pling',
    35: 'note.bd', 36: 'note.bd',                        # 底鼓
    37: 'note.snare', 38: 'note.snare',                  # 边击/军鼓
    39: 'note.hat', 40: 'note.snare',
    41: 'note.bd', 42: 'note.hat', 43: 'note.bd',        # 桶鼓/踩镲
    44: 'note.hat', 45: 'note.bd', 46: 'note.hat',
    47: 'note.bd', 48: 'note.bd',
    49: 'note.hat', 50: 'note.bell',                     # 镲
    51: 'note.snare', 52: 'note.pling', 53: 'note.pling',
    54: 'note.pling', 55: 'note.snare', 56: 'note.snare',
    57: 'note.hat', 58: 'note.pling', 59: 'note.pling',
    60: 'note.bell', 61: 'note.bell', 62: 'note.bell',
    63: 'note.pling', 64: 'note.pling', 65: 'note.pling',
    66: 'note.pling', 67: 'note.pling', 68: 'note.pling',
    69: 'note.hat', 70: 'note.snare',
    71: 'note.pling', 72: 'note.pling', 73: 'note.pling',
    74: 'note.pling', 75: 'note.pling', 76: 'note.pling',
    77: 'note.pling', 78: 'note.pling', 79: 'note.pling',
    80: 'note.pling', 81: 'note.pling',
}

DEFAULT_PERCUSSION_SOUND = 'note.bd'


def sound_to_program(sound: str) -> int:
    """音效名 -> MIDI 程序号；未知音效回退 0（钢琴）。"""
    return SOUND_TO_PROGRAM.get(sound, 0)


def sound_to_nbs_instrument(sound: str) -> int | None:
    """音效名 -> NBS 标准乐器号；不在标准表返回 None（需要自定义乐器）。"""
    return SOUND_TO_NBS_INSTRUMENT.get(sound)


def program_to_sound(program: int) -> str:
    """MIDI 程序号 -> 音效名；越界回退 note.harp。"""
    return PROGRAM_TO_SOUND.get(program, 'note.harp')


def percussion_note_to_sound(note: int) -> str:
    """GM 打击乐音符 -> 音效名；未映射回退 note.bd。"""
    return PERCUSSION_NOTE_TO_SOUND.get(note, DEFAULT_PERCUSSION_SOUND)
