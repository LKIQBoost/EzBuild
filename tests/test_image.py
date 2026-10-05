"""图片输入（image Reader）单元测试。

用内存生成的 PNG 验证：像素→方块映射、-w/-h 尺寸与宽高比、朝向、抖动。
未安装 Pillow 时整组跳过。
"""

import io

import pytest

import ezbuild

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def make_png(size, colors):
    """生成一张 PNG：colors 为按行优先排列的 (r,g,b) 列表。"""
    w, h = size
    img = Image.new("RGB", (w, h))
    img.putdata(colors)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestImageReader:
    def test_basic_mapping(self):
        # 2×2：白、黑 / 红、蓝
        px = [(255, 255, 255), (0, 0, 0), (255, 0, 0), (0, 0, 255)]
        b = ezbuild.convert_read_from(
            make_png((2, 2), px), "image", dither="nearest"
        )
        assert b.source_format == "image"
        assert b.size == (2, 1, 2)  # 默认水平：X=宽, Y=1, Z=高
        by_pos = {(blk.x, blk.y, blk.z): blk.name for blk in b.blocks}
        assert by_pos[(0, 0, 0)] == "white_wool"
        assert by_pos[(1, 0, 0)] == "black_wool"
        assert by_pos[(0, 0, 1)] == "tnt"  # 纯红最近的代表色是 TNT
        assert by_pos[(1, 0, 1)] == "blue_wool"

    def test_vertical_plane(self):
        px = [(255, 255, 255), (0, 0, 0), (255, 0, 0), (0, 0, 255)]
        b = ezbuild.convert_read_from(
            make_png((2, 2), px), "image", plane="vertical", dither="nearest"
        )
        assert b.size == (2, 2, 1)  # X=宽, Y=高, Z=1
        by_pos = {(blk.x, blk.y, blk.z): blk.name for blk in b.blocks}
        # 图片顶部 → 墙上方（y 大）
        assert by_pos[(0, 1, 0)] == "white_wool"
        assert by_pos[(0, 0, 0)] == "tnt"  # 左下的纯红

    def test_width_only_keeps_aspect(self):
        # 4×2 原图，宽=8 → 高=4
        px = [(0, 0, 0)] * 8
        b = ezbuild.convert_read_from(
            make_png((4, 2), px), "image", width=8
        )
        assert b.size == (8, 1, 4)

    def test_height_only_keeps_aspect(self):
        px = [(0, 0, 0)] * 8
        b = ezbuild.convert_read_from(
            make_png((4, 2), px), "image", height=4
        )
        assert b.size == (8, 1, 4)

    def test_both_dimensions(self):
        px = [(0, 0, 0)] * 8
        b = ezbuild.convert_read_from(
            make_png((4, 2), px), "image", width=10, height=3
        )
        assert b.size == (10, 1, 3)
        assert b.block_count == 30

    def test_default_uses_original_size(self):
        px = [(0, 0, 0)] * 8
        b = ezbuild.convert_read_from(make_png((4, 2), px), "image")
        assert b.size == (4, 1, 2)

    @pytest.mark.parametrize("dither", ["nearest", "ordered", "floyd"])
    def test_dither_modes(self, dither):
        px = [(r, (r * 7) % 256, (r * 13) % 256) for r in range(0, 256, 8)] * 4
        b = ezbuild.convert_read_from(
            make_png((32, 4), px), "image", dither=dither
        )
        assert b.block_count == 128

    def test_bumpy_runs(self):
        px = [(128, 128, 128)] * 4
        b = ezbuild.convert_read_from(
            make_png((2, 2), px), "image", bumpy=True
        )
        assert b.block_count == 4


def test_extension_detection(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(make_png((1, 1), [(255, 255, 255)]))
    assert ezbuild.registry.format_for_path(p) == "image"


def test_invalid_options():
    with pytest.raises(ValueError):
        ezbuild.convert_read_from(b"\x00", "image", dither="nope")
    with pytest.raises(ValueError):
        ezbuild.convert_read_from(b"\x00", "image", plane="nope")
