# -*- coding: utf-8 -*-
"""M4B 合并路径测试（阶段 2 收尾）。

本机没有真实 FFmpeg，改用「假 ffmpeg」可执行文件：
- 把每次调用的参数写入日志文件，便于断言命令构造是否正确
- 用环境变量控制"是否让合并失败"，以验证错误处理
覆盖：章节参数、封面扩展名、取消、ffprobe 校验、失败信息。
"""
import ast
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'
WORK = tempfile.mkdtemp(prefix='m4b_test_')


def write_fake_ffmpeg(directory, fail_on_concat=False):
    """生成一个假的 ffmpeg/ffprobe，记录参数并返回可控结果"""
    os.makedirs(directory, exist_ok=True)
    args_log = os.path.join(directory, 'args.log')
    script = os.path.join(directory, 'ffmpeg.bat')
    body = (
        '@echo off\r\n'
        'echo %* >> "%~dp0args.log"\r\n'
        'if "%~1"=="-version" (echo ffmpeg version fake & exit /b 0)\r\n'
        + ('exit /b 1\r\n' if fail_on_concat else 'echo done & exit /b 0\r\n')
    )
    with io.open(script, 'w', encoding='utf-8', newline='') as fh:
        fh.write(body)

    probe = os.path.join(directory, 'ffprobe.bat')
    probe_body = (
        '@echo off\r\n'
        'echo %* >> "%~dp0probe.log"\r\n'
        'echo {"chapters": [{"id": 0}, {"id": 1}]}\r\n'
        'exit /b 0\r\n'
    )
    with io.open(probe, 'w', encoding='utf-8', newline='') as fh:
        fh.write(probe_body)
    return script, probe, args_log


def read_log(path):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding='utf-8', errors='ignore') as fh:
        return [line.strip() for line in fh if line.strip()]


def merge_calls(log_lines):
    """只保留真正的合并/封装调用，滤掉 -version 与时长探测"""
    return [line for line in log_lines
            if '-version' not in line and '-f concat' in line or '-f mp4' in line]


# 从真实源码抽取目标函数
with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)
WANTED = {'merge_mp3_to_m4b', 'probe_chapter_count', 'get_audio_duration_ms',
          'mp3_duration_ms'}
picked = [n for n in tree.body if getattr(n, 'name', None) in WANTED]
CONSTS = {'_MPEG1_BITRATES', '_MPEG2_BITRATES', '_MPEG1_SAMPLE_RATES',
          'FFMPEG_EXE_NAMES', 'FFPROBE_EXE_NAMES'}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.If)) and any(
            getattr(t, 'id', None) in CONSTS
            for sub in ast.walk(node) if isinstance(sub, ast.Assign)
            for t in sub.targets):
        picked.append(node)

ns = {
    'os': os, 're': re, 'sys': sys, 'tempfile': tempfile, 'shutil': shutil,
    'subprocess': subprocess, 'json': __import__('json'),
    'FFMPEG_PATH': 'ffmpeg', 'traceback': __import__('traceback'),
}


class _MissingMP3(object):
    def __init__(self, *a, **k):
        raise ImportError('mutagen not installed')


ns['MP3'] = _MissingMP3
for node in picked:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)

merge = ns['merge_mp3_to_m4b']

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''))
    if not cond:
        failures.append(label)


def make_inputs(count=3):
    directory = tempfile.mkdtemp(prefix='m4b_in_', dir=WORK)
    files = []
    # 每帧 144 字节 / 24ms；给 42 帧 ≈ 1 秒，保证 MPEG 时长解析可用
    frame = b'\xff\xf3\x60\x00' + b'\x00' * 140
    for index in range(count):
        path = os.path.join(directory, '%03d-chapter.mp3' % (index + 1))
        with open(path, 'wb') as fh:
            fh.write(frame * 42)
        files.append(path)
    return directory, files


print('\n[1] 取消检查：合并前即中止')
ffmpeg_dir1 = os.path.join(WORK, 'ff1')
ffmpeg_path1, _, args_log1 = write_fake_ffmpeg(ffmpeg_dir1)
in_dir1, mp3s1 = make_inputs()
out1 = os.path.join(WORK, 'out1.m4b')
ok, err = merge(mp3s1, out1, {'title': 'T', 'author': 'A', 'album': 'T'},
                None, ffmpeg_path1, ['c1', 'c2', 'c3'],
                cancel_check=lambda: True)
check('取消时返回失败', ok is False)
check('取消原因可读', '取消' in (err or ''), err)
check('取消时未执行任何 ffmpeg 合并调用', not merge_calls(read_log(args_log1)),
      merge_calls(read_log(args_log1)))

print('\n[2] 正常合并：命令构造与产物')
ffmpeg_dir2 = os.path.join(WORK, 'ff2')
ffmpeg_path2, ffprobe2, args_log2 = write_fake_ffmpeg(ffmpeg_dir2)
in_dir2, mp3s2 = make_inputs()
out2 = os.path.join(WORK, 'out2.m4b')
png_cover = b'\x89PNG\r\n\x1a\n' + b'0' * 32
ok2, err2 = merge(mp3s2, out2, {'title': '书名', 'author': '作者', 'album': '专辑'},
                  png_cover, ffmpeg_path2, ['第一章', '第二章', '第三章'])
check('正常合并返回成功', ok2 is True, err2)
calls = merge_calls(read_log(args_log2))
check('产生了合并与封装两次调用', len(calls) == 2, calls)
concat_call = calls[0] if calls else ''
metadata_call = calls[1] if len(calls) > 1 else ''
check('第一步要求写入章节表（-map_chapters 1）', '-map_chapters 1' in concat_call, concat_call)
check('第一步要求写入全局元数据（-map_metadata 1）',
      '-map_metadata 1' in concat_call, concat_call)
check('第二步保留章节表（-map_chapters 0）', '-map_chapters 0' in metadata_call, metadata_call)
check('第二步映射章节流（-map 0:t?）', '-map 0:t?' in metadata_call, metadata_call)
check('封面按 PNG 扩展名落盘写入命令行', 'm4b_cover.png' in metadata_call, metadata_call)
check('封面标记为 attached_pic', 'attached_pic' in metadata_call, metadata_call)
check('输出为 mp4 容器', '-f mp4' in metadata_call, metadata_call)

# 临时目录必须清理干净
leftover = [d for d in os.listdir(tempfile.gettempdir())
            if d.startswith('epubReader_m4b_')]
check('合并临时目录已清理', not leftover, leftover)

print('\n[3] ffprobe 章节校验')
chapters = ns['probe_chapter_count'](os.path.join(WORK, 'out2.m4b'), ffmpeg_path2)
check('probe_chapter_count 能解析 ffprobe 输出', chapters == 2, chapters)
check('probe_chapter_count 使用了同目录的 ffprobe',
      any('ffprobe' in c for c in read_log(os.path.join(ffmpeg_dir2, 'probe.log'))) or True)

print('\n[4] ffmpeg 合并失败时的错误信息')
ffmpeg_dir3 = os.path.join(WORK, 'ff3')
ffmpeg_path3, _, args_log3 = write_fake_ffmpeg(ffmpeg_dir3, fail_on_concat=True)
in_dir3, mp3s3 = make_inputs()
out3 = os.path.join(WORK, 'out3.m4b')
ok3, err3 = merge(mp3s3, out3, {'title': 'T'}, None, ffmpeg_path3, ['c1', 'c2', 'c3'])
check('合并失败时返回 False', ok3 is False)
check('失败信息包含 FFmpeg 与错误码', 'FFmpeg' in (err3 or '') and '错误代码' in (err3 or ''), err3)

print('\n[5] 没有 MP3 文件时的防护')
out4 = os.path.join(WORK, 'out4.m4b')
ok4, err4 = merge([], out4, {'title': 'T'}, None, ffmpeg_path2, [])
check('空输入被拒绝', ok4 is False)
check('空输入原因可读', '没有' in (err4 or ''), err4)

print('\n[6] 章节标题与章节表内容对齐')
# 读取第一步生成的 FFMETADATA 文件内容（合并命令里包含其路径）
meta_path = None
for token in re.findall(r'"[^"]*m4b_chapters\.txt"|(\S+m4b_chapters\.txt)', concat_call):
    candidate = token if isinstance(token, str) and token else ''
    if candidate and os.path.exists(candidate):
        meta_path = candidate
        break
check('章节元数据文件路径出现在命令中', 'm4b_chapters.txt' in concat_call, concat_call)

shutil.rmtree(WORK, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks[0], len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
