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


def _same_content(a: Building, b: Building) -> bool:
    """比较两座建筑的内容（方块/命令方块按位置与值，忽略调色板顺序）。"""
    def blockset(bl):
        return {(x.x, x.y, x.z): (x.name, tuple(sorted(x.states.items()))) for x in bl.blocks}

    def cbset(bl):
        return {(c.x, c.y, c.z): (c.mode, c.command, c.conditional, c.needs_redstone)
                for c in bl.command_blocks}

    return blockset(a) == blockset(b) and cbset(a) == cbset(b)


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

    def test_classic_schematic_streaming(self, tmp_path):
        """经典 .schematic 也走流式，输出与非流式一致。"""
        from .fixtures import make_schematic_bytes
        from ezbuild.streaming import schematic_to_txt

        b = ezbuild.convert_read_from(make_schematic_bytes(), "schematic")
        non_stream = ezbuild.registry.get_writer("txt")().render(b)
        out = tmp_path / "c.txt"
        schematic_to_txt(make_schematic_bytes(), out)
        assert out.read_text(encoding="utf-8") == non_stream

    def test_streaming_cmd_json(self, tmp_path):
        from ezbuild.streaming import schematic_to_cmd_json

        data = ezbuild.registry.get_writer("schem")().render(_make_building())
        building = ezbuild.convert_read_from(data, "schem")
        non = ezbuild.registry.get_writer("cmd_json")().render(building)
        out = tmp_path / "c.json"
        schematic_to_cmd_json(data, out)
        assert out.read_text(encoding="utf-8") == non

    def test_streaming_ibi_content(self, tmp_path):
        """流式 ibi 解码内容与非流式一致（setblock 行集合 + JSON 段；
        行顺序可能不同，位置显式）。"""
        from ezbuild.streaming import schematic_to_ibi
        from ezbuild.writers.ibi import decode_ibi

        data = ezbuild.registry.get_writer("schem")().render(_make_building())
        building = ezbuild.convert_read_from(data, "schem")
        non = ezbuild.registry.get_writer("ibi")().render(building)
        out = tmp_path / "c.ibi"
        schematic_to_ibi(data, out)
        stream = open(out, "rb").read()
        txt_a, json_a = decode_ibi(non)
        txt_b, json_b = decode_ibi(stream)
        assert sorted(txt_a.splitlines()) == sorted(txt_b.splitlines())
        assert json_a == json_b

    def test_streaming_mcstructure_content(self, tmp_path):
        """流式 mcstructure 读回内容与非流式一致。"""
        from ezbuild.streaming import schematic_to_mcstructure

        data = ezbuild.registry.get_writer("schem")().render(_make_building())
        building = ezbuild.convert_read_from(data, "schem")
        non = ezbuild.convert_read_from(
            ezbuild.registry.get_writer("mcstructure")().render(building), "mcstructure")
        out = tmp_path / "c.mcstructure"
        schematic_to_mcstructure(data, out)
        stream = ezbuild.convert_read(out)
        assert _same_content(stream, non)

    def test_streaming_schem_content(self, tmp_path):
        """流式 schem 读回内容与非流式一致。"""
        from ezbuild.streaming import schematic_to_schem

        data = ezbuild.registry.get_writer("schem")().render(_make_building())
        building = ezbuild.convert_read_from(data, "schem")
        non = ezbuild.convert_read_from(
            ezbuild.registry.get_writer("schem")().render(building), "schem")
        out = tmp_path / "c.schem"
        schematic_to_schem(data, out)
        stream = ezbuild.convert_read(out)
        assert _same_content(stream, non)


class TestMcStructureStreaming:
    """mcstructure → 分区块 txt 的流式转换（不建 Building 模型）。"""

    @staticmethod
    def _mcstructure_bytes(b: Building) -> bytes:
        return ezbuild.registry.get_writer("mcstructure")().render(b)

    @pytest.mark.parametrize("kwargs", [
        {},                                     # 分区块默认
        {"strip_states": frozenset()},          # --all-states
        {"fill_merge": False},                  # --nofill
        {"fill_merge": False, "strip_states": frozenset()},
    ])
    def test_streaming_matches_non_streaming(self, tmp_path, kwargs):
        """mcstructure → txt 流式输出与非流式逐字节一致。"""
        from ezbuild.streaming import schematic_to_txt

        b = _make_building()
        data = self._mcstructure_bytes(b)
        bld = ezbuild.convert_read_from(data, "mcstructure")
        non_stream = ezbuild.registry.get_writer("txt")(progress=False, **kwargs).render(bld)

        out = tmp_path / "s.txt"
        schematic_to_txt(data, out, progress=False, **kwargs)
        assert out.read_text(encoding="utf-8") == non_stream

    def test_streaming_large_matches(self, tmp_path):
        """跨多区块 + 大调色板：区块内迭代顺序须与读取器（x 主序）一致。"""
        import random

        from ezbuild.streaming import schematic_to_txt

        rng = random.Random(7)
        names = [f"block_{i}" for i in range(40)]  # 调色板 > 16
        b = Building()
        for _ in range(3000):
            b.blocks.append(
                Block(x=rng.randint(-20, 60), y=rng.randint(0, 40), z=rng.randint(-20, 60),
                      name=rng.choice(names),
                      states={"facing_direction": rng.randint(0, 5)} if rng.random() < 0.4 else {})
            )
        data = self._mcstructure_bytes(b)
        bld = ezbuild.convert_read_from(data, "mcstructure")
        for kwargs in ({}, {"strip_states": frozenset()}, {"fill_merge": False}):
            non = ezbuild.registry.get_writer("txt")(progress=False, **kwargs).render(bld)
            out = tmp_path / "s.txt"
            schematic_to_txt(data, out, progress=False, **kwargs)
            assert out.read_text(encoding="utf-8") == non

    def test_streaming_cmd_json_matches(self, tmp_path):
        """mcstructure 命令方块（block_position_data）流式 cmd_json 与非流式一致。"""
        from ezbuild.streaming import schematic_to_cmd_json

        data = self._mcstructure_bytes(_make_building())
        bld = ezbuild.convert_read_from(data, "mcstructure")
        non = ezbuild.registry.get_writer("cmd_json")().render(bld)
        out = tmp_path / "c.json"
        schematic_to_cmd_json(data, out)
        assert out.read_text(encoding="utf-8") == non

    def test_schematic_source_supports_mcstructure(self):
        """SchematicSource 直接读 mcstructure（尺寸/方块/命令方块）。"""
        from ezbuild.streaming import SchematicSource

        data = self._mcstructure_bytes(_make_building())
        src = SchematicSource(data)
        assert src.size == (3, 1, 1)
        blocks = {(x, y, z): name for x, y, z, name, _s in src.iter_all_blocks()}
        assert blocks.get((0, 0, 0)) == "stone"
        assert {(c.x, c.y, c.z) for c in src.command_blocks()} == {(1, 0, 0), (2, 0, 0)}
