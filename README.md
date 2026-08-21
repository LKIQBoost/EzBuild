# ezbuild

Minecraft 建筑文件格式转换工具。把各种建筑文件（`.bdx`、`.mcstructure` 等）解析为**中立模型**，
再渲染为任意输出格式（命令方块 JSON、setblock 指令、IBI 包……）。

新增输入/输出格式只需添加一个文件，框架零改动。

## 使用

### 命令行

```bash
# 第一个参数为输出格式（必填），-i 指定一个或多个输入文件；输出与输入同名同目录
python main.py cmd_json -i 建筑.bdx
python main.py ibi -i 建筑.bdx
python main.py txt -i 建筑.bdx 建筑2.mcstructure

# -o 可选：指定单个输出文件
python main.py cmd_json -i 建筑.bdx -o 命令方块.json

# 列出所有支持的格式
python main.py -l

# 把未分区块的 txt 分区块（txt → txt，输出自动加 _分区块 后缀避免覆盖）
# 直接流式解析分组（不建 Building 模型），比普通 Building 路径省一半以上内存与时间；
# 完成后显示优化率（原始指令 → 输出指令，减少百分比）
python main.py txt -i 未分区块.txt

# txt 不进行三维 fill 合并（输出纯 setblock）
python main.py txt -i 建筑.bdx --nofill

# 拆分 IBI：还原命令方块 JSON（lemon 格式）与 setblock txt
python main.py cmd_json -i 建筑.ibi
python main.py txt -i 建筑.ibi

# 保留全部方块状态（默认省略 *_bit 开关状态）
python main.py txt -i 建筑.bdx --all-states

# 音乐：MIDI <-> NBS 互转（音符/力度/声像/弯音保留）
python main.py nbs -i 歌曲.mid
python main.py mid -i 歌曲.nbs

# NBS 输出保留原始音高（默认会把超范围音符八度折叠进音阶块可播放范围 33-57）
python main.py nbs -i 歌曲.mid --raw-range

# 音乐 → 建筑：转成"命令方块音乐机"（蛇形排列，红石触发播放整首歌；固定基岩版 /playsound 语法）
python main.py mcstructure -i 歌曲.mid   # .mcstructure 结构文件（可直接用结构方块导入）
python main.py cmd_json -i 歌曲.mid      # 命令方块 JSON（lemon 格式）
python main.py ibi -i 歌曲.mid           # IBI 导入包

# Java Sponge .schem 结构文件读写
python main.py mcstructure -i 建筑.schem
python main.py schem -i 建筑.bdx

# schem/schematic → 所有建筑格式自动走**流式增量转换**（不建整座建筑模型，
# 千万级方块的巨型结构也不会占满内存；命令方块/调色板等照常保留）
python main.py txt -i 巨型建筑.schem
python main.py ibi -i 巨型建筑.schematic
python main.py mcstructure -i 巨型建筑.schem
python main.py schem -i 巨型建筑.schem
python main.py cmd_json -i 巨型建筑.schematic

# txt → txt 分区块同样走流式（不建 Building 模型，直接按区块分组逐行写出）；
# --nofill 则读一行写一行。大文件内存与耗时约为 Building 路径的 1/2~1/3
python main.py txt -i 未分区块的巨型.txt

# -t N：强制共享内存多进程并行（默认大文件自动并行，小文件单进程）。
# 注意：生成是磁盘 I/O 瓶颈，ibi 能提速 ~1.7 倍；txt 单进程直写已最优，并行反而慢。
python main.py ibi -i 巨型建筑.schem -t 4
python main.py ibi -i 巨型建筑.schem -t          # -t 不接数字 = 自动进程数

# -c：用随包的 C++ DLL（water_structure_shared.dll）做**整文件转换**，
# 全程在 C++ 内完成（不建 Python 模型、不逐方块跑 Python 循环、内部线程不受 GIL 约束），
# 大文件比 Python 快一个量级。DLL 不支持的格式/读不了的文件自动回退 Python 原生转换。
python main.py mcstructure -i 巨型建筑.schem -c
python main.py schem -i 巨型建筑.bdx -c
python main.py ibi -i 巨型建筑.schem -c -t 4    # -t N 传给 DLL 线程数
python main.py txt -i 巨型建筑.schem -c          # txt 也走 DLL（实测 ~4×，见下）
# -c 额外解锁 DLL 独有的输出格式（需加 -c）：
python main.py bdx -i 巨型建筑.schem -c
python main.py litematic -i 巨型建筑.schem -c
python main.py mcfn -i 巨型建筑.schem -c
python main.py axiombp -i 巨型建筑.schem -c
python main.py fuhong -i 巨型建筑.schem -c
python main.py schematic -i 巨型建筑.bdx -c      # 经典 Java .schematic 输出
```

> **`-c` 与 Python 转换的行为差异**：DLL 走它自己的格式约定——坐标统一
> **归零到原点 (0,0,0)**（Python 路径保留 schem 的 Offset 世界坐标），方块
> 用 DLL 的 Java↔Bedrock 映射（个别方块计数/命名可能与 Python 略不同）。
> 输出文件对 Minecraft / WorldEdit / Litematica 等是合法可用的；若需要保留
> 原始世界偏移，请用不加 `-c` 的 Python 原生转换。
>
> **`-c txt` 特别说明**：txt 的 DLL 输出就是 MCFunction 指令文件——**绝对坐标**、
> `minecraft:` 前缀、无分区块/tp 导航、会先 fill 清空整个包围盒为 air。
> 与本库 txt 读取器互读，但它**不是** Python 分区块 txt（相对坐标 + tp 导航 +
> S 型排序 + 保留世界偏移）。要原来的分区块 txt 就不要加 `-c`。实测 3.4M 方块
> schem→txt：Python 7.4s vs `-c` 1.7s（~4×）。
>
> **DLL 读不了的文件**（如个别非标准 schem、ezbuild 自己写出的 schem）
> 会提示并自动回退 Python 原生转换，功能不受影响，只是不加速。
> `cmd_json` / `mid` / `nbs` 无 DLL 实现，始终走 Python。

> setblock/fill 输出默认省略所有 `*_bit` 开关状态（`open_bit`/`toggle_bit`/`powered_bit` 等），
> 让指令更简洁；用 `--all-states` 保留全部。`facing_direction`（朝向）和
> `conditional_bit`（命令方块条件模式）始终保留。命令方块 JSON 的 `IsConditional` 始终输出。

### 世界导出（从存档提取建筑）

`-i` 指向 **世界文件夹** 时，自动进入世界导出模式（按 `region/` 与 `db/` 自动区分
版本）：用起始/结束 xyz 世界坐标框出包围盒，把范围内的建筑导出为任意输出格式。

支持两种存档：
- **Java 版**（含 `region/`）：1.13+ 的 Anvil 存档（1.18+ 的负 y / 负坐标区块均可）。
- **Bedrock 基岩版**（含 `db/` + `level.dat`）：LevelDB 存档，子区块 v1/v8/v9
  （1.18+，含负 y）。方块实体 / 命令方块从 `0x31` 键提取。

```bash
# 导出包围盒内的建筑为 Sponge .schem（Offset 保留世界坐标）。
# 坐标用 WorldEdit 风格：-pos1 x y z -pos2 x y z（含端点，可反着给自动取 min/max）
python main.py schem -i 世界文件夹 -pos1 100 -64 200 -pos2 150 100 250

# 基岩版 .mcstructure（structure_world_origin 保留世界坐标）
python main.py mcstructure -i 世界文件夹 -pos1 100 -64 200 -pos2 150 100 250

# txt / IBI / 命令方块 JSON
python main.py txt -i 世界文件夹 -pos1 100 -64 200 -pos2 150 100 250
python main.py ibi -i 世界文件夹 -pos1 100 -64 200 -pos2 150 100 250
python main.py cmd_json -i 世界文件夹 -pos1 100 -64 200 -pos2 150 100 250

# -o 指定输出路径；旧写法 --x1 --y1 --z1 --x2 --y2 --z2 仍兼容
python main.py schem -i 世界文件夹 -pos1 150 100 250 -pos2 100 -64 200 -o 建筑.schem
```

**性能与内存**：txt / ibi / cmd_json 逐区块流式转换——Java 只解压包围盒相交的
region 文件；Bedrock 用自带的极简 LevelDB 读取器（`ezbuild/leveldb.py`）只解析
各 `.ldb` 的索引块、数据块**按需读取**（LRU 缓存），都**不把整库载入内存**，
内存恒定在几十 MB（实测 300×219×300、约 420 万方块：内存 ~90MB）。
`mcstructure` / `schem` 用两遍扫描（先算实际内容边界，只分配裁剪后数组），
避免为超大包围盒一次性分配整盒数组。

`-t N`：纯 setblock txt / ibi 按 16×16 区块**多进程并行**（共享内存有界），
实测 4 进程比单进程快 ~2.8 倍，输出逐字节一致。进度条：纯 setblock 显示已生成
方块数；schem 显示「扫描边界 / 填充数组」区块进度。

> 注意：如果 `-pos1/-pos2` 框住**整个世界**（例如几千格跨度、地形密布），
> 导出的 schem 本身就接近 GB 级（BlockData 按体积算），属格式固有；建议只框
> 要导出的建筑范围，或用 txt / ibi 流式格式。
>
> **`-s`（split）自动拆分**：Sponge .schem 调色板上限 256 种方块。若区域方块
> 种类超过 256（如整世界地形+矿石+建筑混在一起，实测 600×600 就有 600+ 种），
> 加 `-s` 会自动沿 x/z 轴递归拆分成多个 `<输出名>_1.schem`、`_2.schem`…
> 每个分块调色板 ≤256，Offset 保留各自的世界坐标，可直接拼回原位：
> `python main.py schem -i 世界文件夹 -pos1 100 -64 200 -pos2 1500 100 1800 -s`

### 作为库使用

```python
import ezbuild

building = ezbuild.convert_read("建筑.bdx")              # 读取（按扩展名自动识别）
ezbuild.convert_write(building, "命令方块.json", "cmd_json")  # 输出
```

## 支持的格式

| 类型 | 格式名 | 说明 |
| ---- | ------ | ---- |
| 输入 | `bdx` | 基岩版 BDX 建筑文件 |
| 输入 | `mcstructure` | 基岩版 `.mcstructure` 结构文件 |
| 输入 | `schematic` | Java 版经典 `.schematic` 结构文件 |
| 输入 | `schem` | Java 版 Sponge `.schem` 结构文件（Palette/BlockData） |
| 输入 | `txt` | `setblock`/`fill` 指令文本（未分区块） |
| 输入 | `ibi` | IBI 导入包（setblock 文本 + 命令方块 JSON，XOR 加密） |
| 输入 | `mid` | MIDI（SMF）音乐文件 |
| 输入 | `nbs` | OpenNoteBlockStudio NBS 音乐文件 |
| 输出 | `cmd_json` | 命令方块 JSON（`posx/posy/posz + BlockMode + Command + TickDelay + IsRedStoneMode + IsConditional`） |
| 输出 | `txt` | 分区块优化 txt：16×16 区块 + S 型排序 + tp 导航（默认 fill 三维合并，`--nofill` 关闭） |
| 输出 | `ibi` | IBI 导入包（setblock 文本 + 命令方块 JSON，XOR 加密打包） |
| 输出 | `mcstructure` | 基岩版 `.mcstructure` 结构文件（建筑写出，未压缩 NBT） |
| 输出 | `schem` | Java 版 Sponge `.schem` 结构文件（gzip NBT） |
| 输出 | `mid` | MIDI（SMF）音乐文件 |
| 输出 | `nbs` | OpenNoteBlockStudio NBS 音乐文件 |
| 输出¹ | `bdx` | 基岩版 BDX 建筑文件（`-c` 由 C++ DLL 生成） |
| 输出¹ | `schematic` | Java 版经典 `.schematic` 结构文件（`-c` 由 C++ DLL 生成） |
| 输出¹ | `litematic` | Litematica 结构文件（`-c` 由 C++ DLL 生成） |
| 输出¹ | `mcfn` | Minecraft datapack 函数（`-c` 由 C++ DLL 生成） |
| 输出¹ | `axiombp` | Axiom 蓝图 `.bp`（`-c` 由 C++ DLL 生成） |
| 输出¹ | `fuhong` | FuHong V5 建筑文件（`-c` 由 C++ DLL 生成） |

¹ DLL 独有输出格式：必须加 `-c/--cpp`（由随包的 C++ DLL 生成，无 Python 实现）。

## 项目结构

```
ezbuild/
├── main.py              # CLI 入口
├── ezbuild/             # 核心库包
│   ├── model.py         # 中立数据模型：Building / Block / CommandBlock
│   ├── song.py          # 中立音乐模型：Song / Note / Layer + 乐器映射表
│   ├── registry.py      # 格式注册表与按扩展名分发
│   ├── utils.py         # nbtlib 转换 / 方块名规范化 / 状态字符串解析
│   ├── world.py         # 世界文件夹导出（包围盒 → 建筑，流式逐区块；自动识别版本）
│   ├── bedrock.py       # Bedrock 世界源：LevelDB 子区块 v1/v8/v9 解码 + 方块实体
│   ├── leveldb.py       # 极简 LevelDB 读取器（.ldb/.log，选择性解压，zlib）
│   ├── readers/         # 输入解析器（自动注册）
│   │   ├── base.py      # Reader 抽象基类
│   │   ├── mcstructure.py
│   │   ├── bdx.py
│   │   ├── schematic.py
│   │   ├── txt.py
│   │   └── ibi.py
│   ├── writers/         # 输出渲染器（自动注册）
│   │   ├── base.py      # Writer 抽象基类
│   │   ├── cmd_json.py  # 命令方块 JSON（lemon 格式）
│   │   ├── txt.py       # 分区块优化 txt
│   │   ├── ibi.py
│   │   └── dll_only.py  # DLL 独有格式占位 Writer（bdx/litematic/mcfn 等，需 -c）
│   ├── native/          # C++ DLL 快速转换（-c/--cpp）
│   │   ├── _binding.py  # ctypes 绑定（惰性加载，搜索 WATER_STRUCTURE_LIBRARY 或包内 DLL）
│   │   ├── water_structure_shared.dll
│   │   └── assets/      # DLL 运行时方块映射数据
│   └── data/            # 内置 Runtime ID 映射表
├── samples/             # 示例建筑文件（scripts/make_samples.py 重新生成）
└── tests/               # pytest 测试
```

## 工作原理

```
建筑文件 → Reader → Building（中立模型）→ Writer → 输出文件
          (bdx/mcstructure)             (cmd_json/txt/ibi)
```

`Building` 是核心中间层：

- `blocks`：全部方块（位置 + 方块名 + 方块状态 + 方块实体 NBT）
- `command_blocks`：命令方块（命令、模式、延迟、条件、红石等语义数据）

所以**新增一种输入格式**，所有现有输出格式立即支持；反之亦然。

**`-c/--cpp` 快速路径**（`ezbuild/native`）绕过这个模型：整个转换直接交给
C++ DLL（`文件 → DLL → 文件`），不解析 Building、不逐方块跑 Python 循环，
内部线程不受 GIL 约束。DLL 负责输入格式自动识别（schem/schematic/mcstructure/
ibi/bdx 等）与写出，`-c` 只做文件级转发；DLL 读不了的文件自动回退 Python 路径。

### 音乐格式（mid / nbs）

`mid` / `nbs` 走平行的中立音乐模型 :class:`Song`（`ezbuild/song.py`）：
音符统一为「时间（秒）+ MIDI 音高 + 有效力度 + Minecraft 音效（乐器）+ 通道/层 + 声像 + 弯音」。
两种格式互转的约定：

- **音高**：NBS key = MIDI note − 21（社区标准，key 0-87 对应钢琴 88 键）。
  MIDI → NBS 时默认把 key 八度折叠进音阶块可播放范围 [33,57]（pitch 0.5-2.0），
  保证转出的文件能发声；`--raw-range` 关闭折叠（保留原始音高，round-trip 精确）。
- **节拍**：1 NBS tick = 1/4 拍（16 分音符），NBS tempo = bpm/15；MIDI 输出 division 480。
- **乐器**：GM 程序号 ↔ NBS 乐器号 都以 Minecraft 音效名为中介映射；
  鼓（NBS 乐器 bd/snare/hat ↔ MIDI 通道 9）。
  MIDI → 命令方块时采用 midi-mcstructure_next 的两层映射（程序号/打击乐音符 →
  抽象音效名 → 实际 playsound 部件），用上 1.21 铜管音阶块、`random.fizz` 镲片等，
  每个乐器带响度补偿与半音偏移；只有 MIDI 来源的音符携带这些部件，
  mid↔nbs 音乐文件往返不受影响。
- 力度为有效音量（MIDI 通道音量 CC7 / NBS 层音量会折入音符力度）。

**音乐 → 建筑**（`ezbuild.music_builder.song_to_building`）：
把 Song 转成"命令方块音乐机"——一串命令方块沿 **16×16 方形足迹的 3D 蛇形**
排列（与 midi-mcstructure_next 推荐大模板一致，接近正方体；超过 96 层自动
增大足迹），每块 `facing_direction` 指向下一块；第 0 块为脉冲命令方块
（红石触发启动），其余为连锁命令方块（auto）；每块写一条 `/execute ... playsound`，
`TickDelay` 为距上一音符的游戏刻数（1 秒 = 20 刻），总时长 = 各块 TickDelay 累加。
音高 `2**((note-66)/12)`（与 NBS key 45↔note 66 一致），并叠加 MIDI 弯音
（`Note.pitch_bend`，弯音范围按 RPN 0/1 解析，默认 ±2 半音）；打击乐以基准 66
起算。**超范围音符默认八度折叠**进可播放范围（pitch 0.5-2.0），保住相对音准
（钳制会把极端音符压成同音、旋律被压平）；`--raw-octave` 关闭折叠保留原始八度。
非音阶块音效（镲片等）不折叠也不钳到 2.0，保留半音偏移的区分度；音量会乘以
乐器响度补偿后钳 [0,1]。多部件乐器（如 orchestra_hit、telephone_ring）展开成
多个命令方块；/playsound 固定用基岩版语法。

## 扩展新格式

### 新增输入格式（Reader）

在 `ezbuild/readers/` 下新建 `xxx.py`：

```python
from ..model import Building
from .base import Reader

class XxxReader(Reader):
    format_name = "xxx"            # 格式唯一名
    extensions = (".xxx",)         # 用于按扩展名自动识别
    description = "..."

    def read(self, source) -> Building:
        # 解析 source（文件路径或 bytes），返回 Building
        ...
```

保存即自动注册，`python main.py -l` 与 CLI 立即可用。

### 新增输出格式（Writer）

在 `ezbuild/writers/` 下新建 `xxx.py`：

```python
from ..model import Building
from .base import Writer

class XxxWriter(Writer):
    format_name = "xxx"
    extensions = (".xxx",)
    description = "..."

    def render(self, building: Building) -> str | bytes:
        # 从 building.blocks / building.command_blocks 渲染输出内容
        ...
```

## 测试

```bash
python -m pytest tests/ -q
```

测试使用合成建筑文件（`tests/fixtures.py` 内存生成），无需真实文件。

## 依赖

- `nbtlib` — mcstructure / NBT 解析
- `brotli` — BDX 解压
- `tqdm` + `rich` — 流式转换进度条（`tqdm.rich` 美化）
