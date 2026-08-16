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

@dataclass(frozen=True, slots=True)
class InstrumentPart:
    """一个音符的"乐器部件"（参照 midi-mcstructure_next 的 sound_list）。

    MIDI 程序号 / 打击乐音符最终解析为一组部件（多为 1 个，少数如
    telephone_ring / orchestra_hit / helicopter 是多个）：每个部件是
    一条实际 playsound 音效，带**响度补偿**、**半音偏移**与**相对上一部件
    的间隔秒**。只被 ``music_builder``（命令方块转换）消费，不影响
    NBS/MIDI 写出，因此 mid↔nbs round-trip 保持不变。
    """

    sound: str
    volume: float = 1.0       # 响度补偿（乘到音符力度上，如 note.bell 3.0）
    pitch_offset: float = 0.0  # 半音偏移（镲片 +19/+31/+38 等，加到音高公式里）
    delay_sec: float = 0.0    # 距上一部件的间隔秒（reference 的 delay 是 ms）


@dataclass(slots=True)
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
    instrument_parts: tuple[InstrumentPart, ...] = ()  # 空 = 单部件默认（仅 building 转换用）


@dataclass(slots=True)
class Layer:
    """NBS 层元数据（id 与名字；音量已折入音符 velocity）。"""

    id: int
    name: str = ""


@dataclass(slots=True)
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


# ---------------------------------------------------------------------------
# MIDI 乐器映射（移植自 midi-mcstructure_next 的 mapping.json + profile.json
# new_bedrock.sound_list）
#
# 两层：MIDI 程序号/打击乐音符 -> 抽象音效名 -> 实际 playsound 部件列表。
# 每个部件带响度补偿（volume）、半音偏移（pitch_offset）与相对上一部件间隔
# （delay_sec）。抽象名 -> 部件逐条取自 new_bedrock.sound_list；程序号 ->
# 抽象名取自 mapping.json 非打击乐段；打击乐音符 -> 抽象名取自其 percussion
# 段，但**保留 ezbuild 核心鼓约定**（36↔note.bd、38↔note.snare、42↔note.hat，
# 保证 mid↔nbs 往返不变），其余一律用参考表。
# ---------------------------------------------------------------------------

# 抽象音效名 -> 实际 playsound 部件（new_bedrock.sound_list）
_ABSTRACT_PARTS = {
    'harp': (InstrumentPart('note.harp', 0.53),),
    'pling': (InstrumentPart('note.pling', 0.4),),
    'bit': (InstrumentPart('note.bit', 1.2),),
    'rain': (InstrumentPart('ambient.weather.rain', 1.0),),
    'xylophone': (InstrumentPart('note.xylophone', 0.9),),
    'iron_xylophone': (InstrumentPart('note.iron_xylophone', 0.86),),
    'banjo': (InstrumentPart('note.banjo', 0.8),),
    'flute': (InstrumentPart('note.flute', 0.8),),
    'chime': (InstrumentPart('note.chime', 1.0),),
    'bass': (InstrumentPart('note.bass', 1.0),),
    'guitar': (InstrumentPart('note.guitar', 1.0),),
    'bell': (InstrumentPart('note.bell', 3.0),),
    'cow_bell': (InstrumentPart('note.cow_bell', 0.68),),
    'hat': (InstrumentPart('note.hat', 1.0),),
    'snare': (InstrumentPart('note.snare', 1.0),),
    'base_drum': (InstrumentPart('note.bd', 1.5),),
    'didgeridoo': (InstrumentPart('note.didgeridoo', 0.8),),
    # 1.21 铜管音阶块（4 种氧化态）
    'trumpet': (InstrumentPart('note.trumpet', 0.7),),
    'french_horn': (InstrumentPart('note.trumpet_exposed', 0.7),),
    'trombone': (InstrumentPart('note.trumpet_weathered', 0.7),),
    'tuba': (InstrumentPart('note.trumpet_oxidized', 0.7),),
    # 非音阶块音效：镲片用 random.fizz + 半音偏移区分（转换时不钳到 2.0）
    'cymbal': (InstrumentPart('random.fizz', 1.0, 19),),
    'open_cymbal': (InstrumentPart('random.fizz', 1.5, 19),),
    'pedal_cymbal': (InstrumentPart('random.fizz', 1.5, 31),),
    'closed_cymbal': (InstrumentPart('random.fizz', 1.5, 38),),
    'cabasa': (InstrumentPart('random.fizz', 1.2, 19),),
    'shaker': (InstrumentPart('random.fizz', 1.2, 31),),
    'parrot': (InstrumentPart('mob.parrot.idle', 1.0),),
    'crystal': (InstrumentPart('chime.amethyst_block', 2.0),),
    'gun': (InstrumentPart('random.explode', 1.0),),
    # 多部件：同刻双音
    'orchestra_hit': (
        InstrumentPart('note.bass', 0.4),
        InstrumentPart('note.flute', 0.3),
    ),
    # 多部件：电话铃 = note.bit 高低交替 8 次，间隔 50ms
    'telephone_ring': (
        InstrumentPart('note.bit', 2.0, 6, 0.0),
        InstrumentPart('note.bit', 2.0, -6, 0.05),
        InstrumentPart('note.bit', 2.0, 6, 0.05),
        InstrumentPart('note.bit', 2.0, -6, 0.05),
        InstrumentPart('note.bit', 2.0, 6, 0.05),
        InstrumentPart('note.bit', 2.0, -6, 0.05),
        InstrumentPart('note.bit', 2.0, 6, 0.05),
        InstrumentPart('note.bit', 2.0, -6, 0.05),
    ),
    # 多部件：直升机 = breeze 引擎 + 8 次旋翼声，间隔 100ms
    'helicopter': (
        InstrumentPart('mob.breeze.idle_ground', 0.5),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
        InstrumentPart('mob.breeze.death', 0.5, 0, 0.1),
    ),
}

# MIDI 程序号 -> 抽象音效名（mapping.json 非打击乐段；缺省 = harp）
_PROGRAM_TO_ABSTRACT = {
    0: 'harp', 1: 'pling', 2: 'pling', 6: 'guitar', 7: 'iron_xylophone',
    8: 'iron_xylophone', 9: 'bell', 10: 'bell', 11: 'iron_xylophone',
    12: 'iron_xylophone', 13: 'xylophone', 14: 'chime',
    16: 'flute', 17: 'flute', 18: 'flute', 19: 'flute', 20: 'flute',
    21: 'flute', 22: 'flute', 23: 'flute',
    24: 'guitar', 25: 'guitar', 26: 'guitar', 27: 'guitar', 28: 'guitar',
    29: 'guitar', 30: 'bass', 31: 'bass', 32: 'bass', 33: 'guitar',
    34: 'guitar', 35: 'bass', 36: 'bass', 37: 'bass', 38: 'bass',
    39: 'bass', 40: 'flute', 41: 'flute', 42: 'flute', 43: 'didgeridoo',
    45: 'pling', 46: 'harp', 47: 'iron_xylophone',
    48: 'flute', 49: 'flute', 50: 'flute', 51: 'flute', 52: 'didgeridoo',
    53: 'flute', 54: 'flute', 55: 'orchestra_hit',
    56: 'trumpet', 57: 'trombone', 58: 'tuba', 59: 'trumpet',
    60: 'french_horn',
    61: 'flute', 62: 'flute', 63: 'flute', 64: 'flute', 65: 'guitar',
    66: 'bass', 67: 'didgeridoo', 68: 'flute', 69: 'flute',
    70: 'didgeridoo', 71: 'flute', 72: 'flute', 73: 'flute', 74: 'flute',
    75: 'flute', 76: 'flute', 77: 'banjo', 78: 'flute', 79: 'flute',
    80: 'bit', 81: 'bit', 82: 'bit', 83: 'bit', 84: 'bit',
    85: 'flute', 86: 'flute', 87: 'orchestra_hit', 88: 'banjo',
    89: 'flute', 90: 'bit', 91: 'orchestra_hit', 92: 'didgeridoo',
    93: 'iron_xylophone', 94: 'bit', 95: 'bit', 96: 'rain',
    98: 'crystal', 105: 'banjo', 108: 'xylophone',
    109: 'flute', 110: 'flute', 111: 'flute', 112: 'cow_bell',
    114: 'hat', 115: 'xylophone', 116: 'base_drum', 117: 'snare',
    118: 'snare', 119: 'cymbal', 123: 'parrot', 124: 'telephone_ring',
    125: 'helicopter', 126: 'hat', 127: 'gun', 612: 'trumpet',
}

_DEFAULT_ABSTRACT = 'harp'

# GM 打击乐（通道 9）音符 -> 抽象音效名（mapping.json percussion 段；
# 36/38/42 覆盖为 ezbuild 核心鼓约定保 mid↔nbs 往返；未列出 -> cymbal）
_PERCUSSION_TO_ABSTRACT = {
    31: 'hat', 33: 'snare', 34: 'hat', 35: 'base_drum', 36: 'base_drum',
    37: 'hat', 38: 'snare', 39: 'hat', 40: 'hat', 41: 'base_drum',
    42: 'hat', 43: 'hat', 44: 'pedal_cymbal', 45: 'base_drum',
    46: 'open_cymbal', 47: 'snare', 48: 'hat', 50: 'hat',
    56: 'cow_bell', 69: 'cabasa', 82: 'shaker', 83: 'bell', 84: 'bell',
}

_DEFAULT_PERCUSSION_ABSTRACT = 'cymbal'


def _parts_of(abstract: str) -> tuple[InstrumentPart, ...]:
    """抽象音效名 -> 部件；未知抽象名回退 harp。"""
    return _ABSTRACT_PARTS.get(abstract, _ABSTRACT_PARTS[_DEFAULT_ABSTRACT])


def program_to_parts(program: int) -> tuple[InstrumentPart, ...]:
    """MIDI 程序号 -> 乐器部件列表（首部件音效即音色名）。"""
    return _parts_of(_PROGRAM_TO_ABSTRACT.get(program, _DEFAULT_ABSTRACT))


def percussion_note_to_parts(note: int) -> tuple[InstrumentPart, ...]:
    """GM 打击乐音符 -> 乐器部件列表；未映射回退镲片 (random.fizz)。"""
    return _parts_of(_PERCUSSION_TO_ABSTRACT.get(note, _DEFAULT_PERCUSSION_ABSTRACT))


# GM 程序号 -> 音效名（首部件；NBS->MIDI 音效还原）
PROGRAM_TO_SOUND = {
    prog: program_to_parts(prog)[0].sound
    for prog in range(128)
}

# Minecraft 音效 -> 代表程序号（每个音效取首个程序；MIDI 写出用）
SOUND_TO_PROGRAM = {}
for _prog in range(128):
    SOUND_TO_PROGRAM.setdefault(program_to_parts(_prog)[0].sound, _prog)
del _prog


def sound_to_program(sound: str) -> int:
    """音效名 -> MIDI 程序号；未知音效回退 0（钢琴）。"""
    return SOUND_TO_PROGRAM.get(sound, 0)


def sound_to_nbs_instrument(sound: str) -> int | None:
    """音效名 -> NBS 标准乐器号；不在标准表返回 None（需要自定义乐器）。"""
    return SOUND_TO_NBS_INSTRUMENT.get(sound)


def program_to_sound(program: int) -> str:
    """MIDI 程序号 -> 音效名；越界回退 note.harp。"""
    return program_to_parts(program)[0].sound


def percussion_note_to_sound(note: int) -> str:
    """GM 打击乐音符 -> 音效名；未映射回退 random.fizz（镲片）。"""
    return percussion_note_to_parts(note)[0].sound
