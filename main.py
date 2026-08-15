from __future__ import annotations

import argparse
from pathlib import Path
import time

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


def _read_building(src: str, mapping_file: str | None) -> ezbuild.Building:
    """读取一个建筑文件；--mapping 仅对 bdx 生效。"""
    if mapping_file and registry.format_for_path(src) == "bdx":
        from ezbuild.readers.bdx import BDXReader

        return BDXReader(mapping_file=mapping_file).read(src)
    return ezbuild.convert_read(src)


def _convert_one(src: str, args: argparse.Namespace) -> tuple[str, str]:
    """转换单个文件，返回 (输出路径, 输出格式)。出错抛异常。"""
    start = time.monotonic()
    if not Path(src).is_file():
        raise FileNotFoundError(f"输入文件不存在: {src}")

    print(f"[1/2] 读取建筑: {src}")
    building = _read_building(src, args.mapping)
    print(
        f"      方块 {building.block_count} 个，命令方块 {building.command_block_count} 个"
    )

    to_format = args.format  # 第一个位置参数（输出格式，必填）
    if args.output is not None:
        if len(args.input) > 1:
            raise ValueError("-o/--output 仅支持单个输入文件")
        output = args.output
    else:
        writer_cls = registry.get_writer(to_format)
        ext = writer_cls.extensions[0] if writer_cls else ".out"
        output = str(Path(src).with_suffix(ext))
        if Path(output) == Path(src):
            # 输入输出同路径（如 txt → txt），加后缀避免覆盖
            suffix = "_分区块" if to_format == "txt" else "_转换"
            output = str(
                Path(src).with_name(Path(src).stem + suffix + Path(src).suffix)
            )

    print(f"[2/2] 输出格式 {to_format} -> {output}")
    if args.all_states:
        # 保留全部方块状态（不省略 open_bit/toggle_bit 等）
        writer_cls = registry.get_writer(to_format)
        try:
            writer = writer_cls(strip_states=frozenset())
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
        help="输出格式：cmd_json / setblock_txt / ibi（必填）",
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
        help="保留全部方块状态（默认省略 open_bit/toggle_bit 等纯开关状态）",
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
