"""极简 NBT 解析器：定长基本类型的大列表 / 数组走 numpy 批量读取。

用途：mcstructure 的 ``block_indices`` 是 ``TAG_List[TAG_Int]``（外部工具常这么写），
nbtlib 会为**每个元素**建一个 Python 对象——一个 27MB、347 万元素的文件要
~400MB 内存 / 6s。这里对定长类型用 ``np.frombuffer`` 一次性读出，降到数组本身
（13.9MB / 5ms）。

返回**普通** Python 结构（Compound→dict、List→list、标量→int/float/str、
ByteArray/IntArray/LongArray 及定长基本类型列表→np.ndarray），与本库其他
基于 nbtlib 的代码不同；调用方需按普通 dict/list 访问。

仅支持解析（无写出）；标签类型 0-12 全部支持。
"""

from __future__ import annotations

import struct

import numpy as np

# 定长标量标签：类型 → (字节数, struct/ndarray 类型码)
_FIXED = {1: (1, "b"), 2: (2, "h"), 3: (4, "i"), 4: (8, "q"), 5: (4, "f"), 6: (8, "d")}


def parse_nbt(buf: bytes, byteorder: str = "little"):
    """解析一个 NBT 根 Compound，返回普通 dict。

    ``byteorder``: ``"little"``（Bedrock 结构文件）或 ``"big"``（Java 结构文件）。
    """
    bo = "<" if byteorder == "little" else ">"
    pos = 0

    def read_string() -> str:
        nonlocal pos
        n = struct.unpack_from(bo + "H", buf, pos)[0]
        pos += 2
        s = buf[pos:pos + n].decode("utf-8", "replace")
        pos += n
        return s

    def read_value(tag: int):
        nonlocal pos
        if tag in _FIXED:
            size, code = _FIXED[tag]
            v = struct.unpack_from(bo + code, buf, pos)[0]
            pos += size
            return v
        if tag == 7:  # ByteArray
            n = struct.unpack_from(bo + "i", buf, pos)[0]
            pos += 4
            v = np.frombuffer(buf, np.int8, n, pos).copy()
            pos += n
            return v
        if tag == 8:  # String
            return read_string()
        if tag == 9:  # List
            elem = buf[pos]
            pos += 1
            n = struct.unpack_from(bo + "i", buf, pos)[0]
            pos += 4
            if n <= 0:
                return []
            if elem in _FIXED:  # 定长基本类型 → numpy 批量读取
                size, code = _FIXED[elem]
                arr = np.frombuffer(buf, np.dtype(bo + code), n, pos).copy()
                pos += n * size
                return arr
            return [read_value(elem) for _ in range(n)]
        if tag == 10:  # Compound
            out: dict = {}
            while True:
                t = buf[pos]
                pos += 1
                if t == 0:  # TAG_End
                    break
                name = read_string()
                out[name] = read_value(t)
            return out
        if tag == 11:  # IntArray
            n = struct.unpack_from(bo + "i", buf, pos)[0]
            pos += 4
            v = np.frombuffer(buf, np.dtype(bo + "i"), n, pos).copy()
            pos += n * 4
            return v
        if tag == 12:  # LongArray
            n = struct.unpack_from(bo + "i", buf, pos)[0]
            pos += 4
            v = np.frombuffer(buf, np.dtype(bo + "q"), n, pos).copy()
            pos += n * 8
            return v
        raise ValueError(f"不支持的 NBT 标签类型 {tag}")

    tag = buf[0]
    if tag != 10:
        raise ValueError(f"NBT 根标签不是 Compound（tag={tag}）")
    pos = 1
    read_string()  # 根名（通常为空）
    return read_value(tag)


def root_first_key(buf: bytes, byteorder: str = "little") -> str | None:
    """廉价读取根 Compound 名下第一个键名（不解析整个文件）；失败返回 None。

    用于按字节序快速判别结构文件格式（mcstructure 首键为 ``format_version``）。
    """
    bo = "<" if byteorder == "little" else ">"
    try:
        if buf[0] != 10:
            return None
        pos = 1
        n = struct.unpack_from(bo + "H", buf, pos)[0]
        pos += 2 + n  # 跳过根名
        tag = buf[pos]
        pos += 1
        if tag == 0:
            return None
        n = struct.unpack_from(bo + "H", buf, pos)[0]
        pos += 2
        if pos + n > len(buf):
            return None
        return buf[pos:pos + n].decode("utf-8")
    except (struct.error, IndexError, UnicodeDecodeError):
        return None
