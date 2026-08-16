"""mid / nbs 音乐格式单元测试：Song → Writer → Reader round-trip。

沿用 fixtures.py 的内存合成模式：直接构造 Song，用 Writer 渲染出字节，
再经 Reader 读回比较。
"""

import struct

import pytest

import ezbuild
from ezbuild.model import Building
from ezbuild.song import Note, Song


# ---------------------------------------------------------------------------
# 辅助：构造测试 Song / 读写
# ---------------------------------------------------------------------------

def _make_song(name="test", tempo=120.0, **kw) -> Song:
    song = Song(name=name, tempo=tempo, **kw)
    song.notes = [
        Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0, layer=0,
             panning=0.0, pitch_bend=0.0, duration=0.25),
        Note(time=0.25, note=64, velocity=0.7, sound="note.harp", channel=0, layer=1,
             panning=0.0, pitch_bend=0.0, duration=0.25),
        Note(time=0.0, note=36, velocity=0.9, sound="note.bd", channel=9, layer=2),
        Note(time=0.5, note=67, velocity=0.6, sound="note.flute", channel=1, layer=3,
             panning=0.5, pitch_bend=0.5, duration=0.5),
    ]
    return song


def _to(fmt, song):
    """Song -> 字节。"""
    return ezbuild.registry.get_writer(fmt)().render(song)


def _from(fmt, data):
    """字节 -> Song。"""
    return ezbuild.registry.get_reader(fmt)().read(data)


def _key(note):
    return (round(note.time, 3), note.note, note.sound,
            round(note.velocity, 2), round(note.panning, 2), round(note.pitch_bend, 2))


# ---------------------------------------------------------------------------
# MIDI round-trip
# ---------------------------------------------------------------------------

class TestMidRoundTrip:
    def test_single_note(self):
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0)]
        got = _from("mid", _to("mid", song))
        assert len(got.notes) == 1
        n = got.notes[0]
        assert n.note == 60
        assert n.time == pytest.approx(0.0, abs=1 / 960)
        assert n.velocity == pytest.approx(0.8, abs=0.01)
        assert n.sound == "note.harp"
        assert n.channel == 0

    def test_melody(self):
        song = _make_song()
        got = _from("mid", _to("mid", song))
        keys = sorted(_key(n) for n in got.notes)
        assert len(keys) == 4
        # 时间、音高、音色、力度都保留
        assert (0.0, 60, "note.harp", 0.8, 0.0, 0.0) in keys
        assert (0.25, 64, "note.harp", 0.7, 0.0, 0.0) in keys
        assert (0.0, 36, "note.bd", 0.9, 0.0, 0.0) in keys
        assert (0.5, 67, "note.flute", 0.6, 0.5, 0.5) in keys

    def test_chord_same_tick(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0),
            Note(time=0.0, note=64, velocity=0.8, sound="note.harp", channel=0),
            Note(time=0.0, note=67, velocity=0.8, sound="note.harp", channel=0),
        ]
        got = _from("mid", _to("mid", song))
        notes_at_0 = [n.note for n in got.notes if abs(n.time) < 1e-6]
        assert sorted(notes_at_0) == [60, 64, 67]

    def test_duration_preserved(self):
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.harp",
                           channel=0, duration=0.5)]
        got = _from("mid", _to("mid", song))
        assert got.notes[0].duration == pytest.approx(0.5, abs=1 / 960)

    def test_off_before_on_same_tick(self):
        """同 tick 先 off 后 on：A 在 0.25s 结束、B 在 0.25s 开始，两音都保留。"""
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0, duration=0.25),
            Note(time=0.25, note=60, velocity=0.8, sound="note.harp", channel=0, duration=0.25),
        ]
        got = _from("mid", _to("mid", song))
        assert len(got.notes) == 2
        starts = sorted(round(n.time, 3) for n in got.notes)
        assert starts == [0.0, 0.25]

    def test_drums_channel_9(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=36, velocity=0.9, sound="note.bd", channel=9),
            Note(time=0.0, note=38, velocity=0.8, sound="note.snare", channel=9),
        ]
        got = _from("mid", _to("mid", song))
        by_note = {n.note: n for n in got.notes}
        assert by_note[36].channel == 9 and by_note[36].sound == "note.bd"
        assert by_note[38].channel == 9 and by_note[38].sound == "note.snare"

    def test_low_velocity_clamped_not_zero(self):
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.001, sound="note.harp", channel=0)]
        got = _from("mid", _to("mid", song))
        assert len(got.notes) == 1  # 0 力度会被当成 note_off，不能出现
        assert got.notes[0].velocity > 0

    def test_empty_song(self):
        song = Song(tempo=120.0)
        data = _to("mid", song)
        assert data[:4] == b"MThd"
        assert _from("mid", data).notes == []

    def test_note_127(self):
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=127, velocity=0.8, sound="note.harp", channel=0)]
        got = _from("mid", _to("mid", song))
        assert got.notes[0].note == 127


# ---------------------------------------------------------------------------
# MIDI 乐器映射（参照 midi-mcstructure_next，经 mid round-trip 验证）
# ---------------------------------------------------------------------------

class TestMidiInstrumentMapping:
    def test_program_trumpet_family(self):
        """铜管组不再塌成 flute：56 小号 / 57 长号 / 60 圆号各归其位。"""
        cases = {
            "note.trumpet": 56,
            "note.trumpet_weathered": 57,
            "note.trumpet_exposed": 60,
        }
        for sound, _prog in cases.items():
            song = Song(tempo=120.0)
            song.notes = [Note(time=0.0, note=60, velocity=0.8, sound=sound, channel=1)]
            got = _from("mid", _to("mid", song))
            assert got.notes[0].sound == sound

    def test_program_parts_carry_volume(self):
        """读取 MIDI 后音符带乐器部件（响度补偿挂在上面对，velocity 本身不改）。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.trumpet", channel=1)]
        got = _from("mid", _to("mid", song))
        parts = got.notes[0].instrument_parts
        assert len(parts) == 1
        assert parts[0].sound == "note.trumpet"
        assert parts[0].volume == pytest.approx(0.7)
        # MIDI 力度字节量化有 ±0.01 容差；且 velocity 未被响度补偿污染
        assert got.notes[0].velocity == pytest.approx(0.8, abs=0.01)

    def test_unknown_program_defaults_harp(self):
        from ezbuild.song import program_to_parts

        assert program_to_parts(3)[0].sound == "note.harp"       # 未映射程序
        assert program_to_parts(200)[0].sound == "note.harp"     # 越界
        assert program_to_parts(0)[0].volume == pytest.approx(0.53)  # harp 响度补偿

    def test_percussion_pedal_cymbal(self):
        """踩镲踏板 44 -> random.fizz + 半音偏移 31（区别于其它镲片）。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=44, velocity=0.8, sound="random.fizz", channel=9)]
        got = _from("mid", _to("mid", song))
        n = got.notes[0]
        assert n.sound == "random.fizz"
        assert n.channel == 9
        assert n.instrument_parts[0].pitch_offset == pytest.approx(31)

    def test_percussion_core_drum_preserved(self):
        """混合映射：核心鼓约定 36 底鼓 / 38 军鼓保留，不被参考表覆盖。"""
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=36, velocity=0.8, sound="note.bd", channel=9),
            Note(time=0.0, note=38, velocity=0.8, sound="note.snare", channel=9),
        ]
        got = _from("mid", _to("mid", song))
        by_note = {n.note: n for n in got.notes}
        assert by_note[36].sound == "note.bd"
        assert by_note[38].sound == "note.snare"

    def test_percussion_cowbell(self):
        """牛铃 56 -> note.cow_bell（参考映射，不再塌成 snare）。"""
        from ezbuild.song import percussion_note_to_parts

        parts = percussion_note_to_parts(56)
        assert parts[0].sound == "note.cow_bell"


class TestMidiPitchBendRange:
    """RPN 0/1 弯音范围解析：bend 量 = raw/8192 × 实际范围（默认 ±2）。"""

    def test_default_range_two_semitones(self):
        """无 RPN：满弯 (+8191) = 默认 ±2 半音。"""
        got = _from("mid", _minimal_bend_smf())
        assert got.notes[0].pitch_bend == pytest.approx(2.0, abs=0.01)

    def test_rpn_range_twelve_semitones(self):
        """RPN 0 + CC6=12（吉他常用 ±12）：满弯 = 12 半音。"""
        got = _from("mid", _minimal_bend_smf(bend_range_semis=12))
        assert got.notes[0].pitch_bend == pytest.approx(12.0, abs=0.01)

    def test_rpn_cents_fractional(self):
        """CC38 百分音叠加：CC6=2 + CC38=64 -> ±2.5 半音。"""
        got = _from("mid", _minimal_bend_smf(bend_range_semis=2, cents=64))
        assert got.notes[0].pitch_bend == pytest.approx(2.5, abs=0.01)


# ---------------------------------------------------------------------------
# NBS round-trip
# ---------------------------------------------------------------------------

class TestNbsRoundTrip:
    def test_single_note(self):
        song = Song(name="test", tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.harp",
                           channel=0, layer=0)]
        got = _from("nbs", _to("nbs", song))
        assert got.name == "test"
        assert len(got.notes) == 1
        n = got.notes[0]
        assert n.note == 60
        assert n.time == pytest.approx(0.0, abs=1e-4)
        assert n.velocity == pytest.approx(0.8, abs=0.01)
        assert n.sound == "note.harp"
        assert n.layer == 0

    def test_multi_layer(self):
        song = _make_song()
        got = _from("nbs", _to("nbs", song))
        assert len(got.notes) == 4
        by_layer = {n.layer: (n.time, n.note, n.sound) for n in got.notes}
        assert by_layer[0] == (0.0, 60, "note.harp")
        assert by_layer[1] == (0.25, 64, "note.harp")
        assert by_layer[2] == (0.0, 36, "note.bd")
        assert by_layer[3] == (0.5, 67, "note.flute")

    def test_pan_and_bend(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0,
                 layer=0, panning=0.5, pitch_bend=0.5),
        ]
        got = _from("nbs", _to("nbs", song))
        n = got.notes[0]
        assert n.panning == pytest.approx(0.5, abs=0.01)
        assert n.pitch_bend == pytest.approx(0.5, abs=0.01)

    def test_empty_song(self):
        song = Song(tempo=120.0)
        data = _to("nbs", song)
        assert _from("nbs", data).notes == []

    def test_low_velocity_not_100(self):
        """NBS 力度字节 0 会被读作 100，写入端必须钳制到 >=1。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.01, sound="note.harp", channel=0)]
        got = _from("nbs", _to("nbs", song))
        assert got.notes[0].velocity == pytest.approx(0.01, abs=0.005)

    def test_chord_distinct_layers(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0, layer=0),
            Note(time=0.0, note=64, velocity=0.8, sound="note.harp", channel=0, layer=1),
            Note(time=0.0, note=67, velocity=0.8, sound="note.harp", channel=0, layer=2),
        ]
        got = _from("nbs", _to("nbs", song))
        assert sorted(n.note for n in got.notes) == [60, 64, 67]
        assert len({n.layer for n in got.notes}) == 3

    def test_layer_volume_folded(self):
        """层音量折入音符力度：手写 NBS（层音量 50 + 音符力度 100 -> 0.5）。"""
        data = _build_nbs_with_layer_volume(volume=50, note_vel=100)
        got = _from("nbs", data)
        assert got.notes[0].velocity == pytest.approx(0.5, abs=0.01)

    def test_high_key_clamped(self):
        """外来 NBS key 112 -> MIDI note 133 -> 钳制到 127。"""
        data = _build_nbs_with_layer_volume(volume=100, note_vel=100, key=112)
        got = _from("nbs", data)
        assert got.notes[0].note == 127

    def test_note_127_roundtrip_raw(self):
        """fold_range=False：note 127 -> key 106 -> 读回 127（原始音高）。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=127, velocity=0.8, sound="note.harp", channel=0)]
        data = ezbuild.registry.get_writer("nbs")(fold_range=False).render(song)
        got = _from("nbs", data)
        assert got.notes[0].note == 127

    def test_out_of_range_folded(self):
        """默认八度折叠：note 127 -> key 106 -> 折叠到 46 -> 可播放。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=127, velocity=0.8, sound="note.harp", channel=0)]
        got = _from("nbs", _to("nbs", song))
        key = got.notes[0].note - 21
        assert 33 <= key <= 57
        assert got.notes[0].note == 67  # 106 -> 46 -> note 67

    def test_in_range_not_folded(self):
        """范围内的音符不折叠：note 60 -> key 39 保持不变。"""
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0)]
        got = _from("nbs", _to("nbs", song))
        assert got.notes[0].note == 60

    def test_foreign_drum_normalized(self):
        """外来 NBS 的 bd（任意 key）读回为标准 GM 底鼓音符 36 / 通道 9。"""
        data = _build_nbs_with_layer_volume(volume=100, note_vel=100, key=39, instrument=2)
        got = _from("nbs", data)
        assert got.notes[0].sound == "note.bd"
        assert got.notes[0].channel == 9
        assert got.notes[0].note == 36


# ---------------------------------------------------------------------------
# 字节级校验
# ---------------------------------------------------------------------------

class TestByteLevel:
    def test_midi_header_and_tempo(self):
        song = _make_song(tempo=120.0)
        data = _to("mid", song)
        assert data[:4] == b"MThd"
        fmt, ntrks, division = struct.unpack(">HHH", data[8:14])
        assert (fmt, ntrks, division) == (0, 1, 480)
        assert b"MTrk" in data
        assert b"\xff\x51\x03\x07\xa1\x20" in data  # 120bpm = 500000µs

    def test_nbs_header_layout(self):
        song = Song(name="t", tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0, layer=0)]
        data = _to("nbs", song)

        o = 0
        assert struct.unpack_from("<H", data, o)[0] == 0; o += 2   # song_length=0
        assert data[o] == 5; o += 1                                # version (当前 v5)
        assert data[o] == 16; o += 1                               # default_instruments
        assert struct.unpack_from("<H", data, o)[0] == 1; o += 2   # song_length
        assert struct.unpack_from("<H", data, o)[0] == 1; o += 2   # song_layers
        for _ in range(4):                                         # 4 个字符串
            ln = struct.unpack_from("<I", data, o)[0]
            o += 4 + ln
        assert struct.unpack_from("<H", data, o)[0] == 800; o += 2  # tempo=8.0 -> 800
        for _ in range(3):                                          # auto_save×3
            o += 1
        for _ in range(5):                                          # 5 个 INT
            struct.unpack_from("<I", data, o)[0]
            o += 4
        ln = struct.unpack_from("<I", data, o)[0]; o += 4 + ln     # song_origin
        o += 1 + 1 + 2                                              # loop/max_loop/loop_start
        # 音符：tick 跳（SHORT 1）、层跳（SHORT 1）、instrument/key/vel/pan(4 BYTE)+pitch(SSHORT)
        assert struct.unpack_from("<H", data, o)[0] == 1; o += 2
        assert struct.unpack_from("<H", data, o)[0] == 1; o += 2
        assert struct.unpack_from("<B", data, o)[0] == 0; o += 1   # instrument=0 (harp)
        assert struct.unpack_from("<B", data, o)[0] == 39; o += 1  # key = 60-21
        assert struct.unpack_from("<B", data, o)[0] == 80; o += 1  # velocity
        assert struct.unpack_from("<B", data, o)[0] == 100; o += 1 # panning 居中
        assert struct.unpack_from("<h", data, o)[0] == 0; o += 2   # pitch


# ---------------------------------------------------------------------------
# 跨格式
# ---------------------------------------------------------------------------

class TestCrossFormat:
    def test_mid_to_nbs_to_mid(self):
        song = _make_song(tempo=120.0)
        after_nbs = _from("nbs", _to("nbs", _from("mid", _to("mid", song))))
        assert sorted(_key(n) for n in after_nbs.notes) == sorted(_key(n) for n in song.notes)

    def test_nbs_to_mid_to_nbs(self):
        song = _make_song(tempo=120.0)
        after_mid = _from("mid", _to("mid", _from("nbs", _to("nbs", song))))
        assert sorted(_key(n) for n in after_mid.notes) == sorted(_key(n) for n in song.notes)

    def test_tempo_preserved(self):
        song = _make_song(tempo=150.0)
        got = _from("nbs", _to("nbs", _from("mid", _to("mid", song))))
        assert got.tempo == pytest.approx(150.0)

    def test_long_song_nbs_ticks_in_range(self):
        song = Song(tempo=120.0)
        for i in range(200):
            song.notes.append(Note(time=i * 0.25, note=60, velocity=0.8,
                                   sound="note.harp", channel=0, layer=0))
        data = _to("nbs", song)  # 不应抛错
        got = _from("nbs", data)
        assert len(got.notes) == 200


# ---------------------------------------------------------------------------
# 分发与类型
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_registered(self):
        assert "mid" in ezbuild.registry.list_readers()
        assert "mid" in ezbuild.registry.list_writers()
        assert "nbs" in ezbuild.registry.list_readers()
        assert "nbs" in ezbuild.registry.list_writers()

    def test_detect_by_extension(self, tmp_path):
        p = tmp_path / "a.mid"
        p.write_bytes(_to("mid", Song()))
        assert ezbuild.registry.format_for_path(p) == "mid"
        p2 = tmp_path / "b.nbs"
        p2.write_bytes(_to("nbs", Song()))
        assert ezbuild.registry.format_for_path(p2) == "nbs"

    def test_convert_read_returns_song(self, tmp_path):
        p = tmp_path / "a.nbs"
        p.write_bytes(_to("nbs", _make_song()))
        model = ezbuild.convert_read(p)
        assert isinstance(model, Song)

    def test_writer_rejects_building(self):
        from ezbuild.model import Block

        building = Building()
        building.blocks.append(Block(x=0, y=0, z=0, name="stone"))
        with pytest.raises(TypeError):
            ezbuild.registry.get_writer("mid")().render(building)
        with pytest.raises(TypeError):
            ezbuild.registry.get_writer("nbs")().render(building)


# ---------------------------------------------------------------------------
# 手写最小 NBS（自定义层音量），用于验证层音量折入
# ---------------------------------------------------------------------------

def _build_nbs_with_layer_volume(volume=50, note_vel=100, key=45, instrument=0):
    """构造一个最小 v4 NBS：单音符 (tick0, layer0)，层音量/键位/乐器可自定义。"""
    def s(text):
        raw = text.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    out = bytearray()
    out += struct.pack("<H", 0)          # song_length=0 -> 有 version
    out += bytes([4])                    # version
    out += bytes([10])                   # default_instruments
    out += struct.pack("<H", 1)          # song_length
    out += struct.pack("<H", 1)          # song_layers
    out += s("") + s("") + s("") + s("")  # 4 字符串
    out += struct.pack("<H", 1000)       # tempo 10.0
    out += bytes([1, 10, 4])             # auto_save / duration / time_signature
    out += struct.pack("<I", 0) * 5      # 5 个 INT
    out += s("")                         # song_origin
    out += bytes([0, 0])                 # loop / max_loop_count
    out += struct.pack("<H", 0)          # loop_start
    out += struct.pack("<H", 1)          # tick 跳 -> tick 0
    out += struct.pack("<H", 1)          # 层跳 -> layer 0
    out += bytes([instrument, key, note_vel, 100])  # instrument, key, vel, panning
    out += struct.pack("<h", 0)          # pitch
    out += struct.pack("<H", 0)          # 结束层跳
    out += struct.pack("<H", 0)          # 结束 tick 跳
    out += s("")                         # 层名
    out += bytes([0])                    # lock
    out += bytes([volume])               # 层音量
    out += bytes([100])                  # 层 panning
    out += bytes([0])                    # 乐器数 0
    return bytes(out)


def _minimal_bend_smf(note=60, bend_range_semis=None, cents=None, bend_up=True):
    """构造最小格式0 MIDI：可选 RPN 0 弯音范围 + 一个满弯音 note。

    - ``bend_range_semis``: 设 RPN 0 的 CC6（弯音范围整数半音）；
    - ``cents``: 再设 CC38（百分音）；
    - ``bend_up``: 满弯（+8191）；False 为直音（0）。
    """
    def vlq(v):
        out = bytearray([v & 0x7F])
        v >>= 7
        while v:
            out.append(0x80 | (v & 0x7F))
            v >>= 7
        out.reverse()
        return bytes(out)

    ev = bytearray(b"\x00\xff\x51\x03\x07\xa1\x20")  # tempo 120bpm
    if bend_range_semis is not None:
        ev += b"\x00\xb0\x65\x00"                     # CC101 RPN MSB = 0
        ev += b"\x00\xb0\x64\x00"                     # CC100 RPN LSB = 0
        ev += b"\x00\xb0\x06" + bytes([bend_range_semis])  # CC6 范围整数半音
    if cents is not None:
        ev += b"\x00\xb0\x26" + bytes([cents])        # CC38 百分音
    if bend_up:
        ev += b"\x00\xe0\x7f\x7f"                     # 满弯 +8191
    else:
        ev += b"\x00\xe0\x00\x40"                     # 直音 8192
    ev += b"\x00\x90" + bytes([note, 100])            # note_on
    ev += vlq(480) + b"\x80" + bytes([note, 0])       # note_off @ tick 480
    ev += b"\x00\xff\x2f\x00"                         # 轨结束
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
            + b"MTrk" + struct.pack(">I", len(ev)) + bytes(ev))
