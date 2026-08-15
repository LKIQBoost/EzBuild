"""Sponge .schem 读取/写出单元测试（Java 版结构文件）。"""

import gzip
import io

import pytest

import ezbuild
from ezbuild.model import MODE_CHAIN, MODE_IMPULSE, Block, Building, CommandBlock
from ezbuild.readers.schem import _parse_blockstate
from ezbuild.utils import bedrock_to_java_states, java_to_bedrock_states


def _make_building() -> Building:
    b = Building()
    b.blocks.append(Block(x=0, y=0, z=0, name="stone", states={"waterlogged": False}))
    b.blocks.append(Block(x=1, y=0, z=0, name="chain_command_block",
                          states={"facing_direction": 2, "conditional_bit": 1}))
    b.blocks.append(Block(x=2, y=0, z=0, name="command_block",
                          states={"facing_direction": 3}))
    b.command_blocks.append(CommandBlock(x=1, y=0, z=0, mode=MODE_CHAIN,
                                         command="say hi", conditional=True,
                                         needs_redstone=False))
    b.command_blocks.append(CommandBlock(x=2, y=0, z=0, mode=MODE_IMPULSE,
                                         command="say start", needs_redstone=True))
    return b


class TestBlockstateParsing:
    def test_plain(self):
        assert _parse_blockstate("minecraft:stone") == ("stone", {})

    def test_with_state(self):
        name, states = _parse_blockstate("minecraft:stone[waterlogged=false]")
        assert name == "stone"
        assert states == {"waterlogged": False}

    def test_command_block_states(self):
        name, states = _parse_blockstate(
            "minecraft:chain_command_block[facing=north,conditional=true]"
        )
        assert name == "chain_command_block"
        assert states == {"facing_direction": 2, "conditional_bit": 1}

    def test_java_to_bedrock(self):
        assert java_to_bedrock_states({"facing": "east"}) == {"facing_direction": 5}
        assert java_to_bedrock_states({"conditional": False}) == {"conditional_bit": 0}

    def test_bedrock_to_java(self):
        assert bedrock_to_java_states({"facing_direction": 2}) == {"facing": "north"}
        assert bedrock_to_java_states({"conditional_bit": 1}) == {"conditional": True}


class TestSchemRoundTrip:
    def test_roundtrip(self):
        b = _make_building()
        data = ezbuild.registry.get_writer("schem")().render(b)
        assert data[:2] == b"\x1f\x8b"  # gzip 魔数

        b2 = ezbuild.convert_read_from(data, "schem")
        assert b2.block_count == 3
        assert b2.command_block_count == 2

        by_pos = {(blk.x, blk.y, blk.z): blk for blk in b2.blocks}
        assert by_pos[(0, 0, 0)].name == "stone"
        assert by_pos[(0, 0, 0)].states == {"waterlogged": False}
        assert by_pos[(1, 0, 0)].name == "chain_command_block"
        assert by_pos[(1, 0, 0)].states == {"facing_direction": 2, "conditional_bit": 1}

        cbs = sorted(b2.command_blocks, key=lambda c: c.x)
        assert cbs[0].mode == MODE_CHAIN
        assert cbs[0].command == "say hi"
        assert cbs[0].conditional is True
        assert cbs[0].needs_redstone is False
        assert cbs[1].mode == MODE_IMPULSE
        assert cbs[1].command == "say start"
        assert cbs[1].needs_redstone is True

    def test_offset_negative_coords(self):
        b = Building()
        b.blocks.append(Block(x=-5, y=64, z=-3, name="stone"))
        b.blocks.append(Block(x=0, y=64, z=0, name="command_block",
                              states={"facing_direction": 3}))
        b.command_blocks.append(CommandBlock(x=0, y=64, z=0, mode=MODE_IMPULSE,
                                             command="say hi"))
        data = ezbuild.registry.get_writer("schem")().render(b)
        b2 = ezbuild.convert_read_from(data, "schem")
        pos = {(blk.x, blk.y, blk.z) for blk in b2.blocks}
        assert pos == {(-5, 64, -3), (0, 64, 0)}
        assert b2.command_blocks[0].command == "say hi"

    def test_empty_building(self):
        data = ezbuild.registry.get_writer("schem")().render(Building())
        assert data[:2] == b"\x1f\x8b"
        b = ezbuild.convert_read_from(data, "schem")
        assert b.block_count == 0

    def test_palette_limit(self):
        b = Building()
        for i in range(300):
            b.blocks.append(Block(x=i, y=0, z=0, name=f"block_{i}"))
        with pytest.raises(ValueError):
            ezbuild.registry.get_writer("schem")().render(b)

    def test_gzip_readable(self):
        """输出是 gzip 压缩的大端 NBT，可被 nbtlib 直接读。"""
        b = _make_building()
        data = ezbuild.registry.get_writer("schem")().render(b)
        raw = gzip.decompress(data)
        import nbtlib

        st = nbtlib.File.parse(io.BytesIO(raw), byteorder="big")
        assert int(st["Version"]) == 2
        assert list(int(v) for v in st["Offset"]) == [0, 0, 0]
        assert "minecraft:chain_command_block[facing=north,conditional=true]" in st["Palette"]


class TestCompatibility:
    def test_classic_schematic_reader_unaffected(self):
        """经典 .schematic 仍按原格式读取（sponge 分支不干扰）。"""
        assert ezbuild.registry.format_for_path("a.schem") == "schem"
        assert ezbuild.registry.format_for_path("a.schematic") == "schematic"

    def test_registered(self):
        assert "schem" in ezbuild.registry.list_readers()
        assert "schem" in ezbuild.registry.list_writers()


class TestStreaming:
    def test_streaming_matches_non_streaming(self, tmp_path):
        """schem -> txt 流式输出与非流式逐字节一致。"""
        from ezbuild.streaming import schem_to_txt

        b = _make_building()
        data = ezbuild.registry.get_writer("schem")().render(b)
        non_stream = ezbuild.registry.get_writer("txt")().render(b)

        out = tmp_path / "s.txt"
        schem_to_txt(data, out)
        assert out.read_text(encoding="utf-8") == non_stream

    def test_high_palette_index_roundtrip(self):
        """调色板索引 >127 的方块不再丢失（无符号字节解码）。"""
        b = Building()
        for i in range(150):  # 150 种不同方块，最后一种索引 149 > 127
            b.blocks.append(Block(x=i, y=0, z=0, name=f"block_{i}"))
        data = ezbuild.registry.get_writer("schem")().render(b)
        b2 = ezbuild.convert_read_from(data, "schem")
        names = {blk.name for blk in b2.blocks}
        assert len(names) == 150
        assert "block_0" in names and "block_149" in names

    def test_streaming_bytes_source(self, tmp_path):
        from ezbuild.streaming import schem_to_txt

        data = ezbuild.registry.get_writer("schem")().render(_make_building())
        out = tmp_path / "s.txt"
        schem_to_txt(data, out)
        text = out.read_text(encoding="utf-8")
        assert "setblock" in text or "fill" in text
