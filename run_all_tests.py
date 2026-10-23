# -*- coding: utf-8 -*-
"""一键运行全部测试。

用法：
    py -3 run_all_tests.py ["D:\\SynologyDrive\\图片\\epub reader.py"]
"""
import io
import os
import re
import subprocess
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'
HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ('test_pure_logic.py', '纯逻辑：分块/字幕/文件名/并发工具/语法安全', False),
    ('test_scheduler.py', '调度器：并发/取消/超时/关闭/进度', False),
    ('test_tempfiles.py', '临时文件：实例级目录/去 mktemp/清理', False),
    ('test_m4b.py', 'M4B 合并：命令构造/取消/章节校验（假 ffmpeg）', False),
    ('test_settings.py', '配置持久化与日志', False),
    ('import_check.py', '整模块导入（桩补齐缺失依赖）', True),
    ('test_close_robustness.py', '退出收尾与 Tk 回调异常（真实窗口）', False),
    ('test_m4b_real.py', 'M4B 实测：真实 ffmpeg + ffprobe 断言章节（需 ffmpeg）', False),
    ('test_e2e_tts.py', '端到端：真实 edge_tts 分块合成（需联网）', False),
    ('test_gui_integration.py', 'GUI 实机：真实窗口走完 8 个用户流程（需显示器）', False),
]

env = dict(os.environ)
env['PYTHONIOENCODING'] = 'utf-8'

def syntax_check(path):
    """在内存中做语法检查。

    不用 py_compile：它会往源文件同级的 __pycache__ 写 .pyc，
    而源文件位于 Synology Drive 共享目录时该目录不可写。
    """
    try:
        with io.open(path, encoding='utf-8') as fh:
            source = fh.read()
        compile(source, path, 'exec')
        return True, ''
    except SyntaxError as exc:
        return False, '%s:%s %s' % (exc.filename, exc.lineno, exc.msg)
    except OSError as exc:
        return False, str(exc)


print('目标文件:', SRC)
print('=' * 68)

total_checks = 0
failed_suites = []
skipped = []

ok_syntax, syntax_error = syntax_check(SRC)
print('%-38s %s' % ('语法检查（内存编译）', 'OK' if ok_syntax else 'FAILED'))
if not ok_syntax:
    print(syntax_error)
    sys.exit(1)

for script, desc, is_import_check in SUITES:
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        skipped.append(script)
        continue
    if '--fast' in sys.argv and script in ('test_e2e_tts.py', 'test_gui_integration.py'):
        skipped.append(script + '（--fast）')
        continue

    result = subprocess.run([sys.executable, path, SRC],
                            capture_output=True, text=True, env=env,
                            encoding='utf-8', errors='replace')
    output = (result.stdout or '') + (result.stderr or '')

    if is_import_check:
        ok = 'IMPORT OK' in output
        summary = 'IMPORT OK' if ok else 'IMPORT FAILED'
    else:
        match = re.search(r'(\d+) checks?\s*[,/]\s*(\d+) failures?', output)
        if match:
            checks, fails = int(match.group(1)), int(match.group(2))
            total_checks += checks
            ok = (result.returncode == 0 and fails == 0)
            summary = '%d checks, %d failures' % (checks, fails)
        elif '跳过' in output and result.returncode == 0:
            ok = True
            summary = 'SKIPPED（环境不满足）'
        else:
            ok = False
            summary = '未找到测试结果（退出码 %s）' % result.returncode

    print('%-24s %-6s %-30s %s' % (
        script, 'PASS' if ok else 'FAIL', summary, desc))
    if not ok:
        failed_suites.append(script)
        for line in output.splitlines():
            if 'FAIL' in line or 'Error' in line or 'Traceback' in line:
                print('    | %s' % line.strip())

print('=' * 68)
if skipped:
    print('跳过: %s' % ', '.join(skipped))
print('累计断言: %d   失败套件: %d' % (total_checks, len(failed_suites)))
sys.exit(1 if failed_suites else 0)
