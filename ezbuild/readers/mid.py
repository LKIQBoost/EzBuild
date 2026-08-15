"""mid 读取器：把 MIDI（SMF）二进制解析为 Song。

SMF 二进制解析（``_read_varint`` / ``_parse_smf`` / ``_parse_channel`` /
``_parse_track`` / ``_make_time_converter``）从 musicplayer 项目的
``midilib.py`` 内联移植而来，这里改为输出中立音符（带时长、力度、
通道音量/声像/弯音），而不是 /playsound 命令。
"""

from __future__ import annotations

import os
import struct

from ..song import (
    Note,
    Layer,
    Song,
    percussion_note_to_sound,
    program_to_sound,
)
from .base import Reader, Source


# 未闭合音符（缺 note_off）的默认时长
_DEFAULT_NOTE_SECONDS = 0.5


# ---------------------------------------------------------------------------
# SMF 二进制解析（移植自 midilib.py）
# ---------------------------------------------------------------------------

def _read_varint(data, off):
    value = 0
    n = len(data)
    while off < n:
        b = data[off]
        off += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, off


def _parse_smf(data):
    if len(data) < 14 or data[:4] != b'MThd':
        raise ValueError('不是有效的 MIDI 文件 (缺少 MThd 头)')
    fmt = struct.unpack('>H', data[8:10])[0]
    division = struct.unpack('>H', data[12:14])[0]
    off = 8 + struct.unpack('>I', data[4:8])[0]
    tracks = []
    tlen = 0
    while off + 8 <= len(data):
        if data[off:off + 4] == b'MTrk':
            tlen = struct.unpack('>I', data[off + 4:off + 8])[0]
            tracks.append(data[off + 8:off + 8 + tlen])
        off += 8 + tlen
    return fmt, division, tracks


def _parse_channel(data, off, status):
    hi = status & 0xF0
    ch = status & 0x0F
    d1 = data[off]
    off += 1
    if hi in (0xC0, 0xD0):
        return ({'type': 'program_change' if hi == 0xC0 else 'channel_pressure',
                 'channel': ch, 'value': d1}, off)
    d2 = data[off]
    off += 1
    if hi == 0x80:
        return ({'type': 'note_off', 'channel': ch, 'note': d1, 'velocity': d2}, off)
    if hi == 0x90:
        return ({'type': 'note_on', 'channel': ch, 'note': d1, 'velocity': d2}, off)
    if hi == 0xA0:
        return ({'type': 'poly_pressure', 'channel': ch, 'note': d1, 'value': d2}, off)
    if hi == 0xB0:
        return ({'type': 'control_change', 'channel': ch, 'control': d1, 'value': d2}, off)
    if hi == 0xE0:
        return ({'type': 'pitch_bend', 'channel': ch, 'lsb': d1, 'msb': d2}, off)
    return ({'type': 'unknown', 'channel': ch}, off)


def _parse_track(data):
    events = []
    tick = 0
    off = 0
    n = len(data)
    running_status = None
    while off < n:
        try:
            delta, off = _read_varint(data, off)
        except Exception:
            break
        tick += delta
        if off >= n:
            break
        b = data[off]
        if b >= 0x80:
            status = b
            off += 1
            if status == 0xFF:
                mtype = data[off]
                off += 1
                mlen, off = _read_varint(data, off)
                mdata = data[off:off + mlen]
                off += mlen
                events.append((tick, 'meta', mtype, mdata))
                if mtype == 0x2F:
                    break
                running_status = None  # meta 事件取消 running status
            elif status == 0xF0 or status == 0xF7:
                slen, off = _read_varint(data, off)
                off += slen
                running_status = None  # sysex 事件取消 running status
            else:
                running_status = status
                msg, off = _parse_channel(data, off, status)
                events.append((tick, 'midi', msg))
        else:
            if running_status is None:
                off += 1
                continue
            msg, off = _parse_channel(data, off, running_status)
            events.append((tick, 'midi', msg))
    return events


def _make_time_converter(division, tempo_map):
    """返回 tick -> 秒 的转换函数（移植自 midilib.py）。"""
    if division & 0x8000:
        # SMPTE 时间码
        fps = -((division >> 8) & 0xFF)
        tpf = division & 0xFF
        if fps == 29:
            fps = 30
        if fps > 0 and tpf > 0:
            spt = 1.0 / (float(fps) * float(tpf))
        else:
            spt = 0.01
        return (lambda tick: tick * spt)

    tpb = division & 0x7FFF
    if tpb <= 0:
        tpb = 480

    def conv(tick):
        total = 0.0
        prev_tick = 0
        prev_us = 500000.0
        for t, us in tempo_map:
            if tick <= t:
                break
            total += (t - prev_tick) * (prev_us / 1000000.0) / float(tpb)
            prev_tick = t
            prev_us = us
        total += (tick - prev_tick) * (prev_us / 1000000.0) / float(tpb)
        return total

    return conv


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class MidReader(Reader):
    format_name = "mid"
    extensions = (".mid", ".midi")
    description = "MIDI (SMF) 音乐文件"

    def read(self, source: Source) -> Song:
        data = self._read_bytes(source)
        _fmt, division, tracks = _parse_smf(data)

        events = []
        for tr in tracks:
            events.extend(_parse_track(tr))
        events.sort(key=lambda e: e[0])  # 稳定排序，保持同 tick 顺序

        # 速度映射（meta 0x51: 微秒/四分音符）
        tempo_map = []
        for ev in events:
            if ev[1] == 'meta' and ev[2] == 0x51 and len(ev[3]) >= 3:
                us = (ev[3][0] << 16) | (ev[3][1] << 8) | ev[3][2]
                tempo_map.append((ev[0], us))
        if not tempo_map:
            tempo_map.append((0, 500000))
        tempo_map.sort(key=lambda e: e[0])

        conv = _make_time_converter(division, tempo_map)
        song_tempo = 60000000.0 / tempo_map[0][1]
        if song_tempo <= 0:
            song_tempo = 120.0
        song = Song(name=self._display_name(source), tempo=song_tempo)

        # 每通道当前状态（顺序处理，正确处理同 tick 多次改变）
        program = {}      # ch -> program
        volume = {}       # ch -> 0..1
        balance = {}      # ch -> -1..1
        bend = {}         # ch -> 半音

        # 进行中的音符：(ch, note) -> [(on_tick, 状态快照), ...]
        active: dict[tuple[int, int], list] = {}
        raw_notes = []    # (time, channel, note, vel, sound, pan, pitch_bend, duration)

        last_tick = 0
        for ev in events:
            tick = ev[0]
            last_tick = max(last_tick, tick)
            if ev[1] == 'meta':
                continue
            m = ev[2]
            ch = m['channel']
            if m['type'] == 'program_change':
                program[ch] = m['value']
            elif m['type'] == 'control_change':
                if m['control'] == 7:
                    volume[ch] = m['value'] / 127.0
                elif m['control'] in (8, 10):
                    balance[ch] = max(-1.0, min(1.0, (m['value'] - 64) / 64.0))
                elif m['control'] == 121:
                    volume[ch] = 1.0
            elif m['type'] == 'pitch_bend':
                raw = (m['msb'] << 7 | m['lsb']) - 8192
                bend[ch] = raw / 8192.0 * 2.0  # 默认 ±2 半音
            elif m['type'] == 'note_on' and m['velocity'] > 0:
                if ch == 9:
                    sound = percussion_note_to_sound(m['note'])
                else:
                    sound = program_to_sound(program.get(ch, 0))
                state = (
                    tick,
                    m['note'],
                    (m['velocity'] / 127.0) * volume.get(ch, 1.0),
                    sound,
                    balance.get(ch, 0.0),
                    bend.get(ch, 0.0),
                )
                active.setdefault((ch, m['note']), []).append(state)
            elif m['type'] == 'note_off' or (m['type'] == 'note_on' and m['velocity'] == 0):
                stack = active.get((ch, m['note']))
                if stack:
                    on_tick, note, vel, sound, pan, pb = stack.pop()
                    raw_notes.append(
                        (conv(on_tick), note, vel, sound, ch, pan, pb,
                         conv(tick) - conv(on_tick))
                    )
            # 其它事件（poly_pressure / channel_pressure / unknown）忽略

        # 未闭合音符：延续到文件末尾；末尾即起点则用默认时长
        end_sec = conv(last_tick)
        for (ch, note), stack in active.items():
            for on_tick, n, vel, sound, pan, pb in stack:
                start = conv(on_tick)
                dur = end_sec - start
                if dur <= 0:
                    dur = _DEFAULT_NOTE_SECONDS
                raw_notes.append((start, n, vel, sound, ch, pan, pb, dur))

        self._assign_layers(song, raw_notes)
        return song

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _display_name(source: Source) -> str:
        """字节来源没有文件名，用 format_name 兜底。"""
        if isinstance(source, (bytes, bytearray)):
            return "midi"
        return os.path.basename(str(source))

    @staticmethod
    def _assign_layers(song: Song, raw_notes: list) -> None:
        """midi2nbs 式 stacking：每通道一个层块，块内按同 tick 出现顺序
        分配行号，保证 NBS 写入时 (tick, layer) 不冲突。"""
        if not raw_notes:
            return
        tps = song.tempo / 15.0  # NBS tempo = bpm/15

        def grid(n):
            return round(n[0] * tps)

        ordered = sorted(raw_notes, key=lambda n: (grid(n), n[4], n[0]))
        by_key = {}   # (channel, grid) -> [note, ...]
        heights = {}  # channel -> 最大同 tick 音符数
        for n in ordered:
            key = (n[4], grid(n))
            by_key.setdefault(key, []).append(n)
            heights[n[4]] = max(heights.get(n[4], 0), len(by_key[key]))

        bases = {}
        base = 0
        for ch in sorted(heights):
            bases[ch] = base
            base += heights[ch]

        layer_channel = {}  # layer -> channel（用于命名）
        for (ch, _g), grp in by_key.items():
            for i, n in enumerate(grp):
                layer = bases[ch] + i
                layer_channel[layer] = ch
                song.notes.append(
                    Note(
                        time=round(n[0], 4),
                        note=n[1],
                        velocity=round(n[2], 4),
                        sound=n[3],
                        channel=ch,
                        layer=layer,
                        panning=round(n[5], 4),
                        pitch_bend=round(n[6], 4),
                        duration=round(n[7], 4),
                    )
                )

        for i in sorted(layer_channel):
            ch = layer_channel[i]
            name = "Percussion" if ch == 9 else f"Ch.{ch}"
            song.layers.append(Layer(id=i, name=name))
