from __future__ import annotations

from pathlib import Path
import argparse
import os
import time
import sys

from pyfiglet import figlet_format

import ezbuild
from ezbuild import registry


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化为易读的耗时文本。"""
    if seconds < 1:
        ms = seconds * 1000
        return f"{ms:.0f} 毫秒" if ms >= 1 else "<1 毫秒"
    return f"{seconds:.2f} 秒"


def cmd_list(_args) -> int:
    from ezbuild.native import DLL_ONLY_FORMATS

    print("输入格式（Reader）:")
    for name in registry.list_readers():
        cls = registry.get_reader(name)
        exts = "/".join(cls.extensions)
        print(f"  {name:<14} {exts:<20} {cls.description}")
    # 世界文件夹不是文件（无扩展名），按目录识别，单独列出
    print(f"  {'world':<14} {'(文件夹)':<20} "
          f"Minecraft 世界文件夹（Java region/ 或 Bedrock db/，-pos x1 y1 z1 x2 y2 z2 框包围盒）")
    print("\n输出格式（Writer）:")
    for name in registry.list_writers():
        cls = registry.get_writer(name)
        exts = "/".join(cls.extensions)
        marker = "（需 -c）" if name in DLL_ONLY_FORMATS else ""
        print(f"  {name:<14} {exts:<20} {cls.description}{marker}")
    return 0


def _read_building(src: str) -> ezbuild.Building | ezbuild.Song:
    """读取一个建筑或音乐文件（按扩展名自动识别）。"""
    return ezbuild.convert_read(src)


def _print_schem_info(src: str) -> None:
    """打印 schem/schematic 文件信息（大小、尺寸、方块数、命令方块数）。"""
    from ezbuild.streaming import SchematicSource, _count_non_air

    s = SchematicSource(src)
    W, H, L = s.size
    size = os.path.getsize(src)
    size_str = f"{size / 1e6:.1f}MB" if size >= 1e6 else f"{size / 1024:.0f}KB"
    print(
        f"      文件 {size_str} | 尺寸 {W}×{H}×{L} | "
        f"方块 {_count_non_air(s):,} 个, 命令方块 {len(list(s.command_blocks())):,} 个"
    )


def _print_txt_stats(stats: dict) -> None:
    """打印分区块优化统计：原始指令数 → 输出指令数（fill/setblock/tp），减少百分比。

    参考 fill3d_rust 的压缩率计算；``rate`` 为负数表示指令反而变多（稀疏结构）。
    """
    out = stats["fill"] + stats["setblock"] + stats["tp"]
    print(
        f"      优化: 原始 {stats['orig']:,} 条 → 输出 {out:,} 条"
        f"（fill {stats['fill']:,} / setblock {stats['setblock']:,} / tp {stats['tp']:,}）"
        f"，减少 {stats['rate']:.2f}%"
    )


def _output_path(src: str, to_format: str, args: argparse.Namespace) -> str:
    """计算输出路径：-o 指定，否则按输入名 + 输出扩展名（同路径时加后缀）。"""
    if args.output is not None:
        if len(args.input) > 1:
            raise ValueError("-o/--output 仅支持单个输入文件")
        return args.output
    writer_cls = registry.get_writer(to_format)
    ext = writer_cls.extensions[0] if writer_cls else ".out"
    p = Path(src)
    if p.is_dir():
        # 世界文件夹：输出到同名 + 扩展名（避免 with_suffix 误改文件夹名）
        output = str(p.with_name(p.name + ext))
        return output
    output = str(p.with_suffix(ext))
    if Path(output) == Path(src):
        # 输入输出同路径（如 txt → txt），加后缀避免覆盖
        suffix = "_分区块" if to_format == "txt" else "_转换"
        output = str(Path(src).with_name(Path(src).stem + suffix + Path(src).suffix))
    return output


def _convert_one_dll(
    src: str, args: argparse.Namespace, start: float
) -> tuple[str, str] | None:
    """``-c/--cpp`` 快速路径：整个转换交给 C++ DLL（无 GIL、无 Python 模型）。

    返回 ``(输出路径, 输出格式)`` 表示 DLL 转换成功；返回 ``None`` 表示
    未走 DLL（格式无 DLL 实现 / DLL 不可用 / DLL 转换失败且可回退 Python），
    调用方继续走原 Python 原生转换。
    """
    from ezbuild import native

    to_format = args.format
    dll_name = native.DLL_WRITERS.get(to_format)
    if dll_name is None:
        # 输出格式无 DLL 实现（如 txt / cmd_json / mid / nbs）
        print(f"  [提示] 输出格式 {to_format} 无 C++ DLL 实现，使用 Python 原生转换")
        return None

    if not native.is_available():
        if to_format in native.PYTHON_FALLBACK:
            # schem/mcstructure/ibi：有 Python 实现，回退
            print(
                f"  [提示] 未找到 C++ DLL（{native.not_found_reason()}），"
                f"使用 Python 原生转换"
            )
            return None
        # DLL 独有格式：无 Python 实现，只能报错
        raise ValueError(
            f"输出格式 {to_format} 由 C++ DLL 生成，需要 -c，但 DLL 不可用："
            f"{native.not_found_reason()}"
        )

    output = _output_path(src, to_format, args)
    print(f"[1/2] C++ DLL 转换: {src}")

    if args.all_states or args.nofill or args.raw_range or args.raw_octave:
        print(
            "  [提示] -c 走 DLL，--all-states/--nofill/--raw-range/--raw-octave "
            "等 Python 选项不生效"
        )

    threads = args.threads if args.threads is not None else 0
    try:
        with native.Context() as ctx:
            try:
                info = ctx.inspect(src)
                print(
                    f"      尺寸 {info.width}×{info.height}×{info.length} | "
                    f"非空气方块 {info.non_air_blocks:,} 个"
                )
            except Exception:
                pass  # 读不了不拦着，交给 convert 决定
            ctx.convert(src, dll_name, output, threads=threads)
    except Exception as e:
        # 任何原生环节失败（上下文创建/读文件/转换）都按"可回退则回退"处理
        if to_format in native.PYTHON_FALLBACK:
            print(f"      DLL 转换失败（{e}），回退 Python 原生转换")
            return None
        raise ValueError(f"DLL 转换失败: {e}") from e

    elapsed = time.monotonic() - start
    print(f"[2/2] 输出格式 {to_format} -> {output}")
    print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
    return output, to_format


def _convert_world(src: str, args: argparse.Namespace) -> tuple[str, str]:
    """世界导出：把 Java 世界文件夹内包围盒（起始/结束 xyz）的建筑导出为指定格式。

    流式逐区块转换，内存与包围盒总方块数无关（mcstructure/schem 除外，
    这两个格式需要整块数组，内存 ≈ 包围盒体积）。
    """
    start = time.monotonic()
    to_format = args.format
    if args.pos is None:
        raise ValueError("世界导出需要坐标：-pos x1 y1 z1 x2 y2 z2（起始与结束 xyz）")
    coords = tuple(args.pos)

    from ezbuild.world import (
        world_to_cmd_json,
        world_to_ibi,
        world_to_mcstructure,
        world_to_schem,
        world_to_txt,
    )

    _WORLD = {
        "txt": world_to_txt,
        "ibi": world_to_ibi,
        "cmd_json": world_to_cmd_json,
        "mcstructure": world_to_mcstructure,
        "schem": world_to_schem,
    }
    fn = _WORLD.get(to_format)
    if fn is None:
        raise ValueError(
            f"世界导出不支持输出格式 {to_format!r}（支持: {'/'.join(_WORLD)}；"
            f"-c DLL 格式 bdx/litematic/mcfn 等不支持世界导出）"
        )

    output = _output_path(src, to_format, args)
    x1, y1, z1, x2, y2, z2 = coords
    print(f"[1/2] 世界导出: {src}")
    print(f"      坐标范围: ({x1}, {y1}, {z1}) → ({x2}, {y2}, {z2})")
    kwargs: dict = {"progress": True}
    if to_format in ("txt", "ibi"):
        kwargs["strip_states"] = frozenset() if args.all_states else None
    if to_format == "txt":
        kwargs["fill_merge"] = not args.nofill
        if not args.nofill and args.threads is not None:
            print("  [提示] 世界导出 txt 分区块模式需按顺序输出，-t 并行不生效；可加 --nofill 用纯 setblock 并行")
    if to_format in ("txt", "ibi") and args.threads is not None:
        kwargs["workers"] = args.threads  # -t 并行（纯 setblock / ibi）
    if to_format == "schem" and args.split:
        kwargs["split"] = True  # 自动拆分成多个 schem（调色板 >256 时）
    ret = fn(src, output, box=coords, **kwargs)

    elapsed = time.monotonic() - start
    if isinstance(ret, (list, tuple)):
        print(f"[2/2] 输出格式 {to_format} -> 拆分为 {len(ret)} 个文件")
        for p in ret:
            print(f"      {p}")
        output = str(ret[0])
    else:
        print(f"[2/2] 输出格式 {to_format} -> {output}")
    print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
    return output, to_format


def _convert_one(src: str, args: argparse.Namespace) -> tuple[str, str]:
    """转换单个文件，返回 (输出路径, 输出格式)。出错抛异常。"""
    start = time.monotonic()
    if Path(src).is_dir():
        # 世界文件夹导出（需 --x1..--z2 包围盒坐标）
        return _convert_world(src, args)
    if not Path(src).is_file():
        raise FileNotFoundError(f"输入文件不存在: {src}")

    to_format = args.format  # 第一个位置参数（输出格式，必填）

    # -c/--cpp：优先走 C++ DLL 快速转换（无 GIL、不建 Python 模型）。
    # 返回 (输出, 格式) 表示已转换完成；None 表示未走 DLL，继续 Python 原生路径。
    if args.cpp:
        result = _convert_one_dll(src, args, start)
        if result is not None:
            return result

    # schem / schematic -> 建筑格式：流式增量转换，避免巨型结构载入整座建筑占满内存
    src_fmt = registry.format_for_path(src)
    if src_fmt in ("schem", "schematic"):
        from ezbuild.streaming import (
            schematic_to_cmd_json,
            schematic_to_ibi,
            schematic_to_mcstructure,
            schematic_to_schem,
            schematic_to_txt,
        )

        _STREAMING = {
            "txt": (schematic_to_txt, {"fill_merge": True, "strip_states": None}),
            "ibi": (schematic_to_ibi, {"strip_states": None}),
            "mcstructure": (schematic_to_mcstructure, {}),
            "schem": (schematic_to_schem, {}),
            "cmd_json": (schematic_to_cmd_json, {}),
        }
        if to_format in _STREAMING:
            fn, kwargs = _STREAMING[to_format]
            kwargs = dict(kwargs)
            kwargs["progress"] = True
            if "fill_merge" in kwargs:
                kwargs["fill_merge"] = not args.nofill
            if "strip_states" in kwargs:
                kwargs["strip_states"] = frozenset() if args.all_states else None
            if to_format in ("ibi", "txt"):
                kwargs["workers"] = args.threads  # -t 强制/指定并行进程数
                if to_format == "txt" and not args.nofill and args.threads is not None:
                    print("  [提示] txt 分区块模式是磁盘 I/O 瓶颈，-t 并行不生效；可加 --nofill 得纯 setblock（自动单进程已最优）")
            elif args.threads is not None:
                print(f"  [提示] -t 并行仅对 ibi/txt 生效，{to_format} 忽略 -t")

            output = _output_path(src, to_format, args)
            print(f"[1/2] 流式转换: {src}")
            _print_schem_info(src)  # 转换前输出文件信息
            fn(src, output, **kwargs)
            elapsed = time.monotonic() - start
            print(f"[2/2] 输出格式 {to_format} -> {output}")
            print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
            return output, to_format

    # txt → txt：未分区块 txt 重排分区块。直接流式解析分组（不建 Building 模型），
    # 省掉 Building.blocks 与中间 dict 的重复拷贝，大文件内存和时间都大减。
    if src_fmt == "txt" and to_format == "txt":
        from ezbuild.streaming import txt_to_chunked_txt

        output = _output_path(src, to_format, args)
        print(f"[1/2] 流式转换: {src}")
        _, stats = txt_to_chunked_txt(
            src,
            output,
            fill_merge=not args.nofill,
            strip_states=frozenset() if args.all_states else None,
            progress=True,
        )
        if stats is not None:
            _print_txt_stats(stats)
        elapsed = time.monotonic() - start
        print(f"[2/2] 输出格式 {to_format} -> {output}")
        print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
        return output, to_format

    print(f"[1/2] 读取: {src}")
    building = _read_building(src)
    if isinstance(building, ezbuild.Song) and to_format not in ezbuild.MUSIC_FORMATS:
        # 音乐 → 建筑：先转成命令方块音乐机（默认把超范围音符八度折叠保证音准；
        # --raw-octave 保留原始八度）
        building = ezbuild.song_to_building(building, fold=not args.raw_octave)
    if isinstance(building, ezbuild.Song):
        n = len(building.notes)
        if n:
            duration = max(note.time for note in building.notes)
            print(
                f"      音符 {n} 个，层 {len(building.layers)} 个，时长 {duration:.2f}s"
            )
        else:
            print("      空歌曲")
    else:
        print(
            f"      方块 {building.block_count} 个，命令方块 {building.command_block_count} 个"
        )

    output = _output_path(src, to_format, args)

    print(f"[2/2] 输出格式 {to_format} -> {output}")
    writer_cls = registry.get_writer(to_format)
    kwargs = {}
    if args.all_states:
        # 保留全部方块状态（不省略 *_bit 开关状态）
        kwargs["strip_states"] = frozenset()
    if args.nofill:
        # 不进行三维 fill 合并（txt 格式）
        kwargs["fill_merge"] = False
    if args.raw_range:
        # NBS 输出不折叠音高（保留原始音高）
        kwargs["fold_range"] = False
    if to_format == "txt" and not args.nofill:
        # 分区块 txt 的 fill 合并阶段显示进度条（纯 setblock 直写无需）
        kwargs["progress"] = True
    if kwargs:
        try:
            writer = writer_cls(**kwargs)
        except TypeError:  # 不支持的 writer（如 cmd_json）直接默认构造
            writer = writer_cls()
        writer.write(building, output)
    else:
        ezbuild.convert_write(building, output, to_format)
    elapsed = time.monotonic() - start
    print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
    return output, to_format


def gradient_ascii(text, start_color=(255, 0, 0), end_color=(0, 0, 255)):
    ascii_art = figlet_format(text, font="standard")
    lines = ascii_art.rstrip("\n").split("\n")

    max_width = max(len(line) for line in lines) if lines else 0

    # 逐行处理
    output = []
    for line in lines:
        padded = line.ljust(max_width)
        colored_line = []
        for col, char in enumerate(padded):
            if char == " ":
                # 空格保留为无色（或可保留颜色以形成背景渐变，这里跳过）
                colored_line.append(" ")
                continue
            # 计算渐变因子 t (0~1)
            t = col / max_width if max_width > 1 else 0
            # 插值 RGB
            r = int(start_color[0] + (end_color[0] - start_color[0]) * t)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * t)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * t)
            colored_char = f"\033[38;2;{r};{g};{b}m{char}\033[0m"
            colored_line.append(colored_char)
        output.append("".join(colored_line))
    return "\n".join(output)


def cmd_convert(args) -> int:
    start = time.monotonic()
    errors = 0
    for src in args.input:
        try:
            _convert_one(src, args)
        except Exception as e:
            print(f"  错误：{e}")
            errors += 1
    total = time.monotonic() - start
    if errors:
        print(f"\n{errors} 个文件转换失败（总耗时 {_fmt_duration(total)}）")
        return 1
    print(f"\n全部完成 ✓（共 {len(args.input)} 个文件，总耗时 {_fmt_duration(total)}）")
    return 0


def main(argv=None) -> int:
    # Windows 中文控制台默认 GBK 无法编码 ✓ 等字符，改为"替换"模式避免转换完成时报错；
    # 只影响无法编码的字符（GBK 下 ✓→?），不影响中文与 UTF-8 终端。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    print(gradient_ascii("Ez Build", start_color=(0, 255, 128), end_color=(0, 0, 255)))
    parser = argparse.ArgumentParser(
        prog="ezbuild",
        description="Minecraft 建筑文件格式转换工具",
        usage="%(prog)s <输出格式> -i <输入文件...> [-o 输出文件]",
    )
    parser.add_argument(
        "format",
        nargs="?",
        metavar="输出格式",
        help="输出格式：cmd_json / txt / ibi（必填）",
    )
    parser.add_argument(
        "-i",
        "--input",
        nargs="+",
        metavar="FILE",
        help="输入建筑文件（可多个，自动识别 .bdx/.mcstructure 等格式）",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="列出支持的输入/输出格式",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="输出文件路径（仅单个输入文件时可用；默认与输入同名同目录）",
    )
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="保留全部方块状态（默认省略 *_bit 开关状态）",
    )
    parser.add_argument(
        "--nofill",
        action="store_true",
        help="txt 格式不进行三维 fill 合并（输出纯 setblock）",
    )
    parser.add_argument(
        "--raw-range",
        action="store_true",
        help="NBS 输出保留原始音高，不做可播放范围(33-57)八度折叠",
    )
    parser.add_argument(
        "--raw-octave",
        action="store_true",
        help="音乐转建筑时保留原始八度（不做可播放范围八度折叠，极端音高会被钳到两端）",
    )
    parser.add_argument(
        "-t",
        "--threads",
        nargs="?",
        const=0,
        type=int,
        default=None,
        metavar="N",
        help="强制并行转换（-t 自动，-t N 指定 N）；"
             "Python 路径=共享内存多进程，-c 时传给 DLL 线程数。"
             "默认大文件（非空气方块≥100万）自动并行，小文件单进程",
    )
    parser.add_argument(
        "-c",
        "--cpp",
        action="store_true",
        help="使用随包的 C++ DLL（water_structure_shared.dll）做整文件转换，"
             "突破 Python 性能限制与解释器锁；DLL 不支持的格式/读不了的文件"
             "自动回退 Python 原生转换。schem/mcstructure/ibi/txt 走 DLL"
             "（txt 的 DLL 输出是绝对坐标指令文件，与 Python 分区块 txt 不同），"
             "并额外解锁 bdx/schematic/litematic/mcfn/axiombp/fuhong 输出格式",
    )
    # 世界导出：-i 指向世界文件夹时需给出包围盒坐标 -pos x1 y1 z1 x2 y2 z2（世界坐标，含端点）
    parser.add_argument(
        "-pos",
        "--pos",
        nargs=6,
        type=int,
        metavar=("X1", "Y1", "Z1", "X2", "Y2", "Z2"),
        help="世界导出包围盒坐标（-pos x1 y1 z1 x2 y2 z2，起始与结束 xyz，含端点）",
    )
    parser.add_argument(
        "-s",
        "--split",
        action="store_true",
        help="schem 导出时若区域方块种类超过 256（Sponge 调色板上限），"
             "自动沿 x/z 轴拆分成多个 <名>_N.schem（每个 ≤256 种）",
    )

    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args)
    if args.input:
        if not args.format:
            parser.error(
                "必须指定输出格式（第一个参数），例如：ezbuild cmd_json -i 建筑.bdx"
            )
        if args.format not in registry.list_writers():
            parser.error(
                f"未知输出格式 {args.format!r}，可用: {registry.list_writers()}"
            )
        return cmd_convert(args)

    # 无参数：显示帮助
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
