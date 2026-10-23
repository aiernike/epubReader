# -*- coding: utf-8 -*-
"""针对 D:\SynologyDrive\图片\epub reader.py 的纯逻辑测试。

不导入整个模块（本机缺少 PIL/bs4/ebooklib/mutagen），而是用 ast 从真实源码中
抽取待测函数，在受控命名空间里执行，从而保证测的是文件里的真实实现。
"""
import ast
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
import zipfile as zipfile_lib

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'

WANTED = [
    'AppConfig',
    '_split_by_boundary', '_hard_split', 'chunk_text',
    '_format_srt_time', 'shift_srt', 'convert_srt_to_lrc',
    'generate_simple_lrc', 'split_text_by_punctuation', 'sanitize_filename',
    'run_coros_with_limit', 'mp3_duration_ms', 'get_audio_duration_ms',
]

with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)

picked = []
for node in tree.body:
    name = getattr(node, 'name', None)
    if name in WANTED:
        picked.append((name, node))
    elif isinstance(node, (ast.Assign, ast.If)):
        # 注意：像 if sys.platform == 'win32': ... else: ... 这种整块执行，
        # 不能只抽取单条赋值，否则 else 分支会覆盖 win32 分支的取值。
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                if any(getattr(t, 'id', None) in ('_SENTENCE_END', '_MINOR_PAUSE',
                                                  '_SRT_TIME', 'FFMPEG_EXE_NAMES',
                                                  'FFPROBE_EXE_NAMES', '_MPEG1_BITRATES',
                                                  '_MPEG2_BITRATES', '_MPEG1_SAMPLE_RATES')
                       for t in sub.targets):
                    picked.append(('const', node))
                    break

print('extracted from source: %s' % ', '.join(sorted(n for n, _ in picked)))

ns = {
    'os': os, 're': re, 'sys': sys, 'tempfile': tempfile, 'asyncio': __import__('asyncio'),
    'shutil': shutil, 'DEFAULT_CHARS_PER_LINE': 15, 'LYRIC_LANG': 'zho',
    'zipfile': zipfile, 'zipfile_lib': zipfile_lib, 'subprocess': subprocess,
    'threading': __import__('threading'),
}
# 模块级定义索引：抽取代码引用的辅助函数/常量要能补齐
TOP_LEVEL = {}
for _node in tree.body:
    _name = getattr(_node, 'name', None)
    if _name:
        TOP_LEVEL[_name] = _node
    elif isinstance(_node, ast.Assign):
        for _t in _node.targets:
            if getattr(_t, 'id', None):
                TOP_LEVEL[_t.id] = _node

injected = []


def inject_dependencies(nodes, ns, depth=0):
    """把被抽取代码引用到的模块级定义补进命名空间。

    抽取式测试的固有限制：只 exec 目标函数，命名空间里没有模块级辅助函数，
    代码一旦走进调用它们的分支（例如异常处理里的失败提示）就会 NameError，
    失败路径因此永远测不到、也不会暴露问题。这里按引用关系补齐依赖。
    """
    if depth > 4:
        return
    referenced = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                referenced.add(sub.id)
    added = []
    for name in sorted(referenced):
        if name in ns or name not in TOP_LEVEL:
            continue
        node = TOP_LEVEL[name]
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign)):
            exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)
            injected.append(name)
            added.append(node)
    if added:
        inject_dependencies(added, ns, depth + 1)


for name, node in picked:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)

inject_dependencies([node for _n, node in picked if isinstance(node, ast.AST)], ns)


chunk_text = ns['chunk_text']
shift_srt = ns['shift_srt']
convert_srt_to_lrc = ns['convert_srt_to_lrc']
sanitize_filename = ns['sanitize_filename']
run_coros_with_limit = ns['run_coros_with_limit']
AppConfig = ns['AppConfig']

failures = []
checks = 0


def check(label, condition, detail=''):
    global checks
    checks += 1
    if condition:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s %s' % (label, detail))
        failures.append(label)


print('\n[1] chunk_text 分块')
long_cn = ('这是第一句话。' * 300) + ('这是第二段，包含逗号，也有停顿。' * 300)
for limit in (200, 500, 1200):
    parts = chunk_text(long_cn, limit)
    check('chunk_text(limit=%d) 每块不超限' % limit,
          all(len(p) <= limit for p in parts), 'max=%d' % max(len(p) for p in parts))
    check('chunk_text(limit=%d) 内容无损' % limit,
          ''.join(parts).replace('\n', '') == long_cn.replace('\n', ''))
    check('chunk_text(limit=%d) 无空块' % limit, all(p.strip() for p in parts))
check('chunk_text 空输入返回空列表', chunk_text('') == [])
check('chunk_text 无标点超长段落被硬切',
      all(len(p) <= 50 for p in chunk_text('啊' * 1000, 50)))
check('chunk_text 优先在句末标点断开',
      chunk_text('短句一。短句二。短句三。', 6)[0].endswith('。'))

print('\n[2] shift_srt 时间轴平移')
srt_a = '1\n00:00:00,000 --> 00:00:02,500\n第一块\n\n2\n00:00:02,500 --> 00:00:05,000\n第二句\n'
shifted, count = shift_srt(srt_a, 0, 0)
check('shift_srt 首块编号从 1 开始', shifted.startswith('1\n00:00:00,000'), shifted[:24])
check('shift_srt 返回条数=2', count == 2, str(count))
shifted2, count2 = shift_srt(srt_a, 5000, count)
check('shift_srt 偏移 +5000ms 生效', '00:00:05,000 --> 00:00:07,500' in shifted2, shifted2[:80])
check('shift_srt 续编号从 3 开始', shifted2.startswith('3\n'), shifted2[:10])
check('shift_srt 累计条数=4', count2 == 4, str(count2))
check('shift_srt 空输入原样返回', shift_srt('', 100, 7) == ('', 7))
case_shift = shift_srt('1\n开头说明\n00:00:01,000 --> 00:00:02,000\n正文\n', 0, 0)
print('      debug shift_srt(时间行在第3行) -> %r' % (case_shift,))
check('shift_srt 时间行不在第二行也能识别',
      case_shift[1] == 1 and '00:00:01,000 --> 00:00:02,000' in case_shift[0])

print('\n[3] convert_srt_to_lrc（真实 edge-tts 输出格式）')
many = '\n\n'.join(
    '%d\n00:00:%02d,000 --> 00:00:%02d,500\n字%d' % (i + 1, i, i + 1, i)
    for i in range(10))
lrc = convert_srt_to_lrc(many, 15)
check('convert_srt_to_lrc 产出带时间戳的行', lrc.count('[') >= 1, lrc[:60])
check('convert_srt_to_lrc 时间戳格式合法',
      all(re.match(r'^\[\d{2}:\d{2}\.\d{2}\]', ln) for ln in lrc.splitlines() if ln))

print('\n[4] sanitize_filename 与并发工具')
check('sanitize_filename 去掉 Windows 非法字符',
      sanitize_filename('a\\b/c:d*e?f"g<h>i|j') == 'abcdefghij',
      sanitize_filename('a\\b/c:d*e?f"g<h>i|j'))
check('sanitize_filename 超长截断', len(sanitize_filename('长' * 500)) <= 200)
check('sanitize_filename 纯非法字符返回 None', sanitize_filename('???') is None)


def run_async(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


async def boom():
    raise ValueError('expected')


async def ok(value):
    return value


results = run_async(run_coros_with_limit([ok(1), boom(), ok(3)], 2))
check('run_coros_with_limit 并发执行并收集结果', results[0] == 1 and results[2] == 3)
check('run_coros_with_limit 异常被隔离成对象', isinstance(results[1], ValueError))
check('run_coros_with_limit 空输入', run_async(run_coros_with_limit([], 3)) == [])

print('\n[5] EdgeTTSEngine.generate_audio 分块合成（用假 edge_tts 验证，不发网络请求）')
# 抽取真实实现（先取基类，再取 EdgeTTSEngine）
tts_nodes = [n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name in ('TTSEngine', 'EdgeTTSEngine')]
for node in tts_nodes:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)
inject_dependencies(tts_nodes, ns)
print('injected module-level deps: %s' % (', '.join(sorted(set(injected))) or '-'))
fake_dir = tempfile.mkdtemp(prefix='fake_tts_')


class FakeSubMaker(object):
    def __init__(self):
        self.items = []

    def feed(self, chunk):
        self.items.append(chunk)

    def get_srt(self):
        out = []
        for i, item in enumerate(self.items):
            start = int(item['offset'] / 10000)
            end = start + 800
            out.append('%d\n00:00:%02d,%03d --> 00:00:%02d,%03d\n%s' % (
                i + 1, start // 1000, start % 1000,
                end // 1000, end % 1000, item['text']))
        return '\n\n'.join(out) + '\n'


class FakeCommunicate(object):
    def __init__(self, text, voice, **kwargs):
        self.text = text
        self.voice = voice

    async def stream(self):
        # 模拟 edge-tts 的真实帧格式：MPEG-2 Layer III / 24kHz / 64kbps，
        # 每帧 144 字节 = 576 采样 = 24ms（这样 mp3_duration_ms 能正确解析）
        frame = bytearray(b'\xff\xf3\x60\x00' + b'\x00' * 140)
        for _ in range(6):
            yield {'type': 'audio', 'data': bytes(frame)}
        for n in range(2):
            yield {'type': 'WordBoundary', 'offset': n * 1000000,
                   'duration': 800, 'text': '字%d' % n}


class FakeMP3(object):
    def __init__(self, path):
        raise ImportError('mutagen not installed in test env')


fake_edge = types.ModuleType('edge_tts')
fake_edge.Communicate = FakeCommunicate
fake_edge.SubMaker = FakeSubMaker
ns['edge_tts'] = fake_edge
ns['MP3'] = FakeMP3
ns['AppConfig'] = AppConfig
ns['traceback'] = __import__('traceback')
EdgeTTSEngine = ns['EdgeTTSEngine']

long_chapter = ('第一句。第二句。' * 400)
progress_seen = []
TTS_DIRS_BEFORE = set(d for d in os.listdir(tempfile.gettempdir())
                      if d.startswith('epubReader_tts_'))
out_mp3 = os.path.join(fake_dir, 'chapter.mp3')
success, srt_out = run_async(EdgeTTSEngine.generate_audio(
    long_chapter, out_mp3, 'zh-CN-YunjianNeural',
    progress_callback=lambda done, total: progress_seen.append((done, total))))

expected_blocks = len(chunk_text(long_chapter))
check('假 edge_tts 下生成成功', success is True)
check('输出文件已写出且非空', os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0)
check('合并后 SRT 覆盖所有块（条数=%d）' % (expected_blocks * 2),
      srt_out.count('-->') == expected_blocks * 2, str(srt_out.count('-->')))
check('SRT 编号连续递增',
      [int(b.split('\n')[0]) for b in srt_out.strip().split('\n\n')] ==
      list(range(1, expected_blocks * 2 + 1)))
check('进度回调被调用 %d 次' % expected_blocks, len(progress_seen) == expected_blocks)
# 只统计本次运行"新出现"的临时目录：其他测试或异常终止也可能留下同名目录，
# 用全局存在性判断会误报（实测被中断的 e2e 进程留下的残留触发过假失败）
new_tts_dirs = (set(d for d in os.listdir(tempfile.gettempdir())
                    if d.startswith('epubReader_tts_')) - TTS_DIRS_BEFORE)
check('临时分块目录已清理', not new_tts_dirs, new_tts_dirs)

# 失败时必须不留下损坏产物
def failing_communicate(*args, **kwargs):
    class Bad(FakeCommunicate):
        async def stream(self):
            if False:
                yield {}
            raise RuntimeError('network died')
    return Bad(*args, **kwargs)


ns['edge_tts'].Communicate = failing_communicate
bad_out = os.path.join(fake_dir, 'bad.mp3')
success_bad, srt_bad = run_async(EdgeTTSEngine.generate_audio(
    '一句话。', bad_out, 'zh-CN-YunjianNeural'))
check('网络失败时返回 False', success_bad is False)
check('网络失败时不留下损坏 MP3', not os.path.exists(bad_out))

shutil.rmtree(fake_dir, ignore_errors=True)

print('\n[6] safe_extract_zip / install_bin_atomically（Zip Slip 与安装安全）')

safe_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name in ('safe_extract_zip', 'locate_ffmpeg_dir',
                             'verify_ffmpeg', 'install_bin_atomically')]
for node in safe_nodes:
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, 'exec'), ns)
safe_extract_zip = ns['safe_extract_zip']
locate_ffmpeg_dir = ns['locate_ffmpeg_dir']
verify_ffmpeg = ns['verify_ffmpeg']
install_bin_atomically = ns['install_bin_atomically']

work = tempfile.mkdtemp(prefix='sq_test_')

# 6.1 正常 zip 可以解压
good_zip = os.path.join(work, 'good.zip')
with zipfile.ZipFile(good_zip, 'w') as zf:
    zf.writestr('ffmpeg-6.0/bin/ffmpeg.exe', 'binary')
    zf.writestr('ffmpeg-6.0/README.txt', 'hello')
out_dir = os.path.join(work, 'out_good')
ok, err = safe_extract_zip(good_zip, out_dir)
check('safe_extract_zip 正常包解压成功', ok is True, str(err))
check('safe_extract_zip 保留目录结构',
      os.path.exists(os.path.join(out_dir, 'ffmpeg-6.0', 'bin', 'ffmpeg.exe')))
check('locate_ffmpeg_dir 能找到 bin 目录',
      locate_ffmpeg_dir(out_dir, 'ffmpeg-6.0') ==
      os.path.join(out_dir, 'ffmpeg-6.0', 'bin'))

# 6.2 Zip Slip：恶意条目必须被拒绝，且不能写出目标目录
bad_zip = os.path.join(work, 'evil.zip')
with zipfile.ZipFile(bad_zip, 'w') as zf:
    zf.writestr('../evil_outside.txt', 'pwned')
    zf.writestr('ffmpeg/bin/ffmpeg.exe', 'binary')
evil_dir = os.path.join(work, 'out_evil')
ok_bad, err_bad = safe_extract_zip(bad_zip, evil_dir)
outside = os.path.join(work, 'evil_outside.txt')
check('safe_extract_zip 拒绝 Zip Slip', ok_bad is False, str(ok_bad))
check('safe_extract_zip 拒绝时给出可读原因', '非法路径' in (err_bad or ''), str(err_bad))
check('safe_extract_zip 未写出目标目录之外的文件', not os.path.exists(outside))

# 6.3 原子安装：坏的可执行文件不能顶掉已装好的版本
target_bin = os.path.join(work, 'installed', 'bin')
os.makedirs(target_bin)
with open(os.path.join(target_bin, 'ffmpeg.exe'), 'w') as fh:
    fh.write('old')
bad_src = os.path.join(work, 'bad_src')
os.makedirs(bad_src)
with open(os.path.join(bad_src, 'ffmpeg.exe'), 'w') as fh:
    fh.write('not executable')
ok_install, err_install = install_bin_atomically(bad_src, target_bin)
check('install_bin_atomically 坏文件被拒绝', ok_install is False, str(ok_install))
check('install_bin_atomically 失败原因包含“保留”', '保留' in (err_install or ''), str(err_install))
check('install_bin_atomically 原有安装未被破坏',
      os.path.exists(os.path.join(target_bin, 'ffmpeg.exe')))

# 6.4 原子安装：可执行文件正常替换
good_src = os.path.join(work, 'good_src')
os.makedirs(good_src)
real_exe = os.path.join(good_src, 'ffmpeg.exe')
real_bat = os.path.join(good_src, 'ffmpeg.bat')
if os.name == 'nt':
    with open(real_bat, 'w') as fh:
        fh.write('@echo off\r\necho ffmpeg version 6.0\r\n')
else:
    with open(real_exe, 'w') as fh:
        fh.write('#!/bin/sh\necho ffmpeg version 6.0\n')
    os.chmod(real_exe, 0o755)
ok_install2, err_install2 = install_bin_atomically(good_src, target_bin)
check('install_bin_atomically 可用文件替换成功', ok_install2 is True, str(err_install2))
check('install_bin_atomically 替换后残留 .old 已清理',
      not os.path.exists(target_bin + '.old'))

shutil.rmtree(work, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks, len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
