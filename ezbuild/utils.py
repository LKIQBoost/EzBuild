"""通用工具：Runtime ID 映射加载、方块名规范化。

BDX 文件中的方块经常以 Runtime ID 引用，需要查映射表得到方块名；
而映射表里的名字是旧版遗留名（如 ``tile.stone``），需要规范化为
不带 ``minecraft:`` 前缀的现代名（如 ``stone``）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 内置映射表（随包分发）
MAPPING_FILENAME = "block_runtime_ids_1_19_10_22.json"


def _package_data_dir() -> Path | None:
    """定位包内数据目录。

    源码运行用 ``Path(__file__).parent / "data"``；
    Nuitka 打包后 ``__file__`` 不再指向源目录，改用 ``importlib.resources``。
    """
    # 源码 / 常规安装
    direct = Path(__file__).parent / "data"
    if (direct / MAPPING_FILENAME).is_file():
        return direct
    # Nuitka 打包（--include-package-data）
    try:
        import importlib.resources

        base = importlib.resources.files("ezbuild.data")
        if (base / MAPPING_FILENAME).is_file():
            return Path(str(base))
    except Exception:
        pass
    return None


def normalize_block_name(name: str) -> str:
    """规范化方块名：小写、去 ``minecraft:`` / ``tile.`` / ``item.`` 前缀。

    ``minecraft:stone`` → ``stone``；``tile.stone`` → ``stone``。
    """
    n = name.lower().strip()
    for prefix in ("minecraft:", "tile.", "item."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n


def find_mapping_file(explicit: str | None = None) -> Path | None:
    """定位 Runtime ID 映射表 JSON。

    查找顺序：显式指定 > 包内置 data/ > 仓库 bdx/ 目录 > 当前目录。
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    pkg_dir = _package_data_dir()
    if pkg_dir is not None:
        candidates.append(pkg_dir / MAPPING_FILENAME)
    for name in ("block_runtime_ids_1_19_10_22.json", "block_runtime_ids.json"):
        candidates.append(Path.cwd() / name)

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def load_runtime_id_mapping(
    mapping_file: str | None = None,
) -> dict[int, dict[str, Any]]:
    """加载 Runtime ID 映射表，统一为 ``{runtime_id: {"block": 名, "data": 数据值}}``。

    兼容三种常见格式：
    1. 数组（索引即 ID）：``[["tile.stone", 0], ...]``
    2. 字典：``{"6584": "tile.stone"}``
    3. 数组对象：``[{"id": 6584, "name": "tile.stone"}]``
    """
    path = find_mapping_file(mapping_file)
    if path is None:
        return {}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result: dict[int, dict[str, Any]] = {}

    if isinstance(data, list):
        for rid, entry in enumerate(data):
            if isinstance(entry, (list, tuple)) and entry:
                result[rid] = {"block": str(entry[0]), "data": entry[1] if len(entry) > 1 else 0}
            elif isinstance(entry, dict) and "name" in entry:
                rid2 = entry.get("id", rid)
                result[int(rid2)] = {"block": str(entry["name"]), "data": entry.get("data", 0)}
    elif isinstance(data, dict):
        for k, v in data.items():
            try:
                rid = int(k)
            except (TypeError, ValueError):
                continue
            result[rid] = {"block": str(v), "data": 0}

    return result


def resolve_runtime_block(
    mapping: dict[int, dict[str, Any]], runtime_id: int
) -> dict[str, Any] | None:
    """按 Runtime ID 查映射，返回规范化的 ``{"block": 名, "data": 数据值}``。"""
    info = mapping.get(runtime_id)
    if info is None:
        return None
    return {
        "block": normalize_block_name(info.get("block", "")),
        "data": info.get("data", 0),
    }


# ---------------------------------------------------------------------------
# nbtlib tag → 普通 Python 结构
#   nbtlib 2.x 的 tag 是 Python 内建类型的子类
#   （Compound→dict、List→list、String→str、Int→int、Byte→int、Float→float），
#   因此可直接用 isinstance 转换，得到不含 tag 的纯 Python 数据。
# ---------------------------------------------------------------------------
def tag_to_python(value: Any) -> Any:
    """递归把 nbtlib tag 转换为普通 Python 结构。"""
    if isinstance(value, dict):
        return {k: tag_to_python(v) for k, v in value.items()}
    if isinstance(value, list):
        return [tag_to_python(v) for v in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# classic .schematic 方块 ID 映射表
# ---------------------------------------------------------------------------
_SCHEMATIC_TABLE_FILENAME = "schematic_blocks.json"


def load_schematic_table(explicit: str | None = None) -> dict[str, str]:
    """加载 classic .schematic 方块映射表（{id*16+data: 方块规格}）。

    查找顺序：显式指定 > 包内 data/ > 当前目录。
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    direct = Path(__file__).parent / "data" / _SCHEMATIC_TABLE_FILENAME
    if direct.is_file():
        candidates.append(direct)
    try:
        import importlib.resources

        base = importlib.resources.files("ezbuild.data")
        candidates.append(Path(str(base)) / _SCHEMATIC_TABLE_FILENAME)
    except Exception:
        pass
    candidates.append(Path.cwd() / _SCHEMATIC_TABLE_FILENAME)

    for cand in candidates:
        if cand.is_file():
            with open(cand, "r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(
        f"找不到 schematic 方块映射表（{_SCHEMATIC_TABLE_FILENAME}）"
    )


# ---------------------------------------------------------------------------
# 方块状态格式化（setblock / fill 指令的方块状态部分）
# ---------------------------------------------------------------------------
def is_default_strip_state(key: str) -> bool:
    """默认省略的开关类状态：``*_bit`` 结尾，但 conditional_bit（命令方块）保留。"""
    return key.endswith("_bit") and key != "conditional_bit"


def format_block_states(
    states: dict[str, Any], strip: frozenset[str] | None = None
) -> str:
    """把方块状态 dict 格式化为指令片段（不含方括号）。

    如 ``{"conditional_bit": 1, "facing_direction": 5}`` →
    ``"conditional_bit"=true,"facing_direction"=5``。
    布尔状态输出 true/false（conditional_bit 存储为 int 0/1 也按布尔处理）。

    ``strip``: ``None`` = 用默认规则（省略 open_bit/toggle_bit 等 ``*_bit``
    开关状态）；传入状态键集合则只省略这些键（空集合 = 保留全部）。
    """
    parts = []
    for k, v in states.items():
        if strip is None:
            if is_default_strip_state(k):
                continue
        elif k in strip:
            continue
        if k == "conditional_bit":
            formatted = "true" if v else "false"
        elif isinstance(v, bool):
            formatted = "true" if v else "false"
        elif isinstance(v, int):
            formatted = str(v)
        else:
            formatted = f'"{v}"'
        parts.append(f'"{k}"={formatted}')
    return ",".join(parts)


# ---------------------------------------------------------------------------
# 方块状态字符串解析（SNBT 风格，兼容 {} 与 [] 外壳、= 与 : 分隔）
# ---------------------------------------------------------------------------
def parse_block_states_string(s: str) -> dict[str, Any]:
    """把方块状态字符串解析为 dict。

    兼容多种写法：``{"facing_direction"=3}``、``["stone_type"="stone"]``、
    ``{facing_direction: 3}``。空字符串返回空 dict。
    """
    s = (s or "").strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        s = s[1:-1]
    result: dict[str, Any] = {}
    if not s.strip():
        return result
    for part in _split_top_level(s):
        sep = "=" if "=" in part else ":"
        if sep not in part:
            continue
        key, _, value = part.partition(sep)
        result[key.strip().strip('"').strip("'")] = _coerce_state_value(value)
    return result


def _split_top_level(s: str) -> list[str]:
    """按逗号切分，忽略引号内 / 括号内的逗号。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "{[":
            depth += 1
            cur.append(ch)
        elif ch in "}]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _coerce_state_value(v: str) -> Any:
    """把状态值字符串转为 Python 值（bool/int/str）。"""
    v = v.strip()
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        pass
    return v.strip('"').strip("'")
