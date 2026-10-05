"""图片读取器：把位图（PNG/JPG/BMP/GIF/WebP…）转成方块墙/地板建筑。

算法参考 Image2Schematic（``frmMain_clean.py``）：把每个像素按 RGB 欧氏距离
映射到一组代表方块色的最近色，可选有序抖动或 Floyd–Steinberg 误差扩散。

用法::

    python main.py schem -i 图.png -w 100          # 宽 100，高按原图比例
    python main.py mcstructure -i 图.png -w 100 -h 60
    python main.py txt -i 图.png --dither nearest  # 不做抖动
    python main.py schem -i 图.png --vertical      # 输出竖直墙（默认水平地板）

依赖 Pillow（``pip install Pillow``）；未安装时仅在真正读取图片时报错，
不影响其它格式。
"""

from __future__ import annotations

import io
from typing import Optional, Sequence

from ..model import Block, Building
from .base import Reader, Source

# ---------------------------------------------------------------------------
# 调色板：方块名 → 代表色（现代方块平均色）
#   16 色羊毛沿用 Minecraft 经典地图色，其余为各方块纹理的近似平均色。
# ---------------------------------------------------------------------------
PALETTE: tuple[tuple[str, tuple[int, int, int]], ...] = (
    # ---- 16 色羊毛 ----
    ("white_wool", (255, 255, 255)),
    ("orange_wool", (216, 127, 51)),
    ("magenta_wool", (178, 76, 216)),
    ("light_blue_wool", (102, 153, 216)),
    ("yellow_wool", (229, 229, 51)),
    ("lime_wool", (127, 204, 25)),
    ("pink_wool", (242, 127, 165)),
    ("gray_wool", (153, 153, 153)),
    ("light_gray_wool", (132, 132, 132)),
    ("cyan_wool", (76, 127, 153)),
    ("purple_wool", (127, 63, 178)),
    ("blue_wool", (51, 76, 178)),
    ("brown_wool", (102, 76, 51)),
    ("green_wool", (102, 127, 51)),
    ("red_wool", (153, 51, 51)),
    ("black_wool", (25, 25, 25)),
    # ---- 其它方块 ----
    ("stone", (125, 125, 125)),
    ("grass_block", (95, 159, 53)),
    ("coarse_dirt", (134, 96, 67)),
    ("oak_planks", (162, 130, 78)),
    ("spruce_planks", (114, 84, 48)),
    ("water", (63, 118, 228)),
    ("oak_leaves", (54, 117, 29)),
    ("lapis_block", (30, 67, 140)),
    ("sandstone", (216, 203, 155)),
    ("cobweb", (227, 227, 227)),
    ("gold_block", (246, 208, 61)),
    ("iron_block", (220, 220, 220)),
    ("tnt", (219, 63, 40)),
    ("diamond_block", (98, 237, 228)),
    ("ice", (145, 183, 253)),
    ("clay", (161, 170, 186)),
    ("netherrack", (97, 38, 38)),
    ("emerald_block", (42, 203, 90)),
    ("melon", (140, 190, 60)),
    ("red_sandstone", (186, 99, 29)),
    ("quartz_block", (235, 229, 222)),
    ("prismarine", (99, 156, 151)),
    ("redstone_block", (175, 24, 5)),
    ("purpur_block", (169, 88, 169)),
    ("nether_wart_block", (114, 6, 7)),
    ("bone_block", (229, 225, 207)),
)

# 抖动模式别名
_DITHER_ALIASES = {
    "nearest": "nearest",
    "none": "nearest",
    "dg": "nearest",
    "ordered": "ordered",
    "od": "ordered",
    "floyd": "floyd",
    "fs": "floyd",
    "fsd": "floyd",
}

# 输出朝向
_PLANES = ("horizontal", "vertical")

# 4×4 Bayer 有序抖动矩阵
_BAYER4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def _require_pillow():
    """惰性导入 Pillow，未安装时给出清晰提示。"""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise ImportError(
            "读取图片需要 Pillow 库：pip install Pillow"
        ) from e
    return Image


def _clamp(v: float) -> int:
    return 0 if v < 0 else (255 if v > 255 else int(v))


def _nearest(r: int, g: int, b: int, colors: Sequence[tuple[int, int, int]]) -> int:
    """返回 ``colors`` 中与 (r,g,b) 欧氏距离最近的索引。"""
    best_i = 0
    best_d = 1 << 30
    for i, (pr, pg, pb) in enumerate(colors):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _dither_nearest(w: int, h: int, pixels: list[list[int]], colors) -> list[int]:
    out = [0] * (w * h)
    for i, (r, g, b) in enumerate(pixels):
        out[i] = _nearest(r, g, b, colors)
    return out


def _dither_ordered(w: int, h: int, pixels: list[list[int]], colors) -> list[int]:
    out = [0] * (w * h)
    for y in range(h):
        row = _BAYER4[y & 3]
        for x in range(w):
            i = y * w + x
            r, g, b = pixels[i]
            threshold = (row[x & 3] - 8) * 16
            out[i] = _nearest(
                _clamp(r + threshold), _clamp(g + threshold), _clamp(b + threshold), colors
            )
    return out


def _dither_floyd(w: int, h: int, pixels: list[list[int]], colors) -> list[int]:
    out = [0] * (w * h)
    # 下一行累积的误差（每列一个 [r,g,b]）
    err = [[0.0, 0.0, 0.0] for _ in range(w)]
    for y in range(h):
        for x in range(w):
            i = y * w + x
            r, g, b = pixels[i]
            r = _clamp(r + err[x][0])
            g = _clamp(g + err[x][1])
            b = _clamp(b + err[x][2])

            idx = _nearest(r, g, b, colors)
            pr, pg, pb = colors[idx]
            out[i] = idx

            er, eg, eb = r - pr, g - pg, b - pb
            if x + 1 < w:
                err[x + 1][0] += er * 7 / 16
                err[x + 1][1] += eg * 7 / 16
                err[x + 1][2] += eb * 7 / 16
            if y + 1 < h:
                if x > 0:
                    err[x - 1][0] += er * 3 / 16
                    err[x - 1][1] += eg * 3 / 16
                    err[x - 1][2] += eb * 3 / 16
                err[x][0] += er * 5 / 16
                err[x][1] += eg * 5 / 16
                err[x][2] += eb * 5 / 16
                if x + 1 < w:
                    err[x + 1][0] += er * 1 / 16
                    err[x + 1][1] += eg * 1 / 16
                    err[x + 1][2] += eb * 1 / 16
    return out


_DITHERS = {
    "nearest": _dither_nearest,
    "ordered": _dither_ordered,
    "floyd": _dither_floyd,
}


def _build_colors(bumpy: bool):
    """构建 (名称列表, 颜色列表)：bumpy 时为每个方块追加暗/亮变体。"""
    names: list[str] = []
    colors: list[tuple[int, int, int]] = []
    for name, (r, g, b) in PALETTE:
        if bumpy:
            variants = (
                (max(0, r - 40), max(0, g - 40), max(0, b - 40)),
                (r, g, b),
                (min(255, r + 40), min(255, g + 40), min(255, b + 40)),
            )
        else:
            variants = ((r, g, b),)
        for c in variants:
            names.append(name)
            colors.append(c)
    return names, colors


class ImageReader(Reader):
    format_name = "image"
    extensions = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga")
    description = "图片转建筑（像素→最近方块色，-w/-h 指定尺寸，默认水平地板）"

    def __init__(
        self,
        width: Optional[int] = None,
        height: Optional[int] = None,
        dither: str = "floyd",
        bumpy: bool = False,
        plane: str = "horizontal",
    ):
        self.width = width
        self.height = height
        self.bumpy = bumpy
        key = _DITHER_ALIASES.get(str(dither).lower())
        if key is None:
            raise ValueError(
                f"未知抖动模式 {dither!r}，可选: nearest/ordered/floyd"
            )
        self.dither = key
        if plane not in _PLANES:
            raise ValueError(f"未知朝向 {plane!r}，可选: {'/'.join(_PLANES)}")
        self.plane = plane

    # ------------------------------------------------------------------ 读取
    def read(self, source: Source) -> Building:
        Image = _require_pillow()

        if isinstance(source, (bytes, bytearray)):
            img = Image.open(io.BytesIO(bytes(source)))
        else:
            img = Image.open(source)

        img = self._to_rgb(Image, img)
        orig_w, orig_h = img.size
        w, h = self._target_size(orig_w, orig_h)
        if (w, h) != (orig_w, orig_h):
            img = img.resize((w, h), Image.Resampling.LANCZOS)

        raw = img.tobytes()  # RGB 原始字节
        pixels = [list(raw[i:i + 3]) for i in range(0, len(raw), 3)]
        names, colors = _build_colors(self.bumpy)
        indices = _DITHERS[self.dither](w, h, pixels, colors)

        building = Building(source_format=self.format_name)
        blocks = building.blocks
        if self.plane == "horizontal":
            # X=宽, Z=高（图片顶部=北/z 小）, Y=厚 1
            building.size = (w, 1, h)
            for py in range(h):
                base = py * w
                for px in range(w):
                    blocks.append(Block(px, 0, py, names[indices[base + px]]))
        else:
            # X=宽, Y=高（图片顶部=上方）, Z=厚 1
            building.size = (w, h, 1)
            for py in range(h):
                y = h - 1 - py
                base = py * w
                for px in range(w):
                    blocks.append(Block(px, y, 0, names[indices[base + px]]))
        return building

    # ---------------------------------------------------------------- 辅助
    def _target_size(self, orig_w: int, orig_h: int) -> tuple[int, int]:
        """确定输出宽高：仅给一个时按原图宽高比算另一个；都不给用原尺寸。"""
        w, h = self.width, self.height
        if w and h:
            return int(w), int(h)
        if w:
            return int(w), max(1, round(int(w) * orig_h / orig_w))
        if h:
            return max(1, round(int(h) * orig_w / orig_h)), int(h)
        return orig_w, orig_h

    @staticmethod
    def _to_rgb(Image, img):
        """统一转 RGB；带透明度的图片先合成到白底，避免透明变黑。"""
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(bg, rgba).convert("RGB")
        return img.convert("RGB")
