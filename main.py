from __future__ import annotations

from pathlib import Path
import argparse
import time
import sys

import pyfiglet

import ezbuild
from ezbuild import registry

def _fmt_duration(seconds: float) -> str:
    """把秒数格式化为易读的耗时文本。"""
    if seconds < 1:
        ms = seconds * 1000
        return f"{ms:.0f} 毫秒" if ms >= 1 else "<1 毫秒"
    return f"{seconds:.2f} 秒"


def cmd_list(_args) -> int:
    print("输入格式（Reader）:")
    for name in registry.list_readers():
        cls = registry.get_reader(name)
        exts = "/".join(cls.extensions)
        print(f"  {name:<14} {exts:<20} {cls.description}")
    print("\n输出格式（Writer）:")
    for name in registry.list_writers():
        cls = registry.get_writer(name)
        exts = "/".join(cls.extensions)
        print(f"  {name:<14} {exts:<20} {cls.description}")
    return 0


def _read_building(src: str, mapping_file: str | None) -> ezbuild.Building | ezbuild.Song:
    """读取一个建筑或音乐文件；--mapping 仅对 bdx 生效。"""
    if mapping_file and registry.format_for_path(src) == "bdx":
        from ezbuild.readers.bdx import BDXReader

        return BDXReader(mapping_file=mapping_file).read(src)
    return ezbuild.convert_read(src)


def _output_path(src: str, to_format: str, args: argparse.Namespace) -> str:
    """计算输出路径：-o 指定，否则按输入名 + 输出扩展名（同路径时加后缀）。"""
    if args.output is not None:
        if len(args.input) > 1:
            raise ValueError("-o/--output 仅支持单个输入文件")
        return args.output
    writer_cls = registry.get_writer(to_format)
    ext = writer_cls.extensions[0] if writer_cls else ".out"
    output = str(Path(src).with_suffix(ext))
    if Path(output) == Path(src):
        # 输入输出同路径（如 txt → txt），加后缀避免覆盖
        suffix = "_分区块" if to_format == "txt" else "_转换"
        output = str(Path(src).with_name(Path(src).stem + suffix + Path(src).suffix))
    return output


def _convert_one(src: str, args: argparse.Namespace) -> tuple[str, str]:
    """转换单个文件，返回 (输出路径, 输出格式)。出错抛异常。"""
    start = time.monotonic()
    if not Path(src).is_file():
        raise FileNotFoundError(f"输入文件不存在: {src}")

    to_format = args.format  # 第一个位置参数（输出格式，必填）

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

            output = _output_path(src, to_format, args)
            print(f"[1/2] 流式转换: {src}")
            fn(src, output, **kwargs)
            elapsed = time.monotonic() - start
            print(f"[2/2] 输出格式 {to_format} -> {output}")
            print(f"      转换完成 ✓（耗时 {_fmt_duration(elapsed)}）")
            return output, to_format

    print(f"[1/2] 读取: {src}")
    building = _read_building(src, args.mapping)
    if isinstance(building, ezbuild.Song) and to_format not in ezbuild.MUSIC_FORMATS:
        # 音乐 → 建筑：先转成命令方块音乐机
        building = ezbuild.song_to_building(building, edition=args.edition)
    if isinstance(building, ezbuild.Song):
        n = len(building.notes)
        if n:
            duration = max(note.time for note in building.notes)
            print(f"      音符 {n} 个，层 {len(building.layers)} 个，时长 {duration:.2f}s")
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
    ascii_art = pyfiglet.figlet_format(text, font="standard")
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
    print(gradient_ascii("Ez Build", start_color=(0, 255, 128), end_color=(0, 0, 255)))
    parser = argparse.ArgumentParser(
        prog="ezbuild",
        description="Minecraft 建筑文件格式转换工具",
        usage="%(prog)s <输出格式> -i <输入文件...> [-o 输出文件] [--mapping 映射表]",
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
        "--mapping",
        metavar="FILE",
        help="BDX Runtime ID 映射表 JSON 路径",
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
        "--edition",
        choices=("bedrock", "java"),
        default="bedrock",
        help="音乐转建筑时 /playsound 命令的版本语法（默认 bedrock）",
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
