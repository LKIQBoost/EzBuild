"""增量（流式）转换：把超大 schem/schematic 一部分一部分转成 txt，避免整座建筑载入内存。

常规路径是 Reader → Building（整座建筑在内存）→ Writer，对千万级方块的巨型
结构会吃掉数 GB。这里直接按 16×16 区块列从原始方块数组提取、就地 fill 合并并
**逐行写出**，内存 ≈ 方块数组 + 单个区块，与总方块数无关。

支持两种 Java 结构格式（自动识别）：
- Sponge ``.schem``（``Palette``/``BlockData``/``Offset``）
- 经典 ``.schematic``（``Blocks``/``Data`` + 方块映射表）
"""

from __future__ import annotations

import gzip
import io
import json
import os
import time
from typing import Callable, Iterator, Union

import nbtlib
import numpy as np

from .model import Block, CommandBlock, COMMAND_BLOCK_IDS, COMMAND_BLOCK_MODES, MODE_IMPULSE
from .readers.base import Source
from .readers.schem import _as_uint8, _parse_blockstate
from .readers.schematic import _parse_spec
from .readers.txt import _coord, _states_from
from .utils import format_block_states, load_schematic_table, normalize_block_name, tag_to_python
from .writers.txt import _divide_chunks, _optimize_region, _s_sort, _y_sort_key

Output = Union[str, "os.PathLike", "io.TextIOBase"]

# 进度条与内存显示从 _progress 引入（共享，避免 writers/streaming 循环导入）；
# 保留 _memory_mb/_refresh_memory_postfix 别名以便旧代码引用。
from ._progress import make_progress  # noqa: F401
from ._progress import memory_mb as _memory_mb  # noqa: F401
from ._progress import refresh_memory_postfix as _refresh_memory_postfix  # noqa: F401


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def _write_parallel_lines(output: Output, files: list) -> Output:
    """把并行生成的 setblock 行临时文件合并写出（去末尾换行）。"""
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    try:
        for i, path in enumerate(files):
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            if i == len(files) - 1 and data.endswith("\n"):
                data = data[:-1]
            fileobj.write(data)
    finally:
        if close:
            fileobj.close()
    return output


def _plain_setblock_txt(source, output, strip_states, progress, workers) -> Output:
    """纯 setblock txt（--nofill，无 tp/分块）。

    txt 是 I/O 瓶颈（写大文本），单进程直写最优；仅 ``-t`` 强制时才并行
    （并行要多写一次临时文件并合并，反而慢，不自动启用）。
    """
    src = SchematicSource(source, strip_states=strip_states)
    if workers is not None:
        files = _generate_txt_parallel(src, src._entries, _parallel_worker_count(workers), progress)
        try:
            return _write_parallel_lines(output, files)
        finally:
            for f in files:
                try:
                    os.remove(f)
                except OSError:
                    pass

    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    bar = None
    n = 0
    if progress:
        from tqdm.rich import tqdm

        counts = np.bincount(src.array3.ravel())
        bar = tqdm(total=int(src.array3.size - counts[src.air]),
                   desc="生成 setblock", unit="个")
        _refresh_memory_postfix(bar)
    try:
        first = True
        for x, y, z, name, state_str in src.iter_all_blocks():
            coord = f"~{x} ~{y} ~{z}"
            if not first:
                fileobj.write("\n")
            fileobj.write(f"setblock {coord} {name} [{state_str}]" if state_str
                          else f"setblock {coord} {name}")
            first = False
            if bar is not None:
                bar.update(1)
                n += 1
                if n % 10000 == 0:
                    _refresh_memory_postfix(bar)
    finally:
        if bar is not None:
            _refresh_memory_postfix(bar)
            bar.close()
        if close:
            fileobj.close()
    return output


def schematic_to_txt(
    source: Source,
    output: Output,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
    workers: int | None = None,
):
    """流式把 Sponge .schem 或经典 .schematic 渲染为 txt，增量写入 ``output``。

    ``fill_merge=True``：分区块（16×16 + tp 导航 + fill 合并，单进程）；
    ``fill_merge=False``（--nofill）：纯 setblock（无 tp/分块），大文件或 ``-t`` 时共享内存并行。
    输出与「格式 → Building → txt」逐字节一致。返回 ``output``。
    """
    if not fill_merge:
        return _plain_setblock_txt(source, output, strip_states, progress, workers)

    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    write_bar = None
    conv_time = 0.0
    write_time = 0.0
    try:
        it = iter_schematic_txt_lines(
            source, chunk_size=chunk_size, fill_merge=fill_merge,
            strip_states=strip_states, progress=progress,
        )
        first = True
        while True:
            # 生成（CPU）时间
            t0 = time.monotonic()
            try:
                line = next(it)
            except StopIteration:
                break
            conv_time += time.monotonic() - t0

            # 写入磁盘（I/O）时间
            t0 = time.monotonic()
            # 写入进度条（懒创建，让"转换方块"条在它上面；不显示内存）
            if write_bar is None and progress:
                from tqdm.rich import tqdm

                write_bar = tqdm(desc="写入磁盘", unit="行")
            if not first:
                fileobj.write("\n")
            fileobj.write(line)
            first = False
            write_time += time.monotonic() - t0
            if write_bar is not None:
                write_bar.update(1)
    finally:
        if write_bar is not None:
            # tqdm.rich 的 total 在创建时固定；完成时直接更新 rich task 为 100%
            # （rich 在 Windows 非 TTY 下渲染可能崩，整体容错）
            try:
                write_bar._prog.update(
                    write_bar._task_id, total=write_bar.n, completed=write_bar.n
                )
                write_bar.refresh()
            except Exception:
                pass
            try:
                write_bar.close()
            except Exception:
                pass
        if close:
            fileobj.close()
    if progress:
        print(f"      转换用时 {conv_time:.1f}s，写入用时 {write_time:.1f}s")
    return output


# 向后兼容别名（schem_to_txt 现在也接受经典 .schematic）
schem_to_txt = schematic_to_txt


# ---------------------------------------------------------------------------
# 流式 txt → 分区块 txt（未分区块 setblock/fill 文本重排分块）
# ---------------------------------------------------------------------------
def _txt_lines(source):
    """逐行产出 txt 源（bytes 一次性解码；路径按行迭代，避免整文件+splitlines 列表）。"""
    if isinstance(source, (bytes, bytearray)):
        yield from bytes(source).decode("utf-8", errors="replace").splitlines()
    else:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            yield from f


def _iter_txt_blocks(lines) -> Iterator[tuple[int, int, int, str, dict]]:
    """从 txt 行迭代产出方块 ``(x, y, z, name, states)``；fill 展开，tp/其它行忽略。

    兼容 Bedrock/MCFunction 命令的 ``/`` 前缀（``/setblock`` / ``/fill``）。
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):  # Bedrock 命令前缀
            line = line[1:]
        if line.startswith("setblock"):  # setblock 最常见，优先判断
            parts = line.split()
            if len(parts) < 4:
                continue
            yield (
                _coord(parts[1]), _coord(parts[2]), _coord(parts[3]),
                normalize_block_name(parts[4]), _states_from(parts[5:]),
            )
        elif line.startswith("fill"):
            parts = line.split()
            if len(parts) < 8:
                continue
            x1, y1, z1, x2, y2, z2 = (_coord(p) for p in parts[1:7])
            if x1 > x2:
                x1, x2 = x2, x1
            if y1 > y2:
                y1, y2 = y2, y1
            if z1 > z2:
                z1, z2 = z2, z1
            name = normalize_block_name(parts[7])
            states = _states_from(parts[8:])
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    for z in range(z1, z2 + 1):
                        yield (x, y, z, name, states)


def _scan_txt_bounds(lines):
    """第一遍扫描：返回 ``(min_x, max_x, min_z, max_z, 解析方块总数, 原始指令数)`` 或 None（空输入）。

    ``解析方块总数`` 供进度条用；``原始指令数`` = setblock/fill 命令行数，供优化率统计。
    """
    min_x = min_z = 1 << 60
    max_x = max_z = -(1 << 60)
    total = 0
    cmd = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):  # Bedrock 命令前缀
            line = line[1:]
        if line.startswith("setblock"):
            parts = line.split()
            if len(parts) < 4:
                continue
            x = _coord(parts[1])
            z = _coord(parts[3])
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if z < min_z:
                min_z = z
            if z > max_z:
                max_z = z
            total += 1
            cmd += 1
        elif line.startswith("fill"):
            parts = line.split()
            if len(parts) < 8:
                continue
            x1, y1, z1, x2, y2, z2 = (_coord(p) for p in parts[1:7])
            if x1 > x2:
                x1, x2 = x2, x1
            if y1 > y2:
                y1, y2 = y2, y1
            if z1 > z2:
                z1, z2 = z2, z1
            if x1 < min_x:
                min_x = x1
            if x2 > max_x:
                max_x = x2
            if z1 < min_z:
                min_z = z1
            if z2 > max_z:
                max_z = z2
            total += (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
            cmd += 1
    if total == 0:
        return None
    return min_x, max_x, min_z, max_z, total, cmd


def txt_to_chunked_txt(
    source: Source,
    output: Output,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    insert_count: int = 0,
    progress: bool = False,
) -> tuple[Output, dict | None]:
    """流式把未分区块的 setblock/fill txt 转为分区块 txt，增量写入 ``output``。

    直接解析 txt → 按 16×16 区块分组 → 每区块 fill 合并 → 逐行写出。
    不建 Building 模型、不把整份输出拼成一个大字符串：内存 ≈ 方块分组字典
    （+ 单个区块），比「TxtReader → TxtWriter」省掉 Building.blocks 与
    中间 dict 的重复拷贝。
    ``fill_merge=False``（--nofill）：纯 setblock 流式直写（无 tp/分块/合并）。
    输出与「格式 → Building → txt」逐字节一致。
    返回 ``(output, stats)``：``stats`` 为分区块优化统计
    ``{"orig": 原始指令数, "fill": .., "setblock": .., "tp": .., "rate": 减少百分比}``，
    ``--nofill`` 时为 ``None``。
    """
    close = not hasattr(output, "write")
    fileobj = open(output, "w", encoding="utf-8") if close else output
    try:
        if not fill_merge:
            # --nofill：读一行 → 写一行，保持文件顺序（无合并，无可统计优化率）
            first = True
            for x, y, z, name, states in _iter_txt_blocks(_txt_lines(source)):
                state_str = format_block_states(states, strip_states)
                if not first:
                    fileobj.write("\n")
                first = False
                fileobj.write(f"setblock ~{x} ~{y} ~{z} {name}")
                if state_str:
                    fileobj.write(f" [{state_str}]")
            return output, None

        # 第一遍：包围盒 + 方块总数（进度条用）+ 原始指令数（优化率统计）
        scanned = _scan_txt_bounds(_txt_lines(source))
        if scanned is None:
            return output, None  # 空输入 → 空输出
        min_x, max_x, min_z, max_z, total, orig_cmds = scanned
        chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)

        bar = None
        if progress:
            bar = make_progress(total, "转换方块", "个")
        try:
            # 第二遍：直接按区块分组（不建 Building / blocks dict），进度随解析推进
            groups: dict[tuple[int, int], dict[tuple[int, int, int], tuple[str, str]]] = {}
            n = 0
            for x, y, z, name, states in _iter_txt_blocks(_txt_lines(source)):
                state_str = format_block_states(states, strip_states)
                gx, gz = (x - min_x) // chunk_size, (z - min_z) // chunk_size
                rel = ((x - min_x) % chunk_size, y, (z - min_z) % chunk_size)
                groups.setdefault((gx, gz), {})[rel] = (name, state_str)
                if bar is not None:
                    bar.update(1)
                    n += 1
                    if n % 10000 == 0:
                        _refresh_memory_postfix(bar)

            # 逐区块输出（tp + fill/setblock，按 y 排序，fill 优先）
            first = True
            prev_x = prev_z = 0
            n_tp = n_fill = n_setblock = 0

            def emit(s: str) -> None:
                nonlocal first
                if not first:
                    fileobj.write("\n")
                fileobj.write(s)
                first = False

            for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
                region = groups.get(
                    ((sx - min_x) // chunk_size, (sz - min_z) // chunk_size), {}
                )
                if not region:
                    continue
                emit(f"tp ~{sx - prev_x} ~ ~{sz - prev_z}")
                n_tp += 1
                if insert_count:
                    for _ in range(insert_count):
                        emit("testfor @s")
                prev_x, prev_z = sx, sz
                fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
                n_fill += len(fill_cmds)
                n_setblock += len(setblock_cmds)
                for cmd in sorted(fill_cmds + setblock_cmds, key=_y_sort_key):
                    emit(cmd)
        finally:
            if bar is not None:
                _refresh_memory_postfix(bar)
                bar.close()
    finally:
        if close:
            fileobj.close()

    # 优化统计：原始 setblock/fill 指令数 → 输出指令数（fill + setblock + tp 导航），
    # 参考 fill3d_rust 的压缩率计算
    out_cmds = n_fill + n_setblock + n_tp
    rate = (orig_cmds - out_cmds) / orig_cmds * 100 if orig_cmds else 0.0
    stats = {
        "orig": orig_cmds,
        "fill": n_fill,
        "setblock": n_setblock,
        "tp": n_tp,
        "rate": rate,
    }
    return output, stats


def iter_schematic_txt_lines(
    source: Source,
    *,
    chunk_size: int = 16,
    fill_merge: bool = True,
    strip_states: frozenset | None = None,
    progress: bool = False,
) -> Iterator[str]:
    """生成器：逐行产出 schem/schematic 的 txt。

    ``fill_merge=True``：分区块（16×16 + tp 导航 + fill 合并）；
    ``fill_merge=False``（--nofill）：**纯 setblock**，绝对坐标，无 tp、无分块。
    """
    if not fill_merge:
        # --nofill：纯 setblock（无 tp、无分块）
        src = SchematicSource(source, strip_states=strip_states)
        bar = None
        n = 0
        if progress:
            from tqdm.rich import tqdm

            counts = np.bincount(src.array3.ravel())
            bar = tqdm(total=int(src.array3.size - counts[src.air]),
                       desc="生成 setblock", unit="个")
            _refresh_memory_postfix(bar)
        try:
            for x, y, z, name, state_str in src.iter_all_blocks():
                coord = f"~{x} ~{y} ~{z}"
                yield (f"setblock {coord} {name} [{state_str}]" if state_str
                       else f"setblock {coord} {name}")
                if bar is not None:
                    bar.update(1)
                    n += 1
                    if n % 10000 == 0:
                        _refresh_memory_postfix(bar)
        finally:
            if bar is not None:
                _refresh_memory_postfix(bar)
                bar.close()
        return
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        with open(source, "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":  # gzip 魔数
        data = gzip.decompress(data)
    schem = nbtlib.File.parse(io.BytesIO(data), byteorder="big")

    if "Palette" in schem:
        yield from _iter_sponge_lines(
            schem, chunk_size=chunk_size, fill_merge=fill_merge,
            strip_states=strip_states, progress=progress,
        )
    else:
        yield from _iter_classic_lines(
            schem, chunk_size=chunk_size, fill_merge=fill_merge,
            strip_states=strip_states, progress=progress,
        )


# 向后兼容别名
iter_schem_txt_lines = iter_schematic_txt_lines


def _write_output(output: Output, data) -> Output:
    """写文本或字节到输出（路径或文件对象）。"""
    close = not hasattr(output, "write")
    if close:
        mode = "wb" if isinstance(data, bytes) else "w"
        kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
        fileobj = open(output, mode, **kwargs)
    else:
        fileobj = output
    try:
        fileobj.write(data)
    finally:
        if close:
            fileobj.close()
    return output


# ---------------------------------------------------------------------------
# 流式 cmd_json（命令方块 JSON，lemon 格式）
# ---------------------------------------------------------------------------
def schematic_to_cmd_json(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 的命令方块写为 lemon JSON（不建 Building 模型）。"""
    from .writers.cmd_json import CommandBlockJsonWriter

    src = SchematicSource(source)
    entries = [CommandBlockJsonWriter._entry(cb) for cb in src.command_blocks()]
    text = json.dumps(entries, ensure_ascii=False, indent=4)
    return _write_output(output, text)


# ---------------------------------------------------------------------------
# 流式 ibi（setblock 文本段 + 命令方块 JSON 段，XOR 加密）
# ---------------------------------------------------------------------------
# 非空气方块数阈值：达到才自动启用共享内存多进程（小文件单进程零开销）
PARALLEL_THRESHOLD = 1_000_000

# 子进程全局（由 _init_worker 设置）
_W_ENTRIES = None
_W_AIR = None
_W_OX = _W_OY = _W_OZ = 0
_W_COUNTER = None


def _count_non_air(src) -> int:
    """非空气方块数（bincount，内存友好）。"""
    counts = np.bincount(src.array3.ravel())
    return int(src.array3.size - counts[src.air])


def _parallel_worker_count(workers) -> int:
    """确定进程数：指定 >0 用之；否则按 CPU 数（上限 8）。"""
    if workers and workers > 0:
        return workers
    return max(2, min(os.cpu_count() or 2, 8))


def _init_worker(entries, air, ox, oy, oz, counter):
    global _W_ENTRIES, _W_AIR, _W_OX, _W_OY, _W_OZ, _W_COUNTER
    _W_ENTRIES = entries
    _W_AIR = air
    _W_OX, _W_OY, _W_OZ = ox, oy, oz
    _W_COUNTER = counter


def _ibi_worker(args):
    """子进程：生成 [y0,y1) 的 setblock 行到临时文件（每行带换行）。

    从共享内存读 array3，不重新解析源；按块批量刷新共享进度计数。
    """
    shm_name, shape, dtype, y0, y1, tmp_path = args
    from multiprocessing import shared_memory

    shm = shared_memory.SharedMemory(name=shm_name)
    try:
        array3 = np.ndarray(shape, dtype=dtype, buffer=shm.buf)
        n = 0
        reported = 0
        with open(tmp_path, "w", encoding="utf-8") as f:
            for yy in range(y0, y1, 64):
                col = array3[yy:min(yy + 64, y1), :, :]
                mask = col != _W_AIR
                ys, zs, xs = np.nonzero(mask)
                vals = col[ys, zs, xs]
                for y, z, x, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
                    item = _W_ENTRIES.get(v)
                    if item is None:
                        continue
                    f.write(f"setblock ~{x + _W_OX} ~{yy + y + _W_OY} ~{z + _W_OZ} {item[0]}")
                    if item[1]:
                        f.write(f" [{item[1]}]")
                    f.write("\n")
                    n += 1
                    if n - reported >= 5000 and _W_COUNTER is not None:
                        with _W_COUNTER.get_lock():
                            _W_COUNTER.value += n - reported
                        reported = n
            if n > reported and _W_COUNTER is not None:
                with _W_COUNTER.get_lock():
                    _W_COUNTER.value += n - reported
    finally:
        shm.close()


class _MultiFile:
    """顺序读取多个文件的只读流（跨文件连续 read，供流式打包用）。"""

    def __init__(self, files: list):
        self._files = list(files)
        self._idx = 0
        self._cur = None

    def read(self, n: int = -1) -> bytes:
        if self._cur is None and self._idx < len(self._files):
            self._cur = open(self._files[self._idx], "rb")
        while self._cur is not None:
            chunk = self._cur.read(n)
            if chunk:
                return chunk
            self._cur.close()
            self._idx += 1
            if self._idx >= len(self._files):
                self._cur = None
                break
            self._cur = open(self._files[self._idx], "rb")
        return b""

    def close(self):
        if self._cur is not None:
            self._cur.close()
            self._cur = None


def _generate_txt_parallel(src, entries, workers, progress) -> list:
    """用共享内存多进程并行生成 setblock 临时文件，返回文件路径列表（按 y 序）。"""
    import multiprocessing as mp
    import tempfile
    import time
    from multiprocessing import shared_memory

    shm = shared_memory.SharedMemory(create=True, size=src.array3.nbytes)
    shm_arr = np.ndarray(src.array3.shape, dtype=src.array3.dtype, buffer=shm.buf)
    shm_arr[:] = src.array3
    counter = mp.Value("q", 0)

    bounds = [src.H * i // workers for i in range(workers + 1)]
    args_list = []
    tmp_files = []
    for i in range(workers):
        y0, y1 = bounds[i], bounds[i + 1]
        if y0 >= y1:
            continue
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmpf.close()
        tmp_files.append(tmpf.name)
        args_list.append((shm.name, src.array3.shape, src.array3.dtype, y0, y1, tmpf.name))

    bar = None
    if progress:
        from tqdm.rich import tqdm

        bar = tqdm(total=_count_non_air(src), desc="生成 setblock", unit="个")
        _refresh_memory_postfix(bar)

    pool = mp.Pool(workers, initializer=_init_worker,
                   initargs=(entries, src.air, src.ox, src.oy, src.oz, counter))
    try:
        async_result = pool.map_async(_ibi_worker, args_list)
        pool.close()
        last = 0
        while not async_result.ready():
            done = counter.value
            if done != last and bar is not None:
                bar.update(done - last)
                _refresh_memory_postfix(bar)
                last = done
            time.sleep(0.05)
        async_result.get()  # 抛异常则冒泡
        pool.join()
        if bar is not None:
            bar.update(counter.value - last)
            _refresh_memory_postfix(bar)
    finally:
        if bar is not None:
            bar.close()
        shm.close()
        shm.unlink()
    return tmp_files


def _pack_ibi(
    output: Output,
    txt_source,
    txt_len: int,
    json_bytes: bytes,
    progress: bool,
) -> Output:
    """把 setblock 文本源 + 命令方块 JSON 打包为完整 IBI 文件（流式 XOR 写出）。

    ``txt_source`` 为类文件对象（已 seek 到开头）；``txt_len`` 单独传入避免 seek。
    schem 流式路径与世界导出共用。
    """
    import random

    from .writers.ibi import encode_varint

    close_out = not hasattr(output, "write")
    fileobj = open(output, "wb") if close_out else output
    pack_bar = None
    if progress:
        from tqdm.rich import tqdm

        pack_bar = tqdm(total=txt_len + len(json_bytes), desc="打包加密", unit="B", unit_scale=True)
        _refresh_memory_postfix(pack_bar)
    try:
        fileobj.write(b"IBImport ")
        for data, length in ((txt_source, txt_len), (io.BytesIO(json_bytes), len(json_bytes))):
            key = random.randint(1, 255)
            table = bytes(i ^ key for i in range(256))  # 快速 XOR 映射表（C 速度）
            fileobj.write(encode_varint(length))
            fileobj.write(bytes([key]))
            while True:
                chunk = data.read(1 << 20)
                if not chunk:
                    break
                fileobj.write(chunk.translate(table))
                if pack_bar is not None:
                    pack_bar.update(len(chunk))
                    _refresh_memory_postfix(pack_bar)
    finally:
        if pack_bar is not None:
            pack_bar.close()
        if close_out:
            fileobj.close()
    return output


def schematic_to_ibi(
    source: Source,
    output: Output,
    *,
    progress: bool = False,
    strip_states: frozenset | None = None,
    workers: int | None = None,
) -> Output:
    """流式把 schem/schematic 写为 IBI 包（不建 Building 模型）。

    setblock 文本段流式写入临时文件，再按 1MB 块流式 XOR 加密写出，
    避免把上千万行文本整体放进内存。
    """
    import base64
    import random
    import tempfile

    from .writers.ibi import encode_varint

    src = SchematicSource(source, strip_states=strip_states)
    # 自动判断：workers 显式给（-t）→ 强制并行；否则非空气方块达阈值才并行
    use_parallel = workers is not None or _count_non_air(src) >= PARALLEL_THRESHOLD

    # 阶段 1：setblock 文本流式写入临时文件（避免列表累积）
    if use_parallel:
        n_workers = _parallel_worker_count(workers)
        worker_files = _generate_txt_parallel(src, src._entries, n_workers, progress)
        txt_source = _MultiFile(worker_files)
        tmp_files_to_clean = worker_files
    else:
        bar = None
        if progress:
            from tqdm.rich import tqdm

            counts = np.bincount(src.array3.ravel())
            total_blocks = int(src.array3.size - counts[src.air])
            bar = tqdm(total=total_blocks, desc="生成 setblock", unit="个")
            _refresh_memory_postfix(bar)

        tmp = tempfile.TemporaryFile()
        tw = io.TextIOWrapper(tmp, encoding="utf-8")
        first = True
        n = 0
        try:
            for x, y, z, name, state_str in src.iter_all_blocks():
                coord = f"~{x} ~{y} ~{z}"
                tw.write(
                    ("" if first else "\n")
                    + (f"setblock {coord} {name} [{state_str}]" if state_str
                       else f"setblock {coord} {name}")
                )
                first = False
                if bar is not None:
                    bar.update(1)
                    n += 1
                    if n % 10000 == 0:  # 节流内存刷新，避免每方块渲染刷屏
                        _refresh_memory_postfix(bar)
            tw.flush()
        finally:
            if bar is not None:
                _refresh_memory_postfix(bar)
                bar.close()
        txt_source = tmp
        tmp_files_to_clean = None

    # 阶段 2：命令方块 JSON（小）
    json_content = [
        {
            "posX": f"~{cb.x}",
            "posY": f"~{cb.y}",
            "posZ": f"~{cb.z}",
            "CommandMessage": base64.b64encode(cb.command.encode("utf-8")).decode("utf-8"),
            "Commandtitle": base64.b64encode(str(i).encode("utf-8")).decode("utf-8"),
            "mode": cb.mode,
            "isTime": cb.tick_delay,
            "Conditional": cb.conditional,
            "isRedstone": cb.needs_redstone,
        }
        for i, cb in enumerate(src.command_blocks(), start=1)
    ]
    json_bytes = json.dumps(json_content, ensure_ascii=False, indent=4).encode("utf-8")

    # 阶段 3：打包加密（IBImport + 两段 XOR，流式写出）
    if tmp_files_to_clean is not None:
        txt_len = sum(os.path.getsize(f) for f in tmp_files_to_clean)
    else:
        tmp.seek(0, 2)
        txt_len = tmp.tell()
        tmp.seek(0)
    try:
        return _pack_ibi(output, txt_source, txt_len, json_bytes, progress)
    finally:
        if tmp_files_to_clean is not None:
            txt_source.close()
            for f in tmp_files_to_clean:
                try:
                    os.remove(f)
                except OSError:
                    pass
        else:
            tmp.close()


# ---------------------------------------------------------------------------
# 共享：区块循环（tp + fill/setblock 逐行）
# ---------------------------------------------------------------------------
def _iter_chunk_lines(
    chunks,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
    total_blocks: int | None,
    get_region: Callable,
) -> Iterator[str]:
    """按 S 序遍历区块，逐行产出 tp 与 fill/setblock 命令。

    ``total_blocks`` 为 None 时进度条不显示总进度（只计已转换数，供世界导出等
    无法预先统计的场景）。
    """
    bar = None
    chunk_bar = None
    try:
        if progress:
            from tqdm.rich import tqdm

            bar = tqdm(total=total_blocks, desc="转换方块", unit="个")
            _refresh_memory_postfix(bar)
            chunk_bar = tqdm(total=len(chunks), desc="区块化", unit="块")

        prev_x = prev_z = 0
        for sx, ex, sz, ez in _s_sort(chunks, chunk_size):
            if chunk_bar is not None:
                chunk_bar.update(1)  # 区块进度（含空区块）
            region = get_region(sx, ex, sz, ez)
            if not region:
                continue
            if bar is not None:
                bar.update(len(region))
                _refresh_memory_postfix(bar)
            rel_x, rel_z = sx - prev_x, sz - prev_z
            yield f"tp ~{rel_x} ~ ~{rel_z}"
            prev_x, prev_z = sx, sz
            fill_cmds, setblock_cmds = _optimize_region(region, fill_merge)
            for cmd in sorted(fill_cmds + setblock_cmds, key=_y_sort_key):
                yield cmd
    finally:
        if chunk_bar is not None:
            chunk_bar.close()
        if bar is not None:
            bar.close()


# ---------------------------------------------------------------------------
# Sponge .schem（Palette/BlockData/Offset）
# ---------------------------------------------------------------------------
def _iter_sponge_lines(
    schem,
    *,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
) -> Iterator[str]:
    W, H, L = int(schem["Width"]), int(schem["Height"]), int(schem["Length"])
    ox, oy, oz = (int(v) for v in schem.get("Offset", [0, 0, 0]))
    parsed = {
        int(idx): _parse_blockstate(blockstate)
        for blockstate, idx in schem["Palette"].items()
    }
    # 预计算：索引 -> (name, 格式化状态串)，迭代时零函数调用
    parsed_str = {
        idx: (name, format_block_states(states, strip_states) if states else "")
        for idx, (name, states) in parsed.items()
    }
    air = next((i for i, (n, _) in parsed.items() if not n or n == "air"), None)

    bd = _as_uint8(schem["BlockData"])
    total = W * H * L
    if bd.size == 0 or total <= 0:
        return
    bd = bd[:total]
    if bd.size < total:
        bd = np.pad(bd, (0, total - bd.size), constant_values=air if air is not None else 0)
    bd3 = bd.reshape(H, L, W)

    if air is None:
        anycol = np.ones((L, W), dtype=bool)
    else:
        anycol = np.any(bd3 != air, axis=0)
    zs, xs = np.nonzero(anycol)
    if len(zs) == 0:
        return
    min_x, max_x = int(xs.min()) + ox, int(xs.max()) + ox
    min_z, max_z = int(zs.min()) + oz, int(zs.max()) + oz
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)
    # 进度条总数 = 非空气**方块数**（不是列数）；bincount 内存友好
    total_blocks = 0
    if progress:
        counts = np.bincount(bd)
        total_blocks = int(bd.size - counts[air]) if air is not None else int(bd.size)

    def get_region(sx, ex, sz, ez):
        rx0, rx1 = max(sx - ox, 0), min(ex - ox, W - 1)
        rz0, rz1 = max(sz - oz, 0), min(ez - oz, L - 1)
        if rx0 > rx1 or rz0 > rz1:
            return {}
        col = bd3[:, rz0:rz1 + 1, rx0:rx1 + 1]
        if air is None:
            mask = np.ones(col.shape, dtype=bool)
        else:
            mask = col != air
        ys, zs, xs = np.nonzero(mask)
        if len(ys) == 0:
            return {}
        vals = col[ys, zs, xs]
        region = {}
        for y, zl, xl, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
            item = parsed_str.get(v)
            if item is None:
                continue
            name, state_str = item
            region[(xl, y + oy, zl)] = (name, state_str)
        return region

    yield from _iter_chunk_lines(
        chunks, chunk_size, fill_merge, strip_states, progress, total_blocks, get_region
    )


# ---------------------------------------------------------------------------
# SchematicSource：统一流式方块源（不建 Building 模型）
# ---------------------------------------------------------------------------
class SchematicSource:
    """流式方块源：解析 Sponge .schem 或经典 .schematic，提供逐块迭代。

    - ``array3``: ``(H, L, W)`` 数组 —— Sponge 存调色板索引(uint8)，经典存
      ``id*16+data`` 组合键(uint16)。
    - ``parsed``: ``{索引: (方块名, 状态)}``。
    - ``iter_blocks`` / ``iter_all_blocks``: 产出 ``(绝对x, y, 绝对z, name, 格式化状态串)``。
    - ``command_blocks``: 从 BlockEntities / TileEntities 产出 CommandBlock。
    内存 = 方块数组 + 调色板，与方块数无关。
    """

    def __init__(self, source: Source, strip_states: frozenset | None = None):
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            with open(source, "rb") as f:
                data = f.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        root = nbtlib.File.parse(io.BytesIO(data), byteorder="big")
        if "Palette" in root:
            self._init_sponge(root)
        else:
            self._init_classic(root)
        # 预计算：array 值 -> (name, 格式化状态串)，迭代时零函数调用（优化热点）
        self._entries = {
            idx: (name, format_block_states(states, strip_states) if states else "")
            for idx, (name, states) in self.parsed.items()
        }

    # ---------------- 两种格式初始化 ----------------
    def _init_sponge(self, root):
        self.W, self.H, self.L = (
            int(root["Width"]), int(root["Height"]), int(root["Length"]),
        )
        self.ox, self.oy, self.oz = (int(v) for v in root.get("Offset", [0, 0, 0]))
        self.parsed = {
            int(idx): _parse_blockstate(bs) for bs, idx in root["Palette"].items()
        }
        self.air = next((i for i, (n, _) in self.parsed.items() if not n or n == "air"), None)
        bd = _as_uint8(root["BlockData"])
        total = self.W * self.H * self.L
        bd = bd[:total]
        if bd.size < total:
            bd = np.pad(bd, (0, total - bd.size),
                        constant_values=self.air if self.air is not None else 0)
        self.array3 = bd.reshape(self.H, self.L, self.W)
        self._entities = root.get("BlockEntities")

    def _init_classic(self, root):
        self.W, self.H, self.L = (
            int(root["Width"]), int(root["Height"]), int(root["Length"]),
        )
        self.ox = self.oy = self.oz = 0
        self.parsed = {}
        for key, spec in load_schematic_table().items():
            try:
                self.parsed[int(key)] = _parse_spec(spec)
            except (TypeError, ValueError):
                continue
        self.air = 0
        blocks = _as_uint8(root["Blocks"])
        data = _as_uint8(root["Data"]) if "Data" in root else None
        total = self.W * self.H * self.L

        blocks = blocks[:total]
        if blocks.size < total:
            blocks = np.pad(blocks, (0, total - blocks.size), constant_values=0)
        combined = blocks.astype(np.uint16)  # id*16+data 组合键
        combined <<= 4
        if data is not None:
            data = data[:total]
            if data.size < total:
                data = np.pad(data, (0, total - data.size), constant_values=0)
            combined |= data.astype(np.uint16)
        self.array3 = combined.reshape(self.H, self.L, self.W)
        self._entities = root.get("TileEntities")

    # ---------------- 迭代 ----------------
    @property
    def size(self):
        return (self.W, self.H, self.L)

    def _region(self, x0, x1, z0, z1):
        return self.array3[:, z0:z1 + 1, x0:x1 + 1]

    def iter_blocks(self, x0=None, x1=None, z0=None, z1=None):
        """yield (绝对x, y, 绝对z, name, 格式化状态串)；可限 x/z 范围。"""
        if x0 is None:
            x0, x1 = 0, self.W - 1
        if z0 is None:
            z0, z1 = 0, self.L - 1
        col = self._region(x0, x1, z0, z1)
        mask = col != self.air
        ys, zs, xs = np.nonzero(mask)
        vals = col[ys, zs, xs]  # 批量取值，避免逐格 numpy 标量
        for y, zl, xl, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
            item = self._entries.get(v)
            if item is None:
                continue
            yield (x0 + xl + self.ox, y + self.oy, z0 + zl + self.oz, item[0], item[1])

    def iter_all_blocks(self):
        """yield (绝对x, y, 绝对z, name, 格式化状态串)；按 y 分块迭代。"""
        for y0 in range(0, self.H, 64):
            col = self.array3[y0:y0 + 64, :, :]
            mask = col != self.air
            ys, zs, xs = np.nonzero(mask)
            vals = col[ys, zs, xs]  # 批量取值，避免逐格 numpy 标量
            for y, z, x, v in zip(ys.tolist(), zs.tolist(), xs.tolist(), vals.tolist()):
                item = self._entries.get(v)
                if item is None:
                    continue
                yield (x + self.ox, y0 + y + self.oy, z + self.oz, item[0], item[1])

    def command_blocks(self):
        """yield CommandBlock（从 BlockEntities/TileEntities）。"""
        if self._entities is None:
            return
        for be_tag in self._entities:
            be = tag_to_python(be_tag)
            pos = be.get("Pos")
            if pos is not None:
                x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
            elif "x" in be:
                x, y, z = int(be["x"]), int(be["y"]), int(be["z"])
            else:
                continue
            rx, ry, rz = x - self.ox, y - self.oy, z - self.oz
            if not (0 <= rx < self.W and 0 <= ry < self.H and 0 <= rz < self.L):
                continue
            item = self.parsed.get(int(self.array3[ry, rz, rx]))
            if item is None:
                continue
            name = item[0]
            if name not in COMMAND_BLOCK_MODES:
                continue
            yield _command_block_from_entity(
                x=rx + self.ox, y=ry + self.oy, z=rz + self.oz,
                name=name, states=item[1], be=be,
            )


# ---------------------------------------------------------------------------
# 流式 mcstructure / schem（直接构建调色板 + block_data，不建 Building 模型）
# ---------------------------------------------------------------------------
def _as_source(source):
    """路径/字节 → SchematicSource；已是 source 对象（WorldSource 等）则原样返回。

    WorldSource 提供 array3/parsed/air/ox/oy/oz/command_blocks 等鸭子类型属性，
    因此 schem→mcstructure/schem 的流式函数可直接复用于世界导出。
    """
    if isinstance(source, (str, os.PathLike, bytes, bytearray)):
        return SchematicSource(source)
    return source


def _trimmed_source(src):
    """裁切到实际方块范围，返回调色板信息。

    返回 ``(min_coords, size, palette, lookup, region)``：
    - ``palette``: ``[(name, states), ...]``（不含空气）
    - ``lookup``: array 值 -> 调色板索引（-1 为空气/未映射）
    - ``region``: 裁切后的 array3 ``(H2, L2, W2)``
    空建筑返回 None。
    """
    arr = src.array3
    air = src.air
    anyz = np.any(arr != air, axis=0)
    zs, xs = np.nonzero(anyz)
    if len(zs) == 0:
        return None
    anyy = np.any(arr != air, axis=(1, 2))
    ys = np.nonzero(anyy)[0]
    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    size = (max_x - min_x + 1, max_y - min_y + 1, max_z - min_z + 1)

    palette: list[tuple[str, dict]] = []
    palette_map: dict[tuple, int] = {}
    lookup = np.full(4096, -1, dtype=np.int32)  # 覆盖经典 uint16 组合键
    counts = np.bincount(arr.ravel())
    for v in np.nonzero(counts)[0]:
        v = int(v)
        if v == air:
            continue
        item = src.parsed.get(v)
        if item is None:
            continue
        key = (item[0], tuple(sorted(item[1].items())))
        if key not in palette_map:
            palette_map[key] = len(palette)
            palette.append((item[0], dict(item[1])))
        lookup[v] = palette_map[key]

    region = arr[min_y:max_y + 1, min_z:max_z + 1, min_x:max_x + 1]
    return (min_x, min_y, min_z), size, palette, lookup, region


def _states_to_nbt(states: dict):
    from nbtlib.tag import Byte, Compound, Int, String

    out = Compound()
    for key, val in states.items():
        if isinstance(val, bool):
            out[key] = Byte(int(val))
        elif isinstance(val, int):
            out[key] = Int(val)
        else:
            out[key] = String(str(val))
    return out


def schematic_to_mcstructure(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 写为 .mcstructure（不建 Building 模型）。

    ``source`` 可以是路径/字节（自动构建 SchematicSource），也可以是已构造的
    方块源对象（如 WorldSource，见 :func:`_as_source`）。
    """
    from nbtlib.tag import Compound, Int, IntArray, List, Short, String

    src = _as_source(source)
    trimmed = _trimmed_source(src)
    if trimmed is None:
        return _write_output(output, _empty_mcstructure())
    (min_x, min_y, min_z), (W2, H2, L2), palette, lookup, region = trimmed
    ox, oy, oz = src.ox + min_x, src.oy + min_y, src.oz + min_z
    n_cells = W2 * H2 * L2

    # mcstructure 索引：z + y*Z + x*Y*Z（z 最快）=> transpose(2,0,1) 后 ravel
    block_data = lookup[region.transpose(2, 0, 1).ravel()].astype(np.int32)

    palette_tags = []
    for name, states in palette:
        val = states.get("facing_direction", 0) if name in COMMAND_BLOCK_MODES else 0
        palette_tags.append(
            Compound({
                "name": String(f"minecraft:{name}"),
                "states": _states_to_nbt(states),
                "val": Short(val),
                "version": Int(18090528),
            })
        )

    position_data = Compound()
    for cb in src.command_blocks():
        rx, ry, rz = cb.x - ox, cb.y - oy, cb.z - oz
        idx = rz + ry * L2 + rx * (H2 * L2)
        position_data[str(idx)] = Compound({"block_entity_data": _mcstructure_entity(cb)})

    structure = Compound({
        "format_version": Int(1),
        "size": IntArray([W2, H2, L2]),
        "structure_world_origin": IntArray([ox, oy, oz]),
        "structure": Compound({
            "palette": Compound({
                "default": Compound({
                    "block_palette": List(palette_tags),
                    "block_position_data": position_data,
                }),
            }),
            "block_indices": List([
                IntArray(block_data),
                IntArray(np.full(n_cells, -1, dtype=np.int32)),
            ]),
        }),
    })
    buf = io.BytesIO()
    nbtlib.File(structure).write(buf, byteorder="little")
    return _write_output(output, buf.getvalue())


def schematic_to_schem(source: Source, output: Output, *, progress: bool = False) -> Output:
    """流式把 schem/schematic 写为 Sponge .schem（不建 Building 模型）。

    ``source`` 可以是路径/字节（自动构建 SchematicSource），也可以是已构造的
    方块源对象（如 WorldSource，见 :func:`_as_source`）。
    """
    from nbtlib.tag import ByteArray, Compound, Int, IntArray, List, String, Byte

    src = _as_source(source)
    trimmed = _trimmed_source(src)
    if trimmed is None:
        return _write_output(output, _empty_schem())
    (min_x, min_y, min_z), (W2, H2, L2), palette, lookup, region = trimmed
    ox, oy, oz = src.ox + min_x, src.oy + min_y, src.oz + min_z

    # Sponge BlockData 索引：x + z*W + y*W*L（x 最快）=> region.ravel()（x 最快）
    # Sponge 调色板索引 0 固定为 air：air 单元 -> 0，其余索引 +1
    if len(palette) + 1 > 256:
        raise ValueError("方块种类超过 Sponge .schem 调色板上限 256")
    block_data = lookup[region.ravel()]
    block_data = np.where(block_data == -1, 0, block_data + 1)
    # Sponge 用字节存索引（0-255），>127 转补码
    block_int8 = block_data.astype(np.int8)

    palette_compound = Compound({"minecraft:air": Int(0)})
    for idx, (name, states) in enumerate(palette, start=1):
        palette_compound[_format_blockstate(name, states)] = Int(idx)

    entities = [_schem_entity(cb) for cb in src.command_blocks()]

    root = Compound({
        "Version": Int(2),
        "DataVersion": Int(2975),
        "Width": Int(W2),
        "Height": Int(H2),
        "Length": Int(L2),
        "Offset": IntArray([ox, oy, oz]),
        "Palette": palette_compound,
        "BlockData": ByteArray(block_int8),
        "BlockEntities": List(entities),
        "Entities": List([]),
    })
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        nbtlib.File(root).write(gz, byteorder="big")
    return _write_output(output, buf.getvalue())


def _format_blockstate(name: str, states: dict) -> str:
    """(方块名, 模型状态) -> Sponge 方块状态串（Bedrock->Java）。"""
    from .utils import bedrock_to_java_states

    java = bedrock_to_java_states(states)
    s = f"minecraft:{name}"
    if java:
        parts = []
        for key, val in java.items():
            val_str = "true" if isinstance(val, bool) and val else (
                "false" if isinstance(val, bool) else str(val))
            parts.append(f"{key}={val_str}")
        s += "[" + ",".join(parts) + "]"
    return s


def _mcstructure_entity(cb: CommandBlock):
    from nbtlib.tag import Byte, Compound, Int, String

    return Compound({
        "id": String("CommandBlock"),
        "x": Int(cb.x), "y": Int(cb.y), "z": Int(cb.z),
        "Command": String(cb.command),
        "CustomName": String(cb.custom_name),
        "auto": Byte(int(not cb.needs_redstone)),
        "conditionalMode": Byte(int(cb.conditional)),
        "TickDelay": Int(cb.tick_delay),
        "TrackOutput": Byte(int(cb.track_output)),
    })


def _schem_entity(cb: CommandBlock):
    from nbtlib.tag import Byte, Compound, Int, String

    return Compound({
        "id": String(f"minecraft:{COMMAND_BLOCK_IDS[cb.mode]}"),
        "x": Int(cb.x), "y": Int(cb.y), "z": Int(cb.z),
        "Command": String(cb.command),
        "CustomName": String(cb.custom_name),
        "auto": Byte(int(not cb.needs_redstone)),
        "TrackOutput": Byte(int(cb.track_output)),
        "conditionMet": Byte(0),
    })


def _empty_mcstructure() -> bytes:
    from .writers.mcstructure import McStructureWriter
    from .model import Building

    return McStructureWriter().render(Building())


def _empty_schem() -> bytes:
    from .writers.schem import SchemWriter
    from .model import Building

    return SchemWriter().render(Building())


def _command_block_from_entity(x, y, z, name, states, be) -> CommandBlock:
    """方块名 + BlockEntity NBT -> CommandBlock（兼容 Sponge/经典两种 NBT）。"""
    auto = bool(be.get("auto", False))
    return CommandBlock(
        x=x, y=y, z=z,
        mode=COMMAND_BLOCK_MODES.get(name, MODE_IMPULSE),
        command=str(be.get("Command", "") or ""),
        custom_name=str(be.get("CustomName", "") or ""),
        tick_delay=int(be.get("TickDelay", 0) or 0),
        conditional=bool(states.get("conditional_bit", False)),
        needs_redstone=not auto,
        execute_on_first_tick=bool(be.get("ExecuteOnFirstTick", False)),
        track_output=bool(be.get("TrackOutput", True)),
    )


# ---------------------------------------------------------------------------
# 经典 .schematic（Blocks/Data + 映射表）
# ---------------------------------------------------------------------------
def _iter_classic_lines(
    schem,
    *,
    chunk_size: int,
    fill_merge: bool,
    strip_states: frozenset | None,
    progress: bool,
) -> Iterator[str]:
    X, Y, Z = int(schem["Width"]), int(schem["Height"]), int(schem["Length"])
    total = X * Y * Z
    if total <= 0:
        return

    blocks = _as_uint8(schem["Blocks"])
    data = _as_uint8(schem["Data"]) if "Data" in schem else None

    def _pad_arr(arr):
        arr = arr[:total]
        if arr.size < total:
            arr = np.pad(arr, (0, total - arr.size), constant_values=0)
        return arr.reshape(Y, Z, X)

    blocks3 = _pad_arr(blocks)
    data3 = _pad_arr(data) if data is not None else None

    # 预计算映射表：id*16+data -> (方块名, 格式化状态串)
    parsed = {}
    parsed_str = {}
    for key, spec in load_schematic_table().items():
        try:
            name, states = _parse_spec(spec)
        except (TypeError, ValueError):
            continue
        key = int(key)
        parsed[key] = (name, states)
        parsed_str[key] = (name, format_block_states(states, strip_states) if states else "")

    anycol = np.any(blocks3 != 0, axis=0)
    zs, xs = np.nonzero(anycol)
    if len(zs) == 0:
        return
    min_x, max_x = int(xs.min()), int(xs.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    chunks = _divide_chunks(min_x, max_x, min_z, max_z, chunk_size)
    # 进度条总数 = 非空气**方块数**（不是列数）
    total_blocks = 0
    if progress:
        counts = np.bincount(blocks)
        total_blocks = int(blocks.size - counts[0])

    def get_region(sx, ex, sz, ez):
        rx0, rx1 = max(sx, 0), min(ex, X - 1)
        rz0, rz1 = max(sz, 0), min(ez, Z - 1)
        if rx0 > rx1 or rz0 > rz1:
            return {}
        colb = blocks3[:, rz0:rz1 + 1, rx0:rx1 + 1]
        mask = colb != 0
        ys, zs, xs = np.nonzero(mask)
        if len(ys) == 0:
            return {}
        cold = data3[:, rz0:rz1 + 1, rx0:rx1 + 1] if data3 is not None else None
        region = {}
        for y, zl, xl in zip(ys.tolist(), zs.tolist(), xs.tolist()):
            bid = int(colb[y, zl, xl]) & 0xFF
            dval = int(cold[y, zl, xl]) & 0xFF if cold is not None else 0
            item = parsed_str.get((bid << 4) | dval)
            if item is None:
                continue
            name, state_str = item
            region[(xl, y, zl)] = (name, state_str)
        return region

    yield from _iter_chunk_lines(
        chunks, chunk_size, fill_merge, strip_states, progress, total_blocks, get_region
    )
