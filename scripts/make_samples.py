"""生成 samples/ 下的示例建筑文件（合成数据，用于演示与测试）。

用法:  python scripts/make_samples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from fixtures import (  # noqa: E402
    make_bdx_bytes,
    make_mcstructure_bytes,
    make_schematic_bytes,
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def main() -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    (SAMPLES_DIR / "sample.mcstructure").write_bytes(make_mcstructure_bytes())
    (SAMPLES_DIR / "sample.bdx").write_bytes(make_bdx_bytes())
    (SAMPLES_DIR / "sample.schematic").write_bytes(make_schematic_bytes())
    print(f"示例文件已生成到: {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
