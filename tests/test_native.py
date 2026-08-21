"""``-c/--cpp`` 原生 DLL 快速路径测试。

覆盖三部分：
1. 纯常量：DLL writer 映射表 / 占位 Writer 注册 / 扩展名推断（不依赖 DLL）。
2. 需要 DLL 的转换：skipped（DLL 缺失或非 Windows）。
3. CLI 路由：占位格式无 -c 时给出清晰报错。
"""

from __future__ import annotations

import pytest

import ezbuild
from ezbuild import native
from ezbuild import registry
from ezbuild.model import Building
from ezbuild.writers.dll_only import DLL_ONLY_SPECS


def _skip_unless_dll() -> None:
    if not native.is_available():
        pytest.skip(f"原生 DLL 不可用: {native.not_found_reason()}")


class TestFormatMapping:
    """DLL writer 映射与占位注册（纯常量，无需 DLL）。"""

    def test_dll_writers_cover_overlapping_and_only(self):
        # 与 ezbuild 自带 Writer 语义重叠的格式：schem/mcstructure/ibi/txt
        assert {"schem", "mcstructure", "ibi", "txt"} <= set(native.DLL_WRITERS)
        # txt 的 DLL 输出是 MCFunction（指令文件，与本库 txt 读取器互读）
        assert native.DLL_WRITERS["txt"] == "MCFunction"
        # DLL 独有格式恰好 6 个
        assert native.DLL_ONLY_FORMATS == frozenset(
            {"bdx", "schematic", "litematic", "mcfn", "axiombp", "fuhong"}
        )
        # Python 可回退集合 = 有 Python 实现的格式
        assert native.PYTHON_FALLBACK == frozenset(
            {"schem", "mcstructure", "ibi", "txt"}
        )

    def test_dll_only_writers_registered(self):
        for fmt in native.DLL_ONLY_FORMATS:
            cls = registry.get_writer(fmt)
            assert cls is not None, f"{fmt} 未注册"
            assert cls.format_name == fmt
            assert cls.extensions[0] == DLL_ONLY_SPECS[fmt][1]

    def test_dll_only_writer_raises_without_cpp(self):
        for fmt in native.DLL_ONLY_FORMATS:
            with pytest.raises(NotImplementedError, match="需要加 -c/--cpp"):
                registry.get_writer(fmt)().render(Building())

    def test_output_extension(self):
        """输出路径按占位 Writer 的扩展名推断（bdx → .bdx 等）。"""
        for fmt, (_, ext, _) in DLL_ONLY_SPECS.items():
            cls = registry.get_writer(fmt)
            assert ext in cls.extensions


class TestNativeConvert:
    """需要 DLL 的真实转换（DLL 缺失时跳过）。"""

    def test_context_convert_roundtrip(self, tmp_path):
        """合成 mcstructure → DLL 转换 → ezbuild 读回，方块/命令方块不丢。"""
        _skip_unless_dll()
        from .fixtures import make_mcstructure_bytes

        src = tmp_path / "in.mcstructure"
        src.write_bytes(make_mcstructure_bytes())
        out = tmp_path / "out.mcstructure"

        with native.Context() as ctx:
            ctx.convert(str(src), "MCStructure", str(out), threads=0)

        b = ezbuild.convert_read(out)
        assert b.block_count == 5
        by_pos = {(bl.x, bl.y, bl.z): bl.name for bl in b.blocks}
        assert by_pos[(0, 0, 0)] == "command_block"
        assert by_pos[(2, 2, 0)] == "repeating_command_block"
        # 命令方块语义保留
        assert len(b.command_blocks) == 3
        cmds = sorted(c.command for c in b.command_blocks)
        assert cmds == ["execute as @a run say hi", "say hello", "say loop"]

    def test_dll_only_format_writes_file(self, tmp_path):
        """BDX 是 DLL 独有输出格式：-c 时 DLL 能写出。"""
        _skip_unless_dll()
        from .fixtures import make_mcstructure_bytes

        src = tmp_path / "in.mcstructure"
        src.write_bytes(make_mcstructure_bytes())
        out = tmp_path / "out.bdx"
        with native.Context() as ctx:
            ctx.convert(str(src), "BDX", str(out), threads=0)
        assert out.stat().st_size > 0
        # BDX 是合法输入格式，ezbuild 能读回
        b = ezbuild.convert_read(out)
        assert b.block_count >= 1

    def test_unreadable_input_raises(self, tmp_path):
        """DLL 读不了的文件抛 Error（CLI 靠这个触发 Python 回退）。"""
        _skip_unless_dll()
        bogus = tmp_path / "bogus.schem"
        bogus.write_bytes(b"this is not a schem at all")
        with native.Context() as ctx:
            with pytest.raises(native.Error):
                ctx.convert(str(bogus), "MCStructure", str(tmp_path / "x.mcstructure"))


class TestCliRouting:
    """CLI 层路由：-c 与占位格式的报错。"""

    def test_main_dll_only_requires_cpp(self, capsys):
        """不带 -c 用 DLL 独有格式 → 报错并提示加 -c。"""
        import main as cli

        rc = cli.main(["bdx", "-i", "samples/sample.mcstructure"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "需要加 -c/--cpp" in out or "需要 -c" in out

    def test_main_cpp_hint_for_python_only(self, tmp_path, capsys):
        """-c 配无 DLL 实现的格式（cmd_json）→ 提示回退 Python，仍能完成。"""
        import main as cli

        out = tmp_path / "out.json"
        rc = cli.main(
            ["cmd_json", "-i", "samples/sample.mcstructure", "-c", "-o", str(out)]
        )
        assert out.stat().st_size > 0
        out_text = capsys.readouterr().out
        assert "无 C++ DLL 实现" in out_text or "Python 原生" in out_text
        assert rc == 0

    def test_main_cpp_txt_routes_dll(self, tmp_path, capsys):
        """-c txt 现在走 DLL（MCFunction 指令文件），不再回退 Python。"""
        _skip_unless_dll()
        import main as cli
        from .fixtures import make_mcstructure_bytes

        src = tmp_path / "in.mcstructure"
        src.write_bytes(make_mcstructure_bytes())
        out = tmp_path / "out.txt"
        rc = cli.main(["txt", "-i", str(src), "-c", "-o", str(out)])
        assert rc == 0
        assert out.stat().st_size > 0
        text = out.read_text(encoding="utf-8", errors="replace")
        assert "setblock" in text or "fill" in text
        out_text = capsys.readouterr().out
        assert "C++ DLL" in out_text  # 走了 DLL 路径，而非回退

    def test_main_cpp_dll_path_writes_file(self, tmp_path, capsys):
        """-c 走 DLL：mcstructure 输入 → bdx 输出文件生成。"""
        _skip_unless_dll()
        import main as cli
        from .fixtures import make_mcstructure_bytes

        src = tmp_path / "in.mcstructure"
        src.write_bytes(make_mcstructure_bytes())
        out = tmp_path / "out.bdx"
        rc = cli.main(["bdx", "-i", str(src), "-c", "-o", str(out)])
        assert rc == 0
        assert out.stat().st_size > 0
        assert "C++ DLL" in capsys.readouterr().out
