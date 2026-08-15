"""BDX 读取器：把 Minecraft 基岩版 .bdx 建筑文件解析为 Building。

完整处理 BDX 操作码（方块、命令方块、容器、NBT），
输出为中立模型，供所有 Writer 使用。

说明：BDX 解析算法参考 BDXConverter 项目
（原仓库作者：Happy2018new / Inotart / 凌云金羿，QQ群见 BDXConverter 官方仓库）。
"""

from __future__ import annotations

import base64
import io
import struct
from typing import Any, Optional

try:
    import brotli
except ImportError as e:  # pragma: no cover
    raise ImportError("读取 BDX 需要 brotli：pip install brotli") from e

try:
    import nbtlib

    HAS_NBTLIB = True
except ImportError:
    HAS_NBTLIB = False

from ..model import (
    MODE_CHAIN,
    MODE_IMPULSE,
    MODE_REPEAT,
    Block,
    Building,
    CommandBlock,
    COMMAND_BLOCK_IDS,
    COMMAND_BLOCK_MODES,
    COMMAND_BLOCK_NAMES,
)
from ..utils import load_runtime_id_mapping, parse_block_states_string, resolve_runtime_block
from .base import Reader, Source


# ---------------------------------------------------------------------------
# BDX 命令方块 mode 字段 → 内部 mode
#   说明：BDX 协议中 mode 字段约定 0=脉冲 1=重复 2=连锁。
#   （待与反编译的 bdx_to_dsb.py 核对，确认后可随时调整此映射）
# ---------------------------------------------------------------------------
_BDX_MODE_MAP: dict[int, int] = {
    0: MODE_IMPULSE,
    1: MODE_REPEAT,
    2: MODE_CHAIN,
}


# ---------------------------------------------------------------------------
# 二进制读取辅助
# ---------------------------------------------------------------------------
def _read_bytes(reader: io.BytesIO, length: int) -> bytes:
    data = reader.read(length)
    if len(data) < length:
        raise EOFError("Unexpected end of BDX stream")
    return data


def _read_u8(reader: io.BytesIO) -> int:
    return _read_bytes(reader, 1)[0]


def _read_u16(reader: io.BytesIO) -> int:
    return struct.unpack(">H", _read_bytes(reader, 2))[0]


def _read_i16(reader: io.BytesIO) -> int:
    return struct.unpack(">h", _read_bytes(reader, 2))[0]


def _read_u32(reader: io.BytesIO) -> int:
    return struct.unpack(">I", _read_bytes(reader, 4))[0]


def _read_i32(reader: io.BytesIO) -> int:
    return struct.unpack(">i", _read_bytes(reader, 4))[0]


def _read_i8(reader: io.BytesIO) -> int:
    return struct.unpack(">b", _read_bytes(reader, 1))[0]


def _read_string(reader: io.BytesIO) -> str:
    """读取以 NUL 结尾的 UTF-8 字符串。"""
    buf = bytearray()
    while True:
        b = _read_bytes(reader, 1)
        if b == b"\x00":
            break
        buf.extend(b)
    return buf.decode("utf-8", errors="replace")


def _read_bool(reader: io.BytesIO) -> bool:
    return bool(_read_u8(reader))


def _read_command_block_data(reader: io.BytesIO) -> dict[str, Any]:
    """读取命令方块公共数据字段（BDX 内嵌格式）。"""
    return {
        "mode": _read_u32(reader),
        "command": _read_string(reader),
        "customName": _read_string(reader),
        "lastOutput": _read_string(reader),
        "tickDelay": _read_u32(reader),
        "executeOnFirstTick": _read_bool(reader),
        "trackOutput": _read_bool(reader),
        "conditional": _read_bool(reader),
        "needsRedstone": _read_bool(reader),
    }


def _read_chest_slot(reader: io.BytesIO) -> dict[str, Any]:
    return {
        "itemName": _read_string(reader),
        "count": _read_u8(reader),
        "data": _read_u16(reader),
        "slotID": _read_u8(reader),
    }


class BDXReader(Reader):
    format_name = "bdx"
    extensions = (".bdx",)
    description = "Minecraft 基岩版 BDX 建筑文件"

    def __init__(
        self,
        runtime_id_mapping: Optional[dict[int, dict[str, Any]]] = None,
        mapping_file: Optional[str] = None,
    ):
        self._mapping = (
            runtime_id_mapping
            if runtime_id_mapping is not None
            else load_runtime_id_mapping(mapping_file)
        )

    def read(self, source: Source) -> Building:
        data = self._read_bytes(source)
        return self._parse(data)

    # ------------------------------------------------------------------ 解析
    def _parse(self, data: bytes) -> Building:
        if data[:3] != b"BD@":
            raise ValueError(f"无效的 BDX 外头: {data[:3]!r}")

        reader = io.BytesIO(brotli.decompress(data[3:]))

        if _read_bytes(reader, 4) != b"BDX\x00":
            raise ValueError("无效的 BDX 内头")

        self._building = Building(source_format=self.format_name)
        self._strings: list[str] = []  # 常量字符串池
        self._x = self._y = self._z = 0  # 当前坐标（BDX 用相对移动追踪）
        self._building.author = _read_string(reader)

        while True:
            op_id = _read_u8(reader)
            if op_id == 88:  # 'X' 结束标记
                break
            self._handle_op(op_id, reader)

        return self._building

    def _handle_op(self, op_id: int, reader: io.BytesIO) -> None:
        # ---- 坐标移动类 ----
        if op_id == 1:  # CreateConstantString
            self._strings.append(_read_string(reader))

        elif op_id == 6:  # AddInt16ZValue0
            self._z += _read_i16(reader)
        elif op_id == 8:  # AddZValue0
            self._z += 1
        elif op_id == 12:  # AddInt32ZValue0
            self._z += _read_i32(reader)
        elif op_id == 14:
            self._x += 1
        elif op_id == 15:
            self._x -= 1
        elif op_id == 16:
            self._y += 1
        elif op_id == 17:
            self._y -= 1
        elif op_id == 18:
            self._z += 1
        elif op_id == 19:
            self._z -= 1
        elif op_id == 20:
            self._x += _read_i16(reader)
        elif op_id == 21:
            self._x += _read_i32(reader)
        elif op_id == 22:
            self._y += _read_i16(reader)
        elif op_id == 23:
            self._y += _read_i32(reader)
        elif op_id == 24:
            self._z += _read_i16(reader)
        elif op_id == 25:
            self._z += _read_i32(reader)
        elif op_id == 28:
            self._x += _read_i8(reader)
        elif op_id == 29:
            self._y += _read_i8(reader)
        elif op_id == 30:
            self._z += _read_i8(reader)

        # ---- 普通方块放置 ----
        elif op_id == 5:  # PlaceBlockWithBlockStates
            block_id = _read_u16(reader)
            states_id = _read_u16(reader)
            self._add_block(
                self._string(block_id),
                parse_block_states_string(self._string(states_id)),
            )

        elif op_id == 7:  # PlaceBlock
            block_id = _read_u16(reader)
            _ = _read_u16(reader)  # block_data
            self._add_block(self._string(block_id), {})

        elif op_id == 13:  # PlaceBlockWithBlockStatesDeprecated
            block_id = _read_u16(reader)
            states_str = _read_string(reader)
            self._add_block(self._string(block_id), parse_block_states_string(states_str))

        # ---- 命令方块 ----
        elif op_id == 26:  # SetCommandBlockData
            cmd = _read_command_block_data(reader)
            self._apply_command_block_data(self._x, self._y, self._z, cmd)

        elif op_id == 27:  # PlaceBlockWithCommandBlockData
            block_id = _read_u16(reader)
            data = _read_u16(reader)  # block_data（编码朝向/条件位）
            cmd = _read_command_block_data(reader)
            self._add_command_block(self._string(block_id), cmd, data)

        elif op_id == 34:  # PlaceRuntimeBlockWithCommandBlockData
            runtime_id = _read_u16(reader)
            cmd = _read_command_block_data(reader)
            self._add_runtime_command_block(runtime_id, cmd)

        elif op_id == 35:  # ...AndUint32RuntimeID
            runtime_id = _read_u32(reader)
            cmd = _read_command_block_data(reader)
            self._add_runtime_command_block(runtime_id, cmd)

        elif op_id == 36:  # PlaceCommandBlockWithCommandBlockData
            data = _read_u16(reader)  # data（编码朝向/条件位）
            cmd = _read_command_block_data(reader)
            self._add_command_block(None, cmd, data)

        # ---- Runtime 方块 ----
        elif op_id == 31:  # UseRuntimeIDPool
            _ = _read_u8(reader)  # pool_id（当前实现不区分池）
        elif op_id == 32:  # PlaceRuntimeBlock
            runtime_id = _read_u16(reader)
            self._add_runtime_block(runtime_id)
        elif op_id == 33:  # PlaceBlockWithRuntimeId
            runtime_id = _read_u32(reader)
            self._add_runtime_block(runtime_id)

        # ---- 容器 ----
        elif op_id == 37:  # PlaceRuntimeBlockWithChestData
            runtime_id = _read_u16(reader)
            chest = self._read_chest(reader)
            self._add_block_with_nbt(self._runtime_name(runtime_id), chest)

        elif op_id == 38:  # ...AndUint32RuntimeID
            runtime_id = _read_u32(reader)
            chest = self._read_chest(reader)
            self._add_block_with_nbt(self._runtime_name(runtime_id), chest)

        elif op_id == 40:  # PlaceBlockWithChestData
            block_id = _read_u16(reader)
            _ = _read_u16(reader)
            chest = self._read_chest(reader)
            self._add_block_with_nbt(self._string(block_id), chest)

        # ---- NBT 数据 ----
        elif op_id == 41:  # PlaceBlockWithNBTData
            block_id = _read_u16(reader)
            states_id = _read_u16(reader)
            _ = _read_u16(reader)
            nbt = self._read_block_nbt(reader)
            self._add_block_with_nbt(
                self._string(block_id),
                nbt,
                states=parse_block_states_string(self._string(states_id)),
            )

        elif op_id == 39:  # AssignDebugData
            length = _read_u32(reader)
            _ = _read_bytes(reader, length)

        elif op_id == 9:  # NOP
            pass

        else:
            raise ValueError(f"未知 BDX 操作码: {op_id} (0x{op_id:02X})")

    # ---------------------------------------------------------------- 辅助
    def _string(self, idx: int) -> str:
        if 0 <= idx < len(self._strings):
            return self._strings[idx]
        return f"<unknown_string_id:{idx}>"

    def _runtime_name(self, runtime_id: int) -> str:
        """Runtime ID → 规范化方块名；查不到时用占位名。"""
        info = resolve_runtime_block(self._mapping, runtime_id)
        return info["block"] if info else f"runtime_{runtime_id}"

    def _add_block(self, name: str, states: dict[str, Any]) -> None:
        from ..utils import normalize_block_name

        self._building.blocks.append(
            Block(self._x, self._y, self._z, normalize_block_name(name), states)
        )

    def _add_runtime_block(self, runtime_id: int) -> None:
        self._add_block(self._runtime_name(runtime_id), {})

    def _add_block_with_nbt(
        self,
        name: str,
        nbt: dict[str, Any],
        states: Optional[dict[str, Any]] = None,
    ) -> None:
        from ..utils import normalize_block_name

        self._building.blocks.append(
            Block(
                self._x,
                self._y,
                self._z,
                normalize_block_name(name),
                states or {},
                nbt,
            )
        )

    # ---- 命令方块 ----
    def _add_runtime_command_block(self, runtime_id: int, cmd: dict[str, Any]) -> None:
        name = self._runtime_name(runtime_id)
        if name in COMMAND_BLOCK_MODES:
            self._add_command_block(name, cmd)
        else:
            self._add_command_block(None, cmd)

    def _add_command_block(
        self, block_id: Optional[str], cmd: dict[str, Any], data: int | None = None
    ) -> None:
        if block_id and block_id in COMMAND_BLOCK_MODES:
            mode = COMMAND_BLOCK_MODES[block_id]
        else:
            mode = _BDX_MODE_MAP.get(cmd.get("mode", 0), MODE_IMPULSE)
            block_id = COMMAND_BLOCK_IDS[mode]

        states = self._command_block_states(data)

        # 命令方块也要进入 blocks（setblock 放置需要）
        self._ensure_command_block_block(self._x, self._y, self._z, block_id, cmd, states)

        # 若该位置已有命令方块（可能是先放方块、后补数据），则合并
        for cb in self._building.command_blocks:
            if (cb.x, cb.y, cb.z) == (self._x, self._y, self._z):
                self._apply_command_block_fields(cb, cmd)
                return

        self._building.command_blocks.append(self._make_command_block(mode, cmd))

    def _apply_command_block_data(
        self, x: int, y: int, z: int, cmd: dict[str, Any]
    ) -> None:
        """SetCommandBlockData：补充当前位置命令方块的数据（或新增）。"""
        mode = _BDX_MODE_MAP.get(cmd.get("mode", 0), MODE_IMPULSE)
        block_id = COMMAND_BLOCK_IDS[mode]
        self._ensure_command_block_block(x, y, z, block_id, cmd, {})

        for cb in self._building.command_blocks:
            if (cb.x, cb.y, cb.z) == (x, y, z):
                self._apply_command_block_fields(cb, cmd)
                return
        # 之前没有命令方块记录，则新增
        self._building.command_blocks.append(self._make_command_block(mode, cmd))

    def _ensure_command_block_block(
        self,
        x: int,
        y: int,
        z: int,
        block_id: str,
        cmd: dict[str, Any],
        states: dict[str, int],
    ) -> None:
        """确保该位置有一个命令方块 Block（供 setblock 放置）。"""
        for blk in self._building.blocks:
            if (blk.x, blk.y, blk.z) == (x, y, z):
                if blk.name not in COMMAND_BLOCK_MODES:
                    # 该位置原是非命令方块，被命令方块覆盖
                    blk.name = block_id
                blk.states.update(states)
                blk.nbt = dict(cmd)
                return
        self._building.blocks.append(Block(x, y, z, block_id, states=states, nbt=dict(cmd)))

    @staticmethod
    def _command_block_states(data: int | None) -> dict[str, int]:
        """解码命令方块 legacy data → 方块状态。

        Bedrock 命令方块 data 值：bit0-2 = facing_direction，bit3 = conditional_bit。
        （与 CHelper 等社区转换工具的映射一致）
        """
        if data is None:
            return {}
        return {
            "facing_direction": data & 0x7,
            "conditional_bit": (data >> 3) & 0x1,
        }

    def _make_command_block(self, mode: int, cmd: dict[str, Any]) -> CommandBlock:
        return CommandBlock(
            x=self._x,
            y=self._y,
            z=self._z,
            mode=mode,
            command=str(cmd.get("command", "") or ""),
            custom_name=str(cmd.get("customName", "") or ""),
            tick_delay=int(cmd.get("tickDelay", 0) or 0),
            conditional=bool(cmd.get("conditional", False)),
            needs_redstone=bool(cmd.get("needsRedstone", True)),
            execute_on_first_tick=bool(cmd.get("executeOnFirstTick", False)),
            track_output=bool(cmd.get("trackOutput", True)),
        )

    @staticmethod
    def _apply_command_block_fields(cb: CommandBlock, cmd: dict[str, Any]) -> None:
        """用命令数据更新已有 CommandBlock 的字段（保留位置与模式）。"""
        if "command" in cmd:
            cb.command = str(cmd["command"] or "")
        if "customName" in cmd:
            cb.custom_name = str(cmd["customName"] or "")
        if "tickDelay" in cmd:
            cb.tick_delay = int(cmd["tickDelay"] or 0)
        if "conditional" in cmd:
            cb.conditional = bool(cmd["conditional"])
        if "needsRedstone" in cmd:
            cb.needs_redstone = bool(cmd["needsRedstone"])
        if "executeOnFirstTick" in cmd:
            cb.execute_on_first_tick = bool(cmd["executeOnFirstTick"])
        if "trackOutput" in cmd:
            cb.track_output = bool(cmd["trackOutput"])

    # ---- 容器 / NBT ----
    @staticmethod
    def _read_chest(reader: io.BytesIO) -> dict[str, Any]:
        slot_count = _read_u8(reader)
        slots = [_read_chest_slot(reader) for _ in range(slot_count)]
        return {"chestData": slots}

    def _read_block_nbt(self, reader: io.BytesIO) -> dict[str, Any]:
        if HAS_NBTLIB:
            try:
                tag, _ = nbtlib.tag.Compound.parse(reader, byteorder="big")
                return {"snbt": nbtlib.serialize_tag(tag)}
            except Exception:
                pass
        # 无 nbtlib 或解析失败：剩余字节整体编码为 base64
        remaining = reader.read()
        reader.seek(0, io.SEEK_END)
        return {"base64": base64.b64encode(remaining).decode("ascii")}
