"""世界导出测试：合成 Anvil 世界 → WorldSource / world_to_*。"""

from __future__ import annotations

import io
import json

import pytest

from ezbuild.world import (
    Box,
    WorldSource,
    world_to_cmd_json,
    world_to_ibi,
    world_to_mcstructure,
    world_to_schem,
    world_to_txt,
)
from ezbuild.writers.ibi import decode_ibi

from .world_fixture import make_world, make_world_many_types


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def _blocks_set(ws: WorldSource):
    return {(x, y, z): (name, state_str) for x, y, z, name, state_str in ws.iter_all_blocks()}


# ---------------------------------------------------------------------------
# WorldSource 基础
# ---------------------------------------------------------------------------
def test_iter_all_blocks_positive(world):
    ws = WorldSource(world, (0, 0, 0, 15, 31, 15))
    blocks = _blocks_set(ws)
    assert blocks[(3, 1, 4)][0] == "stone"
    assert blocks[(10, 2, 9)] == ("red_wool", '"color"="red"')
    assert blocks[(2, 17, 3)][0] == "command_block"
    assert (1, -1, 2) not in blocks  # y=-1 超出包围盒
    assert len(blocks) == 3
    ws.close()


def test_iter_all_blocks_cross_chunk(world):
    # 横跨 z 区块边界 (0,0)/(0,1)
    ws = WorldSource(world, (0, 0, 0, 5, 15, 20))
    blocks = _blocks_set(ws)
    assert (3, 1, 4) in blocks
    assert (5, 1, 20) in blocks
    assert (10, 2, 9) not in blocks  # x 超界
    assert (2, 17, 3) not in blocks  # y 超界
    ws.close()


def test_negative_coords(world):
    # 负区块：chunk (-1,0)，stone @ (-2,1,3)
    ws = WorldSource(world, (-16, 0, 0, -1, 15, 15))
    blocks = _blocks_set(ws)
    assert blocks[(-2, 1, 3)][0] == "stone"
    assert len(blocks) == 1
    ws.close()


def test_negative_y(world):
    # 负 y：section Y=-1，stone @ (1,-1,2)
    ws = WorldSource(world, (0, -16, 0, 15, -1, 15))
    blocks = _blocks_set(ws)
    assert blocks[(1, -1, 2)][0] == "stone"
    assert len(blocks) == 1
    ws.close()


def test_get_region(world):
    ws = WorldSource(world, (0, 0, 0, 15, 31, 15))
    region = ws.get_region(0, 15, 0, 15)  # chunk (0,0)
    # region 键 = (列内 x, 绝对 y, 列内 z)
    assert region[(3, 1, 4)][0] == "stone"
    assert region[(2, 17, 3)][0] == "command_block"
    ws.close()


def test_get_region_outside_box(world):
    ws = WorldSource(world, (0, 0, 0, 15, 15, 15))
    # box y 只到 15，section Y=1（y 16-31）不包含
    region = ws.get_region(0, 15, 0, 15)
    assert (2, 17, 3) not in region
    ws.close()


def test_command_blocks(world):
    ws = WorldSource(world, (0, 0, 0, 15, 31, 15))
    cbs = list(ws.command_blocks())
    assert len(cbs) == 1
    cb = cbs[0]
    assert (cb.x, cb.y, cb.z) == (2, 17, 3)
    assert cb.mode == 0  # impulse
    assert cb.command == "say hi"
    assert cb.conditional is True  # Properties.conditional=true
    assert cb.needs_redstone is False  # auto=1
    ws.close()


def test_box_normalization(world):
    # 坐标可反着给，自动归一化
    ws = WorldSource(world, (15, 31, 15, 0, 0, 0))
    assert ws.box == Box(0, 0, 0, 15, 31, 15)
    ws.close()


def test_invalid_world(tmp_path):
    with pytest.raises(ValueError, match="region"):
        WorldSource(tmp_path / "not_a_world", (0, 0, 0, 15, 15, 15))


def test_no_region_intersects(world):
    # 包围盒在没有任何 region 的区域（r.10.10 不存在）
    with pytest.raises(ValueError, match="没有找到任何区块"):
        WorldSource(world, (10000, 0, 10000, 10100, 15, 10100))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def test_world_to_txt(world, tmp_path):
    out = tmp_path / "out.txt"
    world_to_txt(world, out, (0, 0, 0, 15, 31, 15))
    text = out.read_text(encoding="utf-8")
    assert "setblock ~3 ~1 ~4 stone" in text
    assert 'setblock ~10 ~2 ~9 red_wool ["color"="red"]' in text
    assert "setblock ~2 ~17 ~3 command_block" in text
    assert "tp ~0 ~ ~0" in text  # 分区块 tp 导航


def test_world_to_txt_nofill(world, tmp_path):
    out = tmp_path / "out.txt"
    world_to_txt(world, out, (0, 0, 0, 15, 31, 15), fill_merge=False)
    text = out.read_text(encoding="utf-8")
    assert "setblock ~3 ~1 ~4 stone" in text
    assert "tp" not in text  # 纯 setblock


def test_world_to_cmd_json(world, tmp_path):
    out = tmp_path / "out.json"
    world_to_cmd_json(world, out, (0, 0, 0, 15, 31, 15))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["posx"] == "~2"
    assert data[0]["Command"] == "say hi"
    assert data[0]["BlockMode"] == "command_block"


def test_world_to_ibi(world, tmp_path):
    out = tmp_path / "out.ibi"
    world_to_ibi(world, out, (0, 0, 0, 15, 31, 15))
    data = out.read_bytes()
    assert data[:9] == b"IBImport "
    txt, entries = decode_ibi(data)
    assert "setblock ~3 ~1 ~4 stone" in txt
    assert len(entries) == 1
    assert entries[0]["CommandMessage"]  # base64


def test_world_to_mcstructure(world, tmp_path):
    out = tmp_path / "out.mcstructure"
    world_to_mcstructure(world, out, (0, 0, 0, 15, 31, 15))
    import nbtlib

    root = nbtlib.File.parse(io.BytesIO(out.read_bytes()), byteorder="little")
    size = [int(v) for v in root["size"]]
    origin = [int(v) for v in root["structure_world_origin"]]
    # 实际方块范围：x 2..10, y 1..17, z 3..9 → 9×17×7
    assert size == [9, 17, 7]
    # 结构原点 = 裁剪后最小世界坐标
    assert origin == [2, 1, 3]
    indices = [int(v) for v in root["structure"]["block_indices"][0]]
    # 调色板含 command_block / stone / red_wool
    palette = root["structure"]["palette"]["default"]["block_palette"]
    names = {str(p["name"]).replace("minecraft:", "") for p in palette}
    assert {"stone", "red_wool", "command_block"} <= names


def test_world_to_schem(world, tmp_path):
    out = tmp_path / "out.schem"
    world_to_schem(world, out, (0, 0, 0, 15, 31, 15))
    import gzip
    import nbtlib

    root = nbtlib.File.parse(
        io.BytesIO(gzip.decompress(out.read_bytes())), byteorder="big"
    )
    assert [int(v) for v in root["Offset"]] == [2, 1, 3]
    palette = {str(k) for k in root["Palette"]}
    assert "minecraft:stone" in palette
    assert "minecraft:red_wool[color=red]" in palette
    assert "minecraft:command_block[facing=north,conditional=true]" in palette


def test_world_to_schem_split(tmp_path):
    """区域方块 >256 种时 -s 自动拆分，每个分块调色板 ≤256。"""
    import gzip
    import nbtlib

    world = make_world_many_types(tmp_path)
    out = tmp_path / "s.schem"
    files = world_to_schem(str(world), out, (0, 0, 0, 31, 1, 31), split=True)
    assert isinstance(files, list)
    assert len(files) >= 2  # 260 种 > 256 → 至少拆 2 份
    all_palette = set()
    for f in files:
        root = nbtlib.File.parse(
            io.BytesIO(gzip.decompress(open(f, "rb").read())), byteorder="big"
        )
        assert len(root["Palette"]) <= 256
        all_palette.update(str(k) for k in root["Palette"])
    assert len(all_palette) >= 260  # 各分块拼起来含全部 260 种


def test_world_to_schem_no_split_small(world, tmp_path):
    """方块种类不多时 -s 返回单个文件。"""
    out = tmp_path / "s.schem"
    files = world_to_schem(str(world), out, (0, 0, 0, 15, 31, 15), split=True)
    assert isinstance(files, list)
    assert len(files) == 1
