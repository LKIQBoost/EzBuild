"""IBI 导入包输出器。

IBI = ``"IBImport "`` 头 + 依次打包（setblock 文本, 命令方块 JSON）：
每段数据 = varint(加密长度) + 1 字节密钥 + XOR 加密数据。

命令方块 JSON 为 IBI 内部格式（posX/posY/posZ + base64 命令/标题）。
"""

from __future__ import annotations

import base64
import json
import random

from ..model import Building, CommandBlock
from .base import Writer
from .setblock_txt import SetblockTxtWriter


class IbiWriter(Writer):
    format_name = "ibi"
    extensions = (".ibi",)
    description = "IBI 导入包（setblock 文本 + 命令方块 JSON 加密打包）"

    def render(self, building: Building) -> bytes:
        txt_bytes = SetblockTxtWriter().render(building).encode("utf-8")
        json_bytes = json.dumps(
            self._json_content(building), ensure_ascii=False, indent=4
        ).encode("utf-8")
        return pack_txt_and_json(txt_bytes, json_bytes)

    @staticmethod
    def _json_content(building: Building) -> list[dict]:
        """IBI 内部的命令方块 JSON（posX/posY/posZ + base64）。"""
        output: list[dict] = []
        for i, cb in enumerate(building.command_blocks, start=1):
            output.append(
                {
                    "posX": f"~{cb.x}",
                    "posY": f"~{cb.y}",
                    "posZ": f"~{cb.z}",
                    "CommandMessage": base64.b64encode(
                        cb.command.encode("utf-8")
                    ).decode("utf-8"),
                    "Commandtitle": base64.b64encode(
                        str(i).encode("utf-8")
                    ).decode("utf-8"),
                    "mode": cb.mode,
                    "isTime": cb.tick_delay,
                    "Conditional": cb.conditional,
                    "isRedstone": cb.needs_redstone,
                }
            )
        return output


# ---------------------------------------------------------------------------
# IBI 打包
# ---------------------------------------------------------------------------
def encode_varint(value: int) -> bytes:
    """无符号 varint 编码。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            break
    return bytes(out)


def _encrypt(data: bytes) -> bytes:
    """单段数据：varint(长度) + 密钥 + XOR 加密内容。"""
    key = random.randint(1, 255)
    encrypted = bytes(b ^ key for b in data)
    return encode_varint(len(encrypted)) + bytes([key]) + encrypted


def pack_txt_and_json(txt_bytes: bytes, json_bytes: bytes) -> bytes:
    """把 setblock 文本与命令方块 JSON 打包为完整 IBI 文件字节。"""
    return b"IBImport " + _encrypt(txt_bytes) + _encrypt(json_bytes)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取一个 varint，返回 (值, 下一个位置)。"""
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def decode_ibi(data: bytes) -> tuple[str, list[dict]]:
    """解码 IBI 文件字节，返回 (setblock 文本, 命令方块 JSON 列表)。

    逆向验证用；等价于把 IBI 还原成 txt + 命令方块 json 两份内容。
    """
    if data[:9] != b"IBImport ":
        raise ValueError(f"无效的 IBI 头: {data[:9]!r}")

    segments: list[bytes] = []
    pos = 9
    while pos < len(data):
        length, pos = decode_varint(data, pos)
        key = data[pos]
        pos += 1
        encrypted = data[pos : pos + length]
        pos += length
        segments.append(bytes(b ^ key for b in encrypted))

    if len(segments) < 2:
        raise ValueError("IBI 数据段不足")
    txt = segments[0].decode("utf-8")
    entries = json.loads(segments[1].decode("utf-8"))
    return txt, entries
