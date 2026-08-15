"""Writer 单元测试：验证输出格式与 lemon.json 参考格式一致。"""

import base64
import io
import json

import pytest

import ezbuild
from ezbuild.writers.ibi import decode_ibi
from .fixtures import make_bdx_bytes, make_mcstructure_bytes

# lemon.json 每条记录的字段顺序（参考 D:\\下载\\[zx-093]lemon.json）
LEMON_KEYS = [
    "posx", "posy", "posz", "BlockMode", "name",
    "Command", "TickDelay", "IsRedStoneMode", "IsConditional",
]


def _mc_building():
    return ezbuild.convert_read_from(make_mcstructure_bytes(), "mcstructure")


class TestCommandBlockJsonWriter:
    @pytest.fixture()
    def entries(self):
        building = _mc_building()
        writer = ezbuild.registry.get_writer("cmd_json")()
        return json.loads(writer.render(building))

    def test_field_order_matches_lemon(self, entries):
        for entry in entries:
            assert list(entry.keys()) == LEMON_KEYS

    def test_relative_coordinates(self, entries):
        assert all(
            e["posx"].startswith("~") and e["posy"].startswith("~") and e["posz"].startswith("~")
            for e in entries
        )
        assert entries[0]["posx"] == "~0"

    def test_values(self, entries):
        by_pos = {(e["posx"], e["posy"], e["posz"]): e for e in entries}
        impulse = by_pos[("~0", "~0", "~0")]
        assert impulse["BlockMode"] == "command_block"
        assert impulse["Command"] == "say hello"
        assert impulse["name"] == "入口"  # ensure_ascii=False 保留中文
        assert impulse["IsRedStoneMode"] == 0  # auto=1 → 不需要红石
        assert impulse["IsConditional"] == 0

        chain = by_pos[("~1", "~1", "~0")]
        assert chain["BlockMode"] == "chain_command_block"
        assert chain["TickDelay"] == 4
        assert chain["IsRedStoneMode"] == 1
        assert chain["IsConditional"] == 1

    def test_ensure_ascii_false(self, entries):
        raw = json.dumps(entries, ensure_ascii=False)
        assert "入口" in raw

    def test_write_to_file(self, tmp_path):
        out = tmp_path / "cb.json"
        building = _mc_building()
        ezbuild.convert_write(building, out, "cmd_json")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data) == 3


class TestSetblockTxtWriter:
    def test_render(self):
        building = _mc_building()
        text = ezbuild.registry.get_writer("setblock_txt")().render(building)
        lines = text.splitlines()
        assert len(lines) == 5
        assert lines[0] == "setblock ~0 ~0 ~0 command_block"
        assert 'setblock ~0 ~2 ~0 red_wool ["color"="red"]' in lines
        assert 'setblock ~1 ~1 ~0 chain_command_block ["conditional_bit"=true]' in lines


def test_bdx_setblock_txt_includes_command_blocks():
    """回归：BDX 的命令方块也要出现在 setblock_txt 中（之前丢失导致输出为空）。"""
    building = ezbuild.convert_read_from(make_bdx_bytes(), "bdx")
    text = ezbuild.registry.get_writer("setblock_txt")().render(building)
    lines = text.splitlines()
    # 1 个 stone + 2 个命令方块
    assert len(lines) == 3
    assert "setblock ~0 ~0 ~0 stone" in lines
    # data=4 → 朝西；data=3 → 朝南
    assert 'setblock ~1 ~0 ~0 command_block ["facing_direction"=4,"conditional_bit"=false]' in lines
    assert 'setblock ~2 ~0 ~0 chain_command_block ["facing_direction"=3,"conditional_bit"=false]' in lines


class TestTxtWriter:
    """分区块优化 txt（操作名 txt）。"""

    def test_registered_and_render(self):
        assert "txt" in ezbuild.registry.list_writers()
        building = _mc_building()
        text = ezbuild.registry.get_writer("txt")().render(building)
        # 小建筑只有一个区块
        lines = text.splitlines()
        assert lines[0] == "tp ~0 ~ ~0"
        assert any("chain_command_block" in l for l in lines)
        # 坐标按 y 排序（0 层在 1 层前）
        assert lines[1] == "setblock ~0 ~0 ~0 command_block"

    def test_chunking_and_fill_merge(self):
        from ezbuild.writers.txt import chunk_optimize

        # 20×3×20 stone 实体区域（跨 x=16 区块边界）+ 一个红羊毛单方块
        blocks = {}
        for x in range(20):
            for y in range(3):
                for z in range(20):
                    blocks[(x, y, z)] = ("stone", "")
        blocks[(19, 0, 19)] = ("red_wool", "")

        text = chunk_optimize(blocks, chunk_size=16)
        lines = text.splitlines()

        # 4 个区块 → 4 条 tp，坐标相对化
        tps = [l for l in lines if l.startswith("tp")]
        assert len(tps) == 4
        assert tps == ["tp ~0 ~ ~0", "tp ~0 ~ ~16", "tp ~16 ~ ~0", "tp ~0 ~ ~-16"]

        # 连续 stone 被合并成 fill
        fills = [l for l in lines if l.startswith("fill")]
        assert len(fills) >= 4
        # 红羊毛保留为 setblock
        assert any("red_wool" in l for l in lines)
        # 输出中不应出现超出区块范围的绝对坐标（都是 ~ 相对）
        assert all(l.startswith(("tp", "fill", "setblock")) for l in lines)


class TestStripStates:
    """setblock 输出默认省略所有 *_bit 开关状态（conditional_bit 保留）。"""

    def _building(self):
        from ezbuild.model import Block, Building

        b = Building()
        b.blocks.append(Block(0, 0, 0, "barrel", {"facing_direction": 3, "open_bit": 0}))
        b.blocks.append(Block(1, 0, 0, "hopper", {"facing_direction": 0, "toggle_bit": 0}))
        b.blocks.append(Block(2, 0, 0, "stone", {"powered_bit": 1, "attached_bit": 0}))
        return b

    def test_default_strips_all_bit_states(self):
        text = ezbuild.registry.get_writer("setblock_txt")().render(self._building())
        assert 'barrel ["facing_direction"=3]' in text
        assert 'hopper ["facing_direction"=0]' in text
        assert 'stone []' in text or "setblock ~2 ~0 ~0 stone" in text
        assert "open_bit" not in text and "toggle_bit" not in text
        assert "powered_bit" not in text and "attached_bit" not in text

    def test_all_states_kept(self):
        from ezbuild.writers.setblock_txt import SetblockTxtWriter

        text = SetblockTxtWriter(strip_states=frozenset()).render(self._building())
        assert 'barrel ["facing_direction"=3,"open_bit"=0]' in text
        assert 'hopper ["facing_direction"=0,"toggle_bit"=0]' in text
        assert '["powered_bit"=1,"attached_bit"=0]' in text

    def test_conditional_bit_not_stripped(self):
        """conditional_bit（命令方块）是语义状态，不能被剥离。"""
        from ezbuild.model import Block, Building

        b = Building()
        b.blocks.append(Block(0, 0, 0, "chain_command_block", {"conditional_bit": 1}))
        text = ezbuild.registry.get_writer("setblock_txt")().render(b)
        assert '["conditional_bit"=true]' in text


class TestIbiWriter:
    def test_header_and_roundtrip(self):
        building = _mc_building()
        ibi = ezbuild.registry.get_writer("ibi")().render(building)
        assert ibi[:9] == b"IBImport "
        txt, entries = decode_ibi(ibi)
        assert len(txt.splitlines()) == 5
        assert len(entries) == 3

    def test_json_schema(self):
        building = _mc_building()
        ibi = ezbuild.registry.get_writer("ibi")().render(building)
        _, entries = decode_ibi(ibi)
        first = entries[0]
        assert set(first.keys()) == {
            "posX", "posY", "posZ", "CommandMessage", "Commandtitle",
            "mode", "isTime", "Conditional", "isRedstone",
        }
        assert first["posX"] == "~0"
        assert first["mode"] == 0  # 脉冲
        # CommandMessage 是 base64
        assert base64.b64decode(first["CommandMessage"]).decode() == "say hello"


def test_write_infers_format_from_output_extension(tmp_path):
    """未指定格式时，按输出文件扩展名推断 Writer。"""
    out = tmp_path / "x.json"
    building = _mc_building()
    ezbuild.convert_write(building, out)
    assert json.loads(out.read_text(encoding="utf-8"))


def test_bdx_to_cmd_json(tmp_path):
    """端到端：bdx → cmd_json。"""
    out = tmp_path / "out.json"
    building = ezbuild.convert_read_from(make_bdx_bytes(), "bdx")
    ezbuild.convert_write(building, out, "cmd_json")
    entries = json.loads(out.read_text(encoding="utf-8"))
    assert len(entries) == 2
    by_pos = {(e["posx"], e["posy"], e["posz"]): e for e in entries}
    assert by_pos[("~1", "~0", "~0")]["Command"] == "say impulse"
    assert by_pos[("~2", "~0", "~0")]["BlockMode"] == "chain_command_block"
