"""Reader 单元测试：把合成建筑文件解析为中立模型。"""

import io
import json

import pytest

import ezbuild
from ezbuild.model import MODE_CHAIN, MODE_IMPULSE, MODE_REPEAT
from .fixtures import (
    make_bdx_bytes,
    make_mcstructure_bytes,
    make_schematic_bytes,
)


class TestMCStructureReader:
    @pytest.fixture()
    def building(self):
        return ezbuild.convert_read_from(make_mcstructure_bytes(), "mcstructure")

    def test_parse_blocks(self, building):
        assert building.size == (3, 3, 1)
        assert building.block_count == 5
        names = {(b.x, b.y, b.z): b.name for b in building.blocks}
        assert names[(0, 0, 0)] == "command_block"
        assert names[(2, 0, 0)] == "stone"
        assert names[(0, 2, 0)] == "red_wool"

    def test_block_states_converted(self, building):
        red_wool = next(b for b in building.blocks if b.name == "red_wool")
        assert red_wool.states == {"color": "red"}
        chain = next(b for b in building.blocks if b.name == "chain_command_block")
        assert chain.states == {"conditional_bit": 1}

    def test_command_blocks(self, building):
        assert building.command_block_count == 3
        by_mode = {cb.mode: cb for cb in building.command_blocks}
        assert by_mode[MODE_IMPULSE].command == "say hello"
        assert by_mode[MODE_IMPULSE].custom_name == "入口"
        assert by_mode[MODE_IMPULSE].needs_redstone is False  # auto=1
        assert by_mode[MODE_CHAIN].command == "execute as @a run say hi"
        assert by_mode[MODE_CHAIN].tick_delay == 4
        assert by_mode[MODE_CHAIN].conditional is True
        assert by_mode[MODE_CHAIN].needs_redstone is True  # auto=0
        assert by_mode[MODE_REPEAT].tick_delay == 10


class TestBDXReader:
    @pytest.fixture()
    def building(self):
        return ezbuild.convert_read_from(make_bdx_bytes(), "bdx")

    def test_parse_author_and_blocks(self, building):
        assert building.author == "TestAuthor"
        assert building.source_format == "bdx"
        # stone 被规范化为不带前缀名
        assert building.blocks[0].name == "stone"
        assert (building.blocks[0].x, building.blocks[0].y, building.blocks[0].z) == (0, 0, 0)

    def test_command_blocks(self, building):
        assert building.command_block_count == 2
        cbs = sorted(building.command_blocks, key=lambda cb: cb.x)
        impulse, chain = cbs
        # BDX mode 约定：0=脉冲 2=连锁（待反编译确认，见 readers/bdx.py 顶部注释）
        assert impulse.mode == MODE_IMPULSE
        assert impulse.command == "say impulse"
        assert impulse.custom_name == "impulseCB"
        assert impulse.tick_delay == 2
        assert impulse.needs_redstone is False
        assert chain.mode == MODE_CHAIN
        assert chain.command == "say chain"
        assert chain.conditional is True
        assert chain.block_id == "chain_command_block"

    def test_accepts_bytes_source(self):
        # 字节无法按扩展名推断，须显式指定格式
        b = ezbuild.convert_read_from(make_bdx_bytes(), "bdx")
        assert b.author == "TestAuthor"
        with pytest.raises(ValueError):
            ezbuild.convert_read(make_bdx_bytes())


class TestSchematicReader:
    """经典 Java .schematic（gzip + 大端 NBT + ID 映射表）。"""

    @pytest.fixture()
    def building(self):
        return ezbuild.convert_read_from(make_schematic_bytes(), "schematic")

    def test_parse_blocks(self, building):
        assert building.size == (3, 3, 1)
        names = {(b.x, b.y, b.z): b.name for b in building.blocks}
        assert names[(0, 0, 0)] == "stone"
        assert names[(1, 1, 0)] == "command_block"
        assert names[(2, 2, 0)] == "chain_command_block"

    def test_block_states_from_table(self, building):
        stone = next(b for b in building.blocks if b.name == "stone")
        assert stone.states == {"stone_type": "stone"}
        cb = next(b for b in building.blocks if b.name == "command_block")
        # data=4 → 朝西，无条件位
        assert cb.states["facing_direction"] == 4
        assert cb.states["conditional_bit"] is False

    def test_command_blocks_from_tile_entities(self, building):
        assert building.command_block_count == 2
        cbs = sorted(building.command_blocks, key=lambda cb: cb.x)
        impulse, chain = cbs
        assert impulse.mode == MODE_IMPULSE
        assert impulse.command == "say hello"
        assert impulse.custom_name == "入口"
        assert impulse.needs_redstone is False  # auto=1
        assert chain.mode == MODE_CHAIN
        assert chain.command == "execute as @a run say hi"
        assert chain.tick_delay == 4
        assert chain.conditional is True

    def test_gzip_detection(self, building):
        assert building.source_format == "schematic"


class TestTxtReader:
    """setblock/fill 指令文本 → Building。"""

    SAMPLE = "\n".join(
        [
            "tp ~0 ~ ~0",
            'setblock ~0 ~0 ~0 stone ["pillar_axis"=0]',
            "setblock ~1 ~2 ~3 command_block",
            "fill ~2 ~0 ~0 ~3 ~0 ~0 red_wool",
            "fill ~4 ~0 ~0 ~4 ~0 ~0 stone",
        ]
    )

    def test_parse_setblock(self):
        b = ezbuild.convert_read_from(self.SAMPLE.encode(), "txt")
        by_pos = {(blk.x, blk.y, blk.z): blk for blk in b.blocks}
        assert by_pos[(0, 0, 0)].name == "stone"
        assert by_pos[(0, 0, 0)].states == {"pillar_axis": 0}
        assert by_pos[(1, 2, 3)].name == "command_block"

    def test_fill_expansion(self):
        b = ezbuild.convert_read_from(self.SAMPLE.encode(), "txt")
        # fill 展开：2×1×1 red_wool + 1×1×1 stone
        red = [blk for blk in b.blocks if blk.name == "red_wool"]
        assert len(red) == 2
        assert {(blk.x, blk.y, blk.z) for blk in red} == {(2, 0, 0), (3, 0, 0)}

    def test_ignores_tp_and_size(self):
        b = ezbuild.convert_read_from(self.SAMPLE.encode(), "txt")
        # 5 个方块：stone + command_block + 2 red_wool + 1 stone
        assert b.block_count == 5
        assert b.size == (5, 3, 4)  # x 0..4, y 0..2, z 0..3

    def test_chunked_txt_roundtrip(self):
        """未分区块 txt → txt 分区块，fill 被合并。"""
        b = ezbuild.convert_read_from(self.SAMPLE.encode(), "txt")
        text = ezbuild.registry.get_writer("txt")().render(b)
        lines = text.splitlines()
        assert lines[0].startswith("tp ")
        assert any(l.startswith("fill") for l in lines)  # red_wool 区域合并成 fill
        assert any("red_wool" in l for l in lines)


class TestIbiReader:
    """IBI 导入包 → Building（拆分 txt 段 + 命令方块 JSON 段）。"""

    def _pack(self):
        """用 bdx 样例打包一个 IBI 字节。"""
        building = ezbuild.convert_read_from(make_bdx_bytes(), "bdx")
        return ezbuild.registry.get_writer("ibi")().render(building)

    def test_roundtrip(self):
        building = ezbuild.convert_read_from(self._pack(), "ibi")
        assert building.source_format == "ibi"
        # txt 段：1 stone + 2 命令方块外壳
        assert building.block_count == 3
        # JSON 段：2 个命令方块
        assert building.command_block_count == 2
        cbs = sorted(building.command_blocks, key=lambda cb: cb.x)
        assert cbs[0].command == "say impulse"
        assert cbs[0].mode == MODE_IMPULSE
        assert cbs[1].command == "say chain"
        assert cbs[1].mode == MODE_CHAIN
        assert cbs[1].conditional is True

    def test_to_cmd_json(self):
        building = ezbuild.convert_read_from(self._pack(), "ibi")
        entries = json.loads(
            ezbuild.registry.get_writer("cmd_json")().render(building)
        )
        assert len(entries) == 2
        assert entries[0]["Command"] == "say impulse"


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        ezbuild.convert_read_from(b"\x00\x01", "nope")


def test_format_detection_by_extension(tmp_path):
    p = tmp_path / "a.mcstructure"
    p.write_bytes(make_mcstructure_bytes())
    assert ezbuild.registry.format_for_path(p) == "mcstructure"
