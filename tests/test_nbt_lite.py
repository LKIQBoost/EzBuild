"""nbt_lite 极简 NBT 解析器测试（大数组走 numpy 批量读）。"""

import io

import nbtlib
import numpy as np

from ezbuild import nbt_lite


def _build() -> bytes:
    root = nbtlib.Compound(
        {
            "name": nbtlib.String("hi"),
            "count": nbtlib.Int(7),
            "flag": nbtlib.Byte(1),
            "neg": nbtlib.Short(-2),
            "vals": nbtlib.List([nbtlib.Int(v) for v in (1, -1, 123456)]),
            "bools": nbtlib.List([nbtlib.Byte(v) for v in (0, 1)]),
            "bytes": nbtlib.ByteArray([1, 2, 3]),
            "ints": nbtlib.IntArray([10, 20, 30]),
            "longs": nbtlib.LongArray([1, 2]),
            "nested": nbtlib.Compound({"x": nbtlib.Double(1.5)}),
            "strs": nbtlib.List([nbtlib.String("a"), nbtlib.String("b")]),
            "empty": nbtlib.List([]),
        }
    )
    buf = io.BytesIO()
    nbtlib.File(root, byteorder="little").write(buf, byteorder="little")
    return buf.getvalue()


class TestParseNbt:
    def test_scalars_and_nested(self):
        d = nbt_lite.parse_nbt(_build(), "little")
        assert d["name"] == "hi"
        assert d["count"] == 7
        assert d["flag"] == 1
        assert d["neg"] == -2  # Short 有符号
        assert d["nested"]["x"] == 1.5

    def test_fixed_lists_become_ndarray(self):
        """定长基本类型列表 → numpy 数组（避免逐元素 Python 对象）。"""
        d = nbt_lite.parse_nbt(_build(), "little")
        assert isinstance(d["vals"], np.ndarray) and d["vals"].tolist() == [1, -1, 123456]
        assert isinstance(d["bools"], np.ndarray) and d["bools"].tolist() == [0, 1]

    def test_arrays(self):
        d = nbt_lite.parse_nbt(_build(), "little")
        assert d["bytes"].tolist() == [1, 2, 3]
        assert d["ints"].tolist() == [10, 20, 30]
        assert d["longs"].tolist() == [1, 2]

    def test_string_list_and_empty(self):
        d = nbt_lite.parse_nbt(_build(), "little")
        assert d["strs"] == ["a", "b"]
        assert d["empty"] == []

    def test_matches_nbtlib(self):
        raw = _build()
        fast = nbt_lite.parse_nbt(raw, "little")
        ref = nbtlib.File.parse(io.BytesIO(raw), byteorder="little")
        assert fast["count"] == int(ref["count"])
        assert fast["vals"].tolist() == [int(v) for v in ref["vals"]]
        assert fast["bytes"].tolist() == [int(v) for v in ref["bytes"]]
        assert fast["strs"] == [str(v) for v in ref["strs"]]

    def test_big_int_list_bulk(self):
        """大 List[Int] 走批量读取（形状/值正确）。"""
        n = 100_000
        root = nbtlib.Compound(
            {"big": nbtlib.List([nbtlib.Int((i % 7) - 3) for i in range(n)])}
        )
        buf = io.BytesIO()
        nbtlib.File(root, byteorder="little").write(buf, byteorder="little")
        d = nbt_lite.parse_nbt(buf.getvalue(), "little")
        assert isinstance(d["big"], np.ndarray)
        assert d["big"].shape == (n,)
        assert d["big"][0] == -3 and d["big"][3] == 0

    def test_bad_root(self):
        import pytest

        with pytest.raises(ValueError):
            nbt_lite.parse_nbt(b"\x03\x00\x00", "little")


class TestRootFirstKey:
    def test_mcstructure(self):
        from .fixtures import make_mcstructure_bytes

        assert nbt_lite.root_first_key(make_mcstructure_bytes(), "little") == "format_version"

    def test_non_compound_returns_none(self):
        assert nbt_lite.root_first_key(b"\x00", "little") is None
        assert nbt_lite.root_first_key(b"", "little") is None
