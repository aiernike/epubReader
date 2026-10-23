# -*- coding: utf-8 -*-
"""端到端验证：用真实 edge_tts 库走一遍「分块合成 -> 拼接 MP3 -> 平移 SRT」。

这是本次修复最核心的路径（原实现在长章节上必然失败）。
需要网络；本机已安装 edge_tts。若本机没有 mutagen，会自动验证纯标准库
mp3_duration_ms 回退路径（正是缺少 mutagen 时旧代码出错的那条路）。
"""
import ast
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'
VOICE = sys.argv[2] if len(sys.argv) > 2 else 'zh-CN-XiaoxiaoNeural'

with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)

WANTED = {'AppConfig', '_split_by_boundary', '_hard_split', 'chunk_text',
          '_format_srt_time', 'shift_srt', 'TTSEngine', 'EdgeTTSEngine',
          'mp3_duration_ms', 'get_audio_duration_ms'}
CONST_NAMES = {'_SRT_TIME', '_SENTENCE_END', '_MINOR_PAUSE',
               '_MPEG1_BITRATES', '_MPEG2_BITRATES', '_MPEG1_SAMPLE_RATES'}

picked = [n for n in tree.body if getattr(n, 'name', None) in WANTED]
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.If)) and any(
            getattr(t, 'id', None) in CONST_NAMES
            for sub in ast.walk(node) if isinstance(sub, ast.Assign)
            for t in sub.targets):
        picked.append(node)

import edge_tts

ns = {'os': os, 're': re, 'sys': sys, 'tempfile': tempfile, 'shutil': shutil,
      'asyncio': asyncio, 'json': json, 'subprocess': subprocess,
      'DEFAULT_CHARS_PER_LINE': 15, 'LYRIC_LANG': 'zho',
      'edge_tts': edge_tts, 'traceback': __import__('traceback')}
try:
    from mutagen.mp3 import MP3 as _MP3

    ns['MP3'] = _MP3
    print('mutagen 可用：验证 mutagen 路径')
except ImportError:
    class _MissingMP3(object):
        def __init__(self, *args, **kwargs):
            raise ImportError('mutagen not installed')
    ns['MP3'] = _MissingMP3
    print('本机无 mutagen：验证纯标准库 mp3_duration_ms 回退路径')

for node in picked:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)

chunk_text = ns['chunk_text']
EdgeTTSEngine = ns['EdgeTTSEngine']
mp3_duration_ms = ns['mp3_duration_ms']
TTS_DIRS_BEFORE = set(d for d in os.listdir(tempfile.gettempdir())
                      if d.startswith('epubReader_tts_'))

sentence = ('经过这一段漫长的旅程，他终于明白，真正的勇气并不是无所畏惧，'
            '而是在恐惧面前依然选择向前。')
text = ''.join('%s这是第%s段的补充文字。' % (sentence, i) for i in range(12))
LIMIT = 220
blocks = chunk_text(text, LIMIT)

work = tempfile.mkdtemp(prefix='e2e_tts_')
out_mp3 = os.path.join(work, 'chapter.mp3')
progress = []

print('edge_tts 版本: %s' % getattr(edge_tts, '__version__', '?'))
print('文本长度: %d 字符' % len(text))
print('分块阈值: %d -> %d 块（各块长度 %s）' % (
    LIMIT, len(blocks), [len(b) for b in blocks]))
assert len(blocks) >= 3, '需要至少 3 块才能验证拼接'

loop = asyncio.new_event_loop()
success, srt = loop.run_until_complete(
    EdgeTTSEngine.generate_audio(text, out_mp3, VOICE, max_chars=LIMIT,
                                 progress_callback=lambda d, t: progress.append((d, t))))

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (' -> %s' % (detail,) if not cond else ''))
    if not cond:
        failures.append(label)


check('真实 edge_tts 分块合成成功', success is True)
size = os.path.getsize(out_mp3) if os.path.exists(out_mp3) else 0
check('输出 MP3 非空', size > 1000, '%d bytes' % size)
check('进度回调覆盖全部 %d 块' % len(blocks), len(progress) == len(blocks), progress)

# 用生产代码里的解析器读取整章时长（也是字幕时间轴的推进依据）
total_ms = mp3_duration_ms(out_mp3)
check('拼接结果是完整合法的 MPEG 帧序列', total_ms > 5000, '%dms' % total_ms)

# 逐块合成，验证拼接时长 ≈ 各块之和
part_dir = tempfile.mkdtemp(prefix='e2e_parts_')
part_total_ms = 0
for index, block in enumerate(blocks):
    part_path = os.path.join(part_dir, 'p%03d.mp3' % index)
    ok_part, _ = loop.run_until_complete(
        EdgeTTSEngine.generate_audio(block, part_path, VOICE, max_chars=100000))
    if ok_part:
        part_total_ms += mp3_duration_ms(part_path)
loop.close()
shutil.rmtree(part_dir, ignore_errors=True)
drift = abs(total_ms - part_total_ms) / max(part_total_ms, 1)
check('拼接时长 ≈ 各分块时长之和（误差 <5%）', drift < 0.05,
      '%dms vs %dms (%.1f%%)' % (total_ms, part_total_ms, drift * 100))

blocks_srt = [b for b in srt.strip().split('\n\n') if b.strip()]
check('SRT 非空（证明边界事件被正确接收）', len(blocks_srt) > 0, len(blocks_srt))

timestamps = []
for block in blocks_srt:
    m = re.search(
        r'(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})', block)
    if m:
        g = [int(x) for x in m.groups()]
        timestamps.append((g[0] * 3600 + g[1] * 60 + g[2]) * 1000 + g[3])

def first_inversion(seq):
    for i in range(1, len(seq)):
        if seq[i] < seq[i - 1]:
            return i, seq[i - 1], seq[i]
    return None


inv = first_inversion(timestamps)
check('SRT 时间戳单调不减（跨块平移正确）',
      inv is None, '第 %s 条: %sms -> %sms' % inv if inv else '')
if inv:
    for j in range(max(0, inv[0] - 2), min(len(blocks_srt), inv[0] + 2)):
        print('      附近条目: %r' % blocks_srt[j].replace('\n', ' | '))
check('SRT 最后一条不超出音频总时长 3 秒以上',
      not timestamps or timestamps[-1] <= total_ms + 3000,
      '%sms vs %dms' % (timestamps[-1] if timestamps else -1, total_ms))
check('SRT 编号从 1 连续递增',
      [int(b.split('\n')[0]) for b in blocks_srt] == list(range(1, len(blocks_srt) + 1)))
# 只比较本次运行前后新增的目录：其他套件或异常终止留下的同名目录会造成假失败
new_dirs = (set(d for d in os.listdir(tempfile.gettempdir())
                if d.startswith('epubReader_tts_')) - TTS_DIRS_BEFORE)
check('分块临时目录已清理（无新增残留）', not new_dirs, new_dirs)

shutil.rmtree(work, ignore_errors=True)

print('\n%s' % ('=' * 60))
print('音频 %.2f 秒，字幕 %d 条，%d checks / %d failures' % (
    total_ms / 1000.0, len(blocks_srt), checks[0], len(failures)))
sys.exit(1 if failures else 0)
