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

# 自定义 BDX Runtime ID 映射表
python main.py cmd_json -i 建筑.bdx --mapping block_runtime_ids.json

# 把未分区块的 txt 分区块（txt → txt，输出自动加 _分区块 后缀避免覆盖）
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

# 音乐 → 建筑：转成"命令方块音乐机"（蛇形排列，红石触发播放整首歌）
python main.py mcstructure -i 歌曲.mid   # .mcstructure 结构文件（可直接用结构方块导入）
python main.py cmd_json -i 歌曲.mid      # 命令方块 JSON（lemon 格式）
python main.py ibi -i 歌曲.mid           # IBI 导入包
# Java 版 /playsound 语法
python main.py mcstructure -i 歌曲.mid --edition java

# Java Sponge .schem 结构文件读写
python main.py mcstructure -i 建筑.schem
python main.py schem -i 建筑.bdx
```

> setblock/fill 输出默认省略所有 `*_bit` 开关状态（`open_bit`/`toggle_bit`/`powered_bit` 等），
> 让指令更简洁；用 `--all-states` 保留全部。`facing_direction`（朝向）和
> `conditional_bit`（命令方块条件模式）始终保留。命令方块 JSON 的 `IsConditional` 始终输出。

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

## 项目结构

```
ezbuild/
├── main.py              # CLI 入口
├── ezbuild/             # 核心库包
│   ├── model.py         # 中立数据模型：Building / Block / CommandBlock
│   ├── song.py          # 中立音乐模型：Song / Note / Layer + 乐器映射表
│   ├── registry.py      # 格式注册表与按扩展名分发
│   ├── utils.py         # nbtlib 转换 / 方块名规范化 / 状态字符串解析
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
│   │   └── ibi.py
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
- 力度为有效音量（MIDI 通道音量 CC7 / NBS 层音量会折入音符力度）。

**音乐 → 建筑**（`ezbuild.music_builder.song_to_building`）：
把 Song 转成"命令方块音乐机"——一串命令方块沿 **16×16 方形足迹的 3D 蛇形**
排列（与 midi-mcstructure_next 推荐大模板一致，接近正方体；超过 96 层自动
增大足迹），每块 `facing_direction` 指向下一块；第 0 块为脉冲命令方块
（红石触发启动），其余为连锁命令方块（auto）；每块写一条 `/execute ... playsound`，
`TickDelay` 为距上一音符的游戏刻数（1 秒 = 20 刻），总时长 = 各块 TickDelay 累加。
音高 `2**((note-66)/12)`（与 NBS key 45↔note 66 一致），鼓固定 pitch 1.0，
超范围钳制 [0.5, 2.0]。可用 `--edition bedrock|java` 切换 /playsound 语法。

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
