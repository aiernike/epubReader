# -*- coding: utf-8 -*-
"""临时文件治理测试（阶段 2-B）。

验证：
- 源码中不再有裸 tempfile.mktemp( 调用
- new_temp_path / temp_subdir 在实例级目录内分配且互不冲突
- cleanup_session_temp_dir 能真正删除目录，且对不存在/已删除的目录容错
- 两个实例的临时目录互不干扰（共享 TEMP_DIR 不再是清理目标）
"""
import ast
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'


def install_stubs():
    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class _Any(object):
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return _Any()

        def __getattr__(self, item):
            return _Any()

    stub('PIL')
    stub('PIL.Image', open=lambda *a, **k: _Any(),
         Resampling=types.SimpleNamespace(LANCZOS=1))
    stub('PIL.ImageTk', PhotoImage=_Any)
    stub('bs4', BeautifulSoup=_Any)
    stub('ebooklib', ITEM_COVER='cover', ITEM_DOCUMENT='doc')
    stub('ebooklib.epub', read_epub=lambda *a, **k: _Any())
    stub('mutagen')
    stub('mutagen.mp3', MP3=_Any)
    stub('mutagen.id3', ID3=_Any, APIC=_Any, TIT2=_Any, TPE1=_Any,
         TALB=_Any, USLT=_Any)
    stub('requests', Session=types.SimpleNamespace(merge_environment_settings=_Any),
         get=_Any,
         exceptions=types.SimpleNamespace(Timeout=Exception, RequestException=Exception))


install_stubs()
spec = importlib.util.spec_from_file_location('epub_reader', SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules['epub_reader'] = mod
spec.loader.exec_module(mod)

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''))
    if not cond:
        failures.append(label)


with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)

print('\n[1] 源码层面：不再有 TOCTOU 风险的 mktemp')
bare_mktemp = []
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'mktemp'):
        bare_mktemp.append(node.lineno)
check('不存在 tempfile.mktemp( 调用', not bare_mktemp, bare_mktemp)
check('仍使用 mkstemp 分配唯一文件',
      'tempfile.mkstemp(' in source)

print('\n[2] 实例级目录：new_temp_path / temp_subdir')
epub_app = mod.EpubToMp3App


class FakeApp(object):
    """只借用 EpubToMp3App 上的临时文件方法，不构造真实 GUI"""

    new_temp_path = epub_app.new_temp_path
    temp_subdir = epub_app.temp_subdir
    cleanup_session_temp_dir = epub_app.cleanup_session_temp_dir

    def __init__(self):
        self.session_temp_dir = tempfile.mkdtemp(prefix='epubReader_test_%d_' % os.getpid())
        self._session_temp_counter = 0
        self._session_temp_lock = threading.Lock()


app1 = FakeApp()
app2 = FakeApp()
check('两个实例的临时目录不同', app1.session_temp_dir != app2.session_temp_dir)

p1 = app1.new_temp_path(suffix='.mp3', prefix='preview')
check('new_temp_path 返回的文件已创建', os.path.exists(p1))
check('返回路径位于实例目录内',
      os.path.dirname(os.path.abspath(p1)) == os.path.abspath(app1.session_temp_dir))
check('文件后缀正确', p1.endswith('.mp3'), p1)

p2 = app1.new_temp_path(suffix='.mp3', prefix='preview')
check('同一实例两次分配不冲突', p1 != p2)

sub = app1.temp_subdir('chapter')
check('temp_subdir 创建了子目录', os.path.isdir(sub))
check('temp_subdir 位于实例目录内',
      os.path.dirname(os.path.abspath(sub)) == os.path.abspath(app1.session_temp_dir))
check('两次 temp_subdir 名称不同', sub != app1.temp_subdir('chapter'))

print('\n[3] 清理语义')
with open(os.path.join(app2.session_temp_dir, 'x.bin'), 'wb') as fh:
    fh.write(b'x' * 16)
app2.cleanup_session_temp_dir()
check('cleanup 删除实例目录', not os.path.exists(app2.session_temp_dir))
app2.cleanup_session_temp_dir()  # 再删一次不应抛异常
check('重复清理不抛异常', True)
check('另一个实例的目录未受影响', os.path.isdir(app1.session_temp_dir),
      app1.session_temp_dir)

# 共享 TEMP_DIR 不再作为清理目标
shared = mod.AppConfig.TEMP_DIR
before_shared = os.path.isdir(shared)
app1.cleanup_session_temp_dir()
check('cleanup 不会碰共享 TEMP_DIR',
      os.path.isdir(shared) == before_shared,
      'TEMP_DIR 状态被改变: %s' % shared)

print('\n[4] 关闭流程不再删除共享目录')
# 收尾步骤在 _teardown() 里、由 on_closing() 委托过去，两段合起来才是"关闭流程"；
# 只看 on_closing 会把搬运当成"逻辑被删掉"，所以这里合并源码再断言。
CLOSE_FNS = ('on_closing', '_teardown')
close_parts = {}
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in CLOSE_FNS:
        close_parts[node.name] = ast.get_source_segment(source, node)
close_src = '\n'.join(close_parts.get(n) or '' for n in CLOSE_FNS)
check('关闭流程含 on_closing 与 _teardown',
      all(close_parts.get(n) for n in CLOSE_FNS), sorted(close_parts))
check('关闭流程调用 cleanup_session_temp_dir',
      'cleanup_session_temp_dir' in close_src)
check('on_closing 把收尾委托给 _teardown', '_teardown' in (close_parts.get('on_closing') or ''),
      (close_parts.get('on_closing') or '')[-200:])
check('关闭流程不再 rmtree(TEMP_DIR)',
      'rmtree(TEMP_DIR)' not in close_src,
      close_src[-200:] if close_src else 'not found')

shutil.rmtree(app1.session_temp_dir, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks[0], len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
