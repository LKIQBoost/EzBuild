"""极简 LevelDB 读取器（Minecraft Bedrock 存档 ``db/*.ldb``），流式按需读块。

Bedrock 的 LevelDB 是 Mojang 自研 fork，块格式：
- ``.ldb``（SSTable）：数据块 + 元索引块 + 索引块 + footer；``handle.size`` 是
  **内容长度**（不含 5 字节尾），块尾为 ``[内容][type:1][crc:4]``。
- 内部键 = ``用户键 + [type:1][seq:7]``（``seq<<8|type`` 存小端 u64），
  类型字节在后缀第 1 位（``k[-8]``），末字节是 seq 高位。
- 块压缩：Bedrock fork 枚举 ``0=无、1=snappy、2=zlib、4=raw deflate``。

内存设计：只解析各 ``.ldb`` 的索引块（小），数据块**按需读取**（LRU 缓存），
``collect(prefix)`` 只返回指定 chunk 的键值。内存 ≈ 当前 chunk，与整库大小无关。
"""

from __future__ import annotations

import bisect
import os
import re
import struct
import zlib
from collections import OrderedDict
from pathlib import Path

import numpy as np

# LevelDB footer 魔数（0xdb4775248b80fb57 小端）
_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
# 网易（NetEase）加密存档：每个加密文件带 4 字节头 [0x80, 0x1D, 0x30, 0x01]，
# 其余字节用 8 字节 key 连续 XOR（从数据起点逐字节取 key 模 8）。
_NE_HEADER = b"\x80\x1d\x30\x01"


def _xor_block(data: bytes, key: bytes, start_pos: int) -> bytes:
    """用 key（循环）从 ``start_pos`` 开始对 ``data`` 逐字节 XOR（向量化）。

    ``start_pos`` 是 data 首字节在**加密数据区**（去掉 4 字节头）内的绝对偏移，
    key 偏移 = ``(start_pos + i) % len(key)``。
    """
    if not key:
        return data
    arr = np.frombuffer(data, dtype=np.uint8)
    pos = (np.arange(start_pos, start_pos + len(data)) % len(key))
    key_arr = np.frombuffer(key, dtype=np.uint8)[pos]
    return np.bitwise_xor(arr, key_arr).tobytes()


def decrypt_world_folder(src, dst) -> tuple[bytes | None, int, int]:
    """把网易加密存档解密为普通存档，返回 ``(key, 解密文件数, 原样复制数)``。

    复制整个存档文件夹（level.dat / db / 其它）；db 内带网易头
    ``[0x80,0x1D,0x30,0x01]`` 的文件去头 + XOR 解密，其余原样复制。
    非网易存档（key 为 None）同样整夹复制，只是不解密任何文件。
    """
    import shutil

    src = Path(src)
    dst = Path(dst)
    key = _netease_key(src / "db")
    dst.mkdir(parents=True, exist_ok=True)
    n_dec = 0
    n_copy = 0
    for item in src.iterdir():
        if item.is_dir():
            target = dst / item.name
            if item.name == "db" and key:
                target.mkdir(parents=True, exist_ok=True)
                for f in item.iterdir():
                    if f.is_dir():
                        shutil.copytree(f, target / f.name, dirs_exist_ok=True)
                        continue
                    data = f.read_bytes()
                    if data.startswith(_NE_HEADER):
                        (target / f.name).write_bytes(_xor_block(data[len(_NE_HEADER):], key, 0))
                        n_dec += 1
                    else:
                        shutil.copy2(f, target / f.name)
                        n_copy += 1
            else:
                shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst / item.name)
    return key, n_dec, n_copy


def _netease_key(db_dir) -> bytes | None:
    """检测并推导网易加密 key；非网易存档返回 None。

    key 从加密的 CURRENT 文件推导：CURRENT 明文 = ``MANIFEST-XXXX\\n``，
    key_derived = 加密数据 XOR 明文（即 XOR key 循环 8 字节，前后 8 相同则取前 8）。
    """
    db_dir = Path(db_dir)
    try:
        cur = (db_dir / "CURRENT").read_bytes()
    except OSError:
        return None
    if not cur.startswith(_NE_HEADER):
        return None
    enc_cur = cur[len(_NE_HEADER):]
    manifests = sorted(db_dir.glob("MANIFEST-*"), key=lambda p: p.name)
    for m in manifests:
        name = m.name.encode("utf-8")
        source = name + b"\x0a"
        kd = bytes(enc_cur[i] ^ source[i % len(source)] for i in range(len(enc_cur)))
        if len(kd) >= 16 and kd[:8] == kd[8:16]:
            return kd[:8]
    return None
# 日志块大小
_LOG_BLOCK = 32768
# 数据块尾 = type(1) + crc(4)
_BLOCK_TRAILER = 5
# 数据块 LRU 缓存上限
_MAX_CACHED_BLOCKS = 128


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """读 LEB128 varint，返回 (值, 下一个位置)。"""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_handle(buf: bytes, pos: int) -> tuple[int, int, int]:
    """BlockHandle = varint64(offset) + varint64(size)，返回 (offset, size, pos)。"""
    offset, pos = _read_varint(buf, pos)
    size, pos = _read_varint(buf, pos)
    return offset, size, pos


def _decompress(data: bytes, btype: int) -> bytes | None:
    """按 Bedrock LevelDB 块压缩类型解压（0=无、2=zlib、4=raw deflate）。"""
    if btype == 0:
        return data
    if btype == 2:
        try:
            return zlib.decompress(data)
        except Exception:
            return None
    if btype == 4:  # raw deflate（Bedrock 常用，无 zlib 头）
        try:
            return zlib.decompress(data, -15)
        except Exception:
            return None
    return None  # 1 = snappy 等未支持


def _iter_entries(data: bytes):
    """解析数据块条目，yield (键[含内部后缀], 值)。"""
    n = len(data)
    if n < 4:
        return
    num_restarts = struct.unpack("<I", data[n - 4:n])[0]
    end = n - 4 - num_restarts * 4
    pos = 0
    prev_key = b""
    while pos < end:
        try:
            shared, pos = _read_varint(data, pos)
            non_shared, pos = _read_varint(data, pos)
            value_len, pos = _read_varint(data, pos)
        except IndexError:
            break
        if shared > len(prev_key) or pos + non_shared + value_len > end:
            break
        key = prev_key[:shared] + data[pos:pos + non_shared]
        pos += non_shared
        value = data[pos:pos + value_len]
        pos += value_len
        prev_key = key
        yield key, value


def _file_num(name: str) -> int:
    m = re.match(r"(\d+)\.", name)
    return int(m.group(1)) if m else 0


class _LdbFile:
    """单个 .ldb：解析 footer + 索引块，按需读数据块。"""

    __slots__ = ("path", "handles", "keys", "true_min", "max_key", "_f", "_ne_key", "_ne_off")

    def __init__(self, path, ne_key: bytes | None = None):
        self.path = path
        self.handles: list[tuple[bytes, int, int]] = []  # [(索引键, offset, size)]
        self.keys: list[bytes] = []  # 索引键列表（缓存，避免每次 bisect 重建）
        self.true_min = b""  # 文件真实最小内部键（首块首条）
        self.max_key = b""   # 文件最大内部键（末块末条）
        self._f = None
        # 网易加密：文件带 4 字节头 → 数据区从第 4 字节起连续 XOR
        self._ne_key = None
        self._ne_off = 0
        if ne_key:
            try:
                with open(self.path, "rb") as f:
                    head = f.read(4)
                if head == _NE_HEADER:
                    self._ne_key = ne_key
                    self._ne_off = len(_NE_HEADER)
            except OSError:
                pass
        self._parse()

    def _parse(self):
        with open(self.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < 48 + self._ne_off:
                return
            f.seek(size - 48)
            footer = f.read(48)
            if self._ne_key:
                footer = _xor_block(footer, self._ne_key, size - 48 - self._ne_off)
            if len(footer) < 48 or footer[40:48] != _MAGIC:
                return
            _meta_off, _meta_size, pos = _parse_handle(footer, 0)
            idx_off, idx_size, _ = _parse_handle(footer, pos)
            content = self._read_block_at(f, idx_off, idx_size)
            if content is None:
                return
            for k, v in _iter_entries(content):
                off, sz, _ = _parse_handle(v, 0)
                self.handles.append((k, off, sz))
            if self.handles:
                self.keys = [h[0] for h in self.handles]
                self.max_key = self.handles[-1][0]
                # 文件真实最小键 = 首数据块的首条键（读一次，用于精确跳过无关文件）
                first = next(_iter_entries(self._read_block_at(f, self.handles[0][1], self.handles[0][2]) or b""), None)
                if first is not None:
                    self.true_min = first[0]

    def _read_block_at(self, f, offset: int, size: int) -> bytes | None:
        # 网易加密：footer handle 的 offset 相对"标准内容"（去掉 4 字节头），
        # 原始文件位置 = offset + 4；XOR 相位用标准 offset。
        read_off = offset + self._ne_off if self._ne_key else offset
        f.seek(read_off)
        raw = f.read(size + _BLOCK_TRAILER)
        if len(raw) < size + _BLOCK_TRAILER:
            return None
        if self._ne_key:
            raw = _xor_block(raw, self._ne_key, offset)
        return _decompress(raw[:size], raw[size])

    def read_block(self, offset: int, size: int) -> bytes | None:
        if self._f is None:
            self._f = open(self.path, "rb")
        return self._read_block_at(self._f, offset, size)

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None


class LevelDB:
    """Bedrock ``db/`` 目录的流式读取视图。

    ``collect(prefix)``：收集用户键以 ``prefix``（8 字节 chunk 坐标）开头的键值，
    多文件按新旧合并（后写覆盖、删除生效）。只读需要的数据块，内存有界。
    """

    def __init__(self, db_dir):
        db_dir = Path(db_dir)
        self._ne_key = _netease_key(db_dir)  # 网易加密 key；非网易为 None
        self._files: list[_LdbFile] = []
        for path in sorted(db_dir.glob("*.ldb"), key=lambda p: _file_num(p.name)):
            f = _LdbFile(path, self._ne_key)
            if f.handles:
                self._files.append(f)
        self._cache: "OrderedDict[tuple[int, int], bytes]" = OrderedDict()
        # .log（WAL）——干净保存时通常为空；按需也读入（小）
        self._log: dict[bytes, bytes] = {}
        for path in sorted(db_dir.glob("*.log"), key=lambda p: _file_num(p.name)):
            entries, deleted = _read_log_filtered(path, self._ne_key)
            for k in deleted:
                self._log.pop(k, None)
            for k, v in entries.items():
                self._log[k] = v

    def _get_block(self, file_idx: int, offset: int, size: int) -> bytes | None:
        key = (file_idx, offset)
        c = self._cache.get(key)
        if c is not None:
            self._cache.move_to_end(key)
            return c
        c = self._files[file_idx].read_block(offset, size)
        if c is not None:
            self._cache[key] = c
            if len(self._cache) > _MAX_CACHED_BLOCKS:
                self._cache.popitem(last=False)
        return c

    def _overlapping_blocks(self, f: _LdbFile, lo: bytes, hi: bytes):
        """返回 f 中可能含 [lo, hi] 键的数据块 handle。

        块 i 覆盖 ``(index[i-1], index[i]]``；含 ≥lo 键的块是第一个
        ``index[p] >= lo`` 的块 p（它的区间下界 < lo），必须无条件读它。
        之后继续到 ``index[i] > hi_ext``（多读一块处理分隔键超过块内实际最大键）。
        """
        keys = f.keys
        hi_ext = hi + b"\xff" * 16
        p = bisect.bisect_left(keys, lo)
        if p >= len(keys):
            return []
        out = [f.handles[p]]
        for i in range(p + 1, len(keys)):
            out.append(f.handles[i])
            if keys[i] > hi_ext:
                if i + 1 < len(keys):
                    out.append(f.handles[i + 1])  # 分隔键可能超过块内实际最大键
                break
        return out

    def collect(self, prefix: bytes) -> dict[bytes, bytes]:
        """返回用户键以 ``prefix`` 开头的键值（最新覆盖）。"""
        out: dict[bytes, bytes] = {}
        hi = prefix + b"\xff\xff"
        for idx, f in enumerate(self._files):
            # 文件键范围 [true_min, max_key] 与该 chunk 键范围 [prefix, hi] 不相交则跳过。
            # true_min/max_key 都是内部键，与用户键比较方向安全。
            if f.max_key < prefix or (f.true_min and f.true_min > hi):
                continue
            for _idx_key, off, size in self._overlapping_blocks(f, prefix, hi):
                content = self._get_block(idx, off, size)
                if content is None:
                    continue
                for k, v in _iter_entries(content):
                    if not k.startswith(prefix):
                        continue
                    if len(k) > len(prefix) + 2 + 8:
                        continue  # 用户键最多 prefix+2，再加 8 字节后缀
                    user = k[:-8]
                    if k[-8] == 0:  # kTypeDeletion
                        out.pop(user, None)
                    else:
                        out[user] = v
        if self._log:
            for k, v in self._log.items():
                if k.startswith(prefix) and len(k) <= len(prefix) + 2:
                    out[k] = v
        return out

    def close(self):
        for f in self._files:
            f.close()
        self._cache.clear()
        self._log.clear()


# ---------------------------------------------------------------------------
# .log（WAL）读取
# ---------------------------------------------------------------------------
def _read_log_filtered(path, ne_key: bytes | None = None) -> tuple[dict[bytes, bytes], set[bytes]]:
    """读一个 .log（WAL）文件，返回 (entries, deleted)。

    ``ne_key``：网易加密 key；若 .log 带网易头则先解密。
    """
    entries: dict[bytes, bytes] = {}
    deleted: set[bytes] = set()
    try:
        data = Path(path).read_bytes()
    except OSError:
        return entries, deleted
    if ne_key and data.startswith(_NE_HEADER):
        data = _xor_block(data[len(_NE_HEADER):], ne_key, 0)
    n = len(data)
    pos = 0
    frag = b""
    in_frag = False
    while pos + 7 <= n:
        length = struct.unpack("<H", data[pos + 4:pos + 6])[0]
        rtype = data[pos + 6]
        if rtype == 0:  # kZeroType：跳到下一块
            pos = (pos // _LOG_BLOCK + 1) * _LOG_BLOCK
            continue
        if length == 0 or pos + 7 + length > n:
            break
        record = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 1:  # kFullType
            _apply_batch(record, entries, deleted)
        elif rtype == 2:  # kFirstType
            frag = record
            in_frag = True
        elif rtype == 3 and in_frag:  # kMiddleType
            frag += record
        elif rtype == 4 and in_frag:  # kLastType
            frag += record
            _apply_batch(frag, entries, deleted)
            frag = b""
            in_frag = False
        elif rtype >= 5:
            break
    return entries, deleted


def _apply_batch(batch: bytes, entries: dict, deleted: set) -> None:
    """解析一条 WriteBatch 记录并应用到 dict。"""
    if len(batch) < 12:
        return
    count = struct.unpack("<I", batch[8:12])[0]
    pos = 12
    for _ in range(count):
        if pos >= len(batch):
            break
        btype = batch[pos]
        pos += 1
        try:
            key_len, pos = _read_varint(batch, pos)
            key = batch[pos:pos + key_len]
            pos += key_len
            if btype == 1:  # kTypeValue
                value_len, pos = _read_varint(batch, pos)
                value = batch[pos:pos + value_len]
                pos += value_len
                entries[key] = value
                deleted.discard(key)
            else:  # kTypeDeletion
                deleted.add(key)
                entries.pop(key, None)
        except IndexError:
            break
