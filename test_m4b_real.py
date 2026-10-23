# -*- coding: utf-8 -*-
"""用真实 FFmpeg 验证 M4B 产出（不需要 TTS，几秒内完成）。

依赖：本目录下 ffmpeg_local\\...\\ffmpeg.exe（可由 gh-proxy 下载 BtbN 构建）。
测试音频用 ffmpeg 的 sine 生成器现场合成，因此不需要联网。

验证点：
- ffprobe 断言章节数与输入一致（此前 -map_chapters 缺失会导致章节静默丢失）
- 封面以 attached_pic 形式嵌入
- 书名/作者等元数据写入
- 取消时不产生产物
- 无 ffmpeg 时优雅跳过（退出码 0）
"""
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'epub reader.py')
if not os.path.exists(SRC):
    SRC = r'D:\SynologyDrive\图片\epub reader.py'


def _runs(exe):
    """探测候选 ffmpeg 是否真的能执行。"""
    try:
        out = subprocess.run([exe, '-version'], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=30)
        return out.returncode == 0 and b'ffmpeg version' in out.stdout
    except Exception:
        return False


# 试跑过的不可用候选（存在文件但跑不起来，例如下载不完整或被安全策略拦截）
BROKEN_FFMPEG = []


def find_ffmpeg():
    """按顺序探测候选，返回第一个**能真正执行**的 ffmpeg。

    只判断文件是否存在是不够的：本机上就出现过 ffmpeg.exe 存在却无法启动的
    构建（进程创建直接失败），此时应当跳到下一个候选，而不是拿它去跑测试。
    """
    roots = [
        os.path.join(HERE, 'ffmpeg_local'),
        os.path.join(HERE, 'dist', 'epubReader', 'ffmpeg'),
        os.path.join(os.environ.get('APPDATA', ''), 'epubReader', 'ffmpeg'),
        HERE,
    ]
    candidates = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if 'ffmpeg.exe' in files:
                candidates.append(os.path.join(base, 'ffmpeg.exe'))
    which = shutil.which('ffmpeg')
    if which:
        candidates.append(which)

    for exe in candidates:
        if _runs(exe):
            return exe
        BROKEN_FFMPEG.append(exe)
    return None


FFMPEG = find_ffmpeg()
if not FFMPEG:
    if BROKEN_FFMPEG:
        print('找到 %d 个 ffmpeg 候选，但没有一个能执行：' % len(BROKEN_FFMPEG))
        for exe in BROKEN_FFMPEG:
            print('    %s' % exe)
    print('未找到可用的 ffmpeg，跳过 M4B 实测（这不是失败）')
    sys.exit(0)

for exe in BROKEN_FFMPEG:
    print('跳过不可执行的 ffmpeg 候选: %s' % exe)

FFPROBE = os.path.join(os.path.dirname(FFMPEG), 'ffprobe.exe')
FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

# 从真实源码抽取被验证的函数
with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)
WANTED = {'merge_mp3_to_m4b', 'probe_chapter_count', 'mp3_duration_ms',
          'get_audio_duration_ms'}
picked = [n for n in tree.body if getattr(n, 'name', None) in WANTED]
CONSTS = {'_MPEG1_BITRATES', '_MPEG2_BITRATES', '_MPEG1_SAMPLE_RATES',
          'FFMPEG_EXE_NAMES', 'FFPROBE_EXE_NAMES'}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.If)) and any(
            getattr(t, 'id', None) in CONSTS
            for sub in ast.walk(node) if isinstance(sub, ast.Assign)
            for t in sub.targets):
        picked.append(node)

ns = {'os': os, 're': __import__('re'), 'sys': sys, 'tempfile': tempfile,
      'shutil': shutil, 'subprocess': subprocess, 'json': json,
      'FFMPEG_PATH': FFMPEG, 'traceback': __import__('traceback')}


class _MissingMP3(object):
    def __init__(self, *a, **k):
        raise ImportError('mutagen not available')


ns['MP3'] = _MissingMP3
for node in picked:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)

merge = ns['merge_mp3_to_m4b']
probe_chapters = ns['probe_chapter_count']

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''))
    if not cond:
        failures.append(label)


def run(cmd, timeout=180):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, creationflags=FLAGS)
    return result.returncode, result.stdout, result.stderr


def ffprobe_json(path):
    code, out, _err = run([FFPROBE, '-v', 'error', '-show_format',
                           '-show_streams', '-show_chapters', '-of', 'json', path])
    if code != 0:
        return {}
    return json.loads(out.decode('utf-8', errors='ignore') or '{}')


def valid_png(width=8, height=8, rgb=(30, 120, 200)):
    import binascii
    import struct
    import zlib

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', binascii.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + bytes(rgb) * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


work = tempfile.mkdtemp(prefix='m4b_real_')
titles = ['第一章 起点', '第二章 转折', '第三章 终点']
mp3s = []
for index in range(3):
    path = os.path.join(work, '%03d-%s.mp3' % (index + 1, titles[index]))
    code, _out, err = run([
        FFMPEG, '-f', 'lavfi', '-i', 'sine=frequency=%d:duration=3' % (440 + index * 60),
        '-codec:a', 'libmp3lame', '-b:a', '128k', '-y', path])
    if code != 0:
        detail = err.decode('utf-8', errors='ignore')[-300:]
        print('生成测试 MP3 失败: %s' % detail)
        # 能走到这里的 ffmpeg 一定是 _runs() 探测过的（可执行）。
        # 可执行却写不出文件，通常是执行环境限制（受限沙箱/安全策略），
        # 不是产品缺陷，按"环境不满足"跳过，避免给出假失败。
        print('ffmpeg 可执行但无法写出测试文件（环境限制或构建不可用），跳过 M4B 实测')
        sys.exit(0)
    mp3s.append(path)

print('使用 ffmpeg: %s' % FFMPEG)
print('测试音频: %d 个（各 3 秒）' % len(mp3s))

print('\n[1] 真实合并')
out = os.path.join(work, 'book.m4b')
cover = valid_png()
ok, err = merge(mp3s, out, {'title': '真实合并测试', 'author': '测试作者',
                            'album': '真实合并测试'}, cover, FFMPEG, titles)
check('合并成功', ok is True, err)
check('输出文件存在且合理大小',
      os.path.exists(out) and os.path.getsize(out) > 50000,
      os.path.getsize(out) if os.path.exists(out) else None)

if not os.path.exists(out):
    shutil.rmtree(work, ignore_errors=True)
    print('\n%d checks, %d failures' % (checks[0], len(failures)))
    sys.exit(1)

info = ffprobe_json(out)
streams = info.get('streams', [])
chapters = info.get('chapters', [])
fmt = info.get('format', {})

check('ffprobe 报告 %d 个章节（关键回归点）' % len(mp3s),
      len(chapters) == len(mp3s), len(chapters))
check('检查函数与 ffprobe 结论一致',
      probe_chapters(out, FFMPEG) == len(mp3s),
      probe_chapters(out, FFMPEG))
check('含音频流', any(s.get('codec_type') == 'audio' for s in streams),
      [s.get('codec_type') for s in streams])
check('含 attached_pic 封面流',
      any(s.get('disposition', {}).get('attached_pic') == 1 for s in streams),
      [(s.get('codec_type'), s.get('disposition')) for s in streams])
check('总时长约 9 秒（3×3s）',
      8.0 <= float(fmt.get('duration') or 0) <= 11.0, fmt.get('duration'))
check('章节标题已写入',
      all(any(t in (c.get('tags', {}) or {}).get('title', '') for t in [title])
          for c, title in zip(chapters, titles)),
      [c.get('tags') for c in chapters])
check('书名元数据已写入',
      '真实合并测试' in json.dumps(fmt.get('tags') or {}, ensure_ascii=False),
      fmt.get('tags'))

print('\n[2] 章节时间轴单调')
starts = [float(c.get('start_time') or 0) for c in chapters]
check('章节起点单调不减', starts == sorted(starts), starts)
check('末章起点约为 6 秒', 5.5 <= starts[-1] <= 6.5 if starts else False, starts)

print('\n[3] 取消与失败路径')
abort_out = os.path.join(work, 'abort.m4b')
ok_abort, err_abort = merge(mp3s, abort_out, {'title': 'X'}, None, FFMPEG, titles,
                            cancel_check=lambda: True)
check('取消时返回失败', ok_abort is False)
check('取消原因可读', '取消' in (err_abort or ''), err_abort)
check('取消时不产生输出', not os.path.exists(abort_out))

ok_empty, err_empty = merge([], os.path.join(work, 'x.m4b'), {'title': 'X'},
                            None, FFMPEG, [])
check('空输入被拒绝', ok_empty is False)

print('\n[4] 临时目录清理')
leftover = [d for d in os.listdir(tempfile.gettempdir())
            if d.startswith('epubReader_m4b_')]
check('未残留合并临时目录', not leftover, leftover)

shutil.rmtree(work, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks[0], len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
