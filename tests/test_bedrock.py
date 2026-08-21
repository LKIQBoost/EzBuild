"""Bedrock 世界导出测试：合成 Bedrock 世界（db/*.ldb）→ BedrockWorldSource / world_to_*。"""

from __future__ import annotations

import io
import json

import pytest

from ezbuild.bedrock import BedrockWorldSource
from ezbuild.world import world_to_cmd_json, world_to_ibi, world_to_mcstructure, world_to_schem, world_to_txt
from ezbuild.writers.ibi import decode_ibi

from .bedrock_fixture import make_bedrock_world


@pytest.fixture
def bworld(tmp_path):
    return make_bedrock_world(tmp_path)


def _blocks_set(ws):
    return {(x, y, z): (name, state_str) for x, y, z, name, state_str in ws.iter_all_blocks()}


# ---------------------------------------------------------------------------
# BedrockWorldSource
# ---------------------------------------------------------------------------
def test_iter_all_blocks_positive(bworld):
    ws = BedrockWorldSource(bworld, (0, 0, 0, 15, 31, 15))
    blocks = _blocks_set(ws)
    assert blocks[(3, 1, 4)][0] == "stone"
    assert blocks[(10, 2, 9)] == ("red_wool", '"color"="red"')
    assert blocks[(2, 17, 3)][0] == "command_block"
    assert (1, -1, 2) not in blocks
    assert len(blocks) == 3
    ws.close()


def test_iter_all_blocks_cross_chunk(bworld):
    ws = BedrockWorldSource(bworld, (0, 0, 0, 5, 15, 20))
    blocks = _blocks_set(ws)
    assert (3, 1, 4) in blocks
    assert (5, 1, 20) in blocks
    assert (10, 2, 9) not in blocks
    assert (2, 17, 3) not in blocks
    ws.close()


def test_negative_coords(bworld):
    ws = BedrockWorldSource(bworld, (-16, 0, 0, -1, 15, 15))
    blocks = _blocks_set(ws)
    assert blocks[(-2, 1, 3)][0] == "stone"
    assert len(blocks) == 1
    ws.close()


def test_negative_y(bworld):
    ws = BedrockWorldSource(bworld, (0, -16, 0, 15, -1, 15))
    blocks = _blocks_set(ws)
    assert blocks[(1, -1, 2)][0] == "stone"
    assert len(blocks) == 1
    ws.close()


def test_command_blocks(bworld):
    ws = BedrockWorldSource(bworld, (0, 0, 0, 15, 31, 15))
    cbs = list(ws.command_blocks())
    assert len(cbs) == 1
    cb = cbs[0]
    assert (cb.x, cb.y, cb.z) == (2, 17, 3)
    assert cb.mode == 0  # impulse
    assert cb.command == "say hi"
    assert cb.conditional is True  # conditional_bit=1
    assert cb.needs_redstone is False  # auto=1
    ws.close()


def test_get_region(bworld):
    ws = BedrockWorldSource(bworld, (0, 0, 0, 15, 31, 15))
    region = ws.get_region(0, 15, 0, 15)
    assert region[(3, 1, 4)][0] == "stone"
    assert region[(2, 17, 3)][0] == "command_block"
    ws.close()


def test_invalid_world(tmp_path):
    from ezbuild.world import open_world_source

    with pytest.raises(ValueError, match="不是有效的世界文件夹"):
        open_world_source(tmp_path / "nope", (0, 0, 0, 15, 15, 15))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def test_world_to_txt(bworld, tmp_path):
    out = tmp_path / "out.txt"
    world_to_txt(bworld, out, (0, 0, 0, 15, 31, 15))
    text = out.read_text(encoding="utf-8")
    assert "setblock ~3 ~1 ~4 stone" in text
    assert 'setblock ~10 ~2 ~9 red_wool ["color"="red"]' in text
    assert "setblock ~2 ~17 ~3 command_block" in text
    assert "tp ~0 ~ ~0" in text


def test_world_to_cmd_json(bworld, tmp_path):
    out = tmp_path / "out.json"
    world_to_cmd_json(bworld, out, (0, 0, 0, 15, 31, 15))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["posx"] == "~2"
    assert data[0]["Command"] == "say hi"


def test_world_to_ibi(bworld, tmp_path):
    out = tmp_path / "out.ibi"
    world_to_ibi(bworld, out, (0, 0, 0, 15, 31, 15))
    data = out.read_bytes()
    assert data[:9] == b"IBImport "
    txt, entries = decode_ibi(data)
    assert "setblock ~3 ~1 ~4 stone" in txt
    assert len(entries) == 1


def test_world_to_mcstructure(bworld, tmp_path):
    out = tmp_path / "out.mcstructure"
    world_to_mcstructure(bworld, out, (0, 0, 0, 15, 31, 15))
    import nbtlib

    root = nbtlib.File.parse(io.BytesIO(out.read_bytes()), byteorder="little")
    size = [int(v) for v in root["size"]]
    origin = [int(v) for v in root["structure_world_origin"]]
    assert size == [9, 17, 7]
    assert origin == [2, 1, 3]
    palette = root["structure"]["palette"]["default"]["block_palette"]
    names = {str(p["name"]).replace("minecraft:", "") for p in palette}
    assert {"stone", "red_wool", "command_block"} <= names


def test_world_to_schem(bworld, tmp_path):
    out = tmp_path / "out.schem"
    world_to_schem(bworld, out, (0, 0, 0, 15, 31, 15))
    import gzip
    import nbtlib

    root = nbtlib.File.parse(io.BytesIO(gzip.decompress(out.read_bytes())), byteorder="big")
    assert [int(v) for v in root["Offset"]] == [2, 1, 3]
    palette = {str(k) for k in root["Palette"]}
    assert "minecraft:stone" in palette
    assert "minecraft:red_wool[color=red]" in palette
    assert "minecraft:command_block[facing=north,conditional=true]" in palette
