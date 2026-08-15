"""音乐 → 建筑：Song → 命令方块音乐机 → mcstructure round-trip。"""

import json

import pytest

import ezbuild
from ezbuild.model import MODE_CHAIN, MODE_IMPULSE
from ezbuild.music_builder import song_to_building
from ezbuild.song import Note, Song


def _make_song(n=10) -> Song:
    song = Song(name="test", tempo=120.0)
    song.notes = [
        Note(time=i * 0.25, note=60 + i % 8, velocity=0.8,
             sound="note.harp", channel=0, duration=0.2)
        for i in range(n)
    ]
    return song


def _facing_of(building, x, y, z):
    for blk in building.blocks:
        if (blk.x, blk.y, blk.z) == (x, y, z):
            return blk.states["facing_direction"]
    raise KeyError((x, y, z))


def _chain_continuous(building) -> bool:
    """每块命令方块朝向是否指向链中下一块。"""
    cbs = building.command_blocks
    for i in range(len(cbs) - 1):
        cx, cy, cz = cbs[i].x, cbs[i].y, cbs[i].z
        nx, ny, nz = cbs[i + 1].x, cbs[i + 1].y, cbs[i + 1].z
        f = _facing_of(building, cx, cy, cz)
        dx = dy = dz = 0
        if f == 1:
            dy = 1
        elif f == 0:
            dy = -1
        elif f == 3:
            dz = 1
        elif f == 2:
            dz = -1
        elif f == 5:
            dx = 1
        elif f == 4:
            dx = -1
        if (cx + dx, cy + dy, cz + dz) != (nx, ny, nz):
            return False
    return True


class TestSongToBuilding:
    def test_basic_machine(self):
        b = song_to_building(_make_song(5))
        assert b.command_block_count == 5
        assert b.block_count == 5

    def test_first_impulse_rest_chain(self):
        b = song_to_building(_make_song(5))
        assert b.command_blocks[0].mode == MODE_IMPULSE
        assert b.command_blocks[0].needs_redstone is True
        for cb in b.command_blocks[1:]:
            assert cb.mode == MODE_CHAIN
            assert cb.needs_redstone is False

    def test_serpentine_continuous(self):
        b = song_to_building(_make_song(40))  # 2 行多
        assert _chain_continuous(b)

    def test_delay_accumulates_to_last_tick(self):
        song = _make_song(4)
        # 0, 0.25, 0.5, 0.75 秒 -> tick 0,5,10,15
        b = song_to_building(song)
        assert b.command_blocks[0].tick_delay == 0
        assert [c.tick_delay for c in b.command_blocks] == [0, 5, 5, 5]
        assert sum(c.tick_delay for c in b.command_blocks) == 15

    def test_same_tick_zero_delay(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp", channel=0),
            Note(time=0.0, note=64, velocity=0.8, sound="note.harp", channel=0),
            Note(time=0.25, note=67, velocity=0.8, sound="note.harp", channel=0),
        ]
        b = song_to_building(song)
        assert [c.tick_delay for c in b.command_blocks] == [0, 0, 5]

    def test_pitch_mapping(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=66, velocity=0.8, sound="note.harp"),   # 1.000
            Note(time=0.05, note=54, velocity=0.8, sound="note.harp"),  # 0.500
            Note(time=0.10, note=90, velocity=0.8, sound="note.harp"),  # 4 -> 钳 2.000
            Note(time=0.15, note=36, velocity=0.9, sound="note.bd", channel=9),  # 鼓 -> 1.000
        ]
        b = song_to_building(song)
        cmds = [c.command for c in b.command_blocks]
        assert "0.80 1.000 0.80" in cmds[0]
        assert "0.80 0.500 0.80" in cmds[1]
        assert "0.80 2.000 0.80" in cmds[2]
        assert "0.90 1.000 0.90" in cmds[3]

    def test_velocity_clamped(self):
        song = Song(tempo=120.0)
        song.notes = [Note(time=0.0, note=60, velocity=1.5, sound="note.harp")]
        b = song_to_building(song)
        assert "1.00" in b.command_blocks[0].command

    def test_panning_position(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.8, sound="note.harp"),        # ~ ~ ~
            Note(time=0.05, note=60, velocity=0.8, sound="note.harp", panning=0.5),
            Note(time=0.10, note=60, velocity=0.8, sound="note.harp", panning=-0.5),
        ]
        b = song_to_building(song)
        cmds = [c.command for c in b.command_blocks]
        assert "playsound note.harp @s ~ ~ ~" in cmds[0]
        # 声像公式与 midilib 一致：pan>0 -> ^0.00 ^ ^+pan；pan<0 -> ^-pan ^ ^0.00
        assert "playsound note.harp @s ^0.00 ^ ^0.50" in cmds[1]
        assert "playsound note.harp @s ^0.50 ^ ^0.00" in cmds[2]

    def test_java_edition(self):
        song = _make_song(1)
        b = song_to_building(song, edition="java")
        assert "playsound note.harp record @s" in b.command_blocks[0].command

    def test_invalid_edition(self):
        with pytest.raises(ValueError):
            song_to_building(_make_song(1), edition="bogus")

    def test_empty_song(self):
        b = song_to_building(Song())
        assert b.command_block_count == 0
        assert b.block_count == 0

    def test_velocity_zero_skipped(self):
        song = Song(tempo=120.0)
        song.notes = [
            Note(time=0.0, note=60, velocity=0.0, sound="note.harp"),
            Note(time=0.25, note=64, velocity=0.8, sound="note.harp"),
        ]
        b = song_to_building(song)
        assert b.command_block_count == 1

    def test_large_song_auto_deepen(self):
        """超过 16*320 = 5120 音符时自动加深。"""
        song = _make_song(6000)
        b = song_to_building(song)
        assert b.command_block_count == 6000
        assert b.size[1] <= 320  # 高度不超过上限
        assert _chain_continuous(b)


class TestMcStructureRoundTrip:
    def test_roundtrip(self):
        b = song_to_building(_make_song(40))
        data = ezbuild.registry.get_writer("mcstructure")().render(b)
        b2 = ezbuild.convert_read_from(data, "mcstructure")

        assert b2.command_block_count == b.command_block_count
        orig = {(c.x, c.y, c.z): c for c in b.command_blocks}
        got = {(c.x, c.y, c.z): c for c in b2.command_blocks}
        assert set(got) == set(orig)
        for pos, cb in orig.items():
            g = got[pos]
            assert g.command == cb.command
            assert g.tick_delay == cb.tick_delay
            assert g.mode == cb.mode
            assert g.needs_redstone == cb.needs_redstone

        # 朝向一致
        fac1 = {(blk.x, blk.y, blk.z): blk.states["facing_direction"] for blk in b.blocks}
        fac2 = {(blk.x, blk.y, blk.z): blk.states.get("facing_direction") for blk in b2.blocks}
        assert fac1 == fac2

    def test_empty_song_mcstructure(self):
        data = ezbuild.registry.get_writer("mcstructure")().render(song_to_building(Song()))
        assert data[:4] == b"\x0a\x00\x00\x03"  # 未压缩 NBT
        b = ezbuild.convert_read_from(data, "mcstructure")
        assert b.block_count == 0

    def test_uncompressed_nbt_header(self):
        data = ezbuild.registry.get_writer("mcstructure")().render(song_to_building(_make_song(3)))
        assert data[:4] == b"\x0a\x00\x00\x03"


class TestBridge:
    def test_convert_write_song_to_cmd_json(self, tmp_path):
        song = _make_song(3)
        p = tmp_path / "out.json"
        ezbuild.convert_write(song, p, "cmd_json")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert len(data) == 3
        assert data[0]["BlockMode"] == "command_block"
        assert "playsound" in data[0]["Command"]

    def test_convert_write_song_to_mcstructure(self, tmp_path):
        song = _make_song(3)
        p = tmp_path / "out.mcstructure"
        ezbuild.convert_write(song, p, "mcstructure")
        b = ezbuild.convert_read(p)
        assert b.command_block_count == 3

    def test_convert_write_song_stays_song(self, tmp_path):
        song = _make_song(3)
        p = tmp_path / "out.mid"
        ezbuild.convert_write(song, p, "mid")
        assert isinstance(ezbuild.convert_read(p), Song)

    def test_mcstructure_registered(self):
        assert "mcstructure" in ezbuild.registry.list_writers()
