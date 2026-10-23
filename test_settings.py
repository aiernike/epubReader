# -*- coding: utf-8 -*-
"""配置持久化与日志测试（阶段 2 收尾）。

覆盖 ConfigStore 的正常/损坏/缺失/权限异常路径，以及日志文件创建与轮转配置。
应用侧的接线用源码断言（无法在无 GUI 环境实例化 Tk 窗口）。
"""
import ast
import importlib.util
import io
import json
import os
import sys
import tempfile
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

with io.open(SRC, encoding='utf-8') as fh:
    source = fh.read()
tree = ast.parse(source)

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''))
    if not cond:
        failures.append(label)


work = tempfile.mkdtemp(prefix='cfg_test_')
ConfigStore = mod.ConfigStore

print('\n[1] ConfigStore 正常读写')
path = os.path.join(work, 'config.json')
store = ConfigStore(path)
check('缺失文件时使用空配置', store.data == {}, store.data)
store.update(tts_engine='gtts', voice='zh-CN - 中文', chars_per_line=20,
             concurrent_threads=4, output_format='M4B', output_dir=work,
             album='专辑', artist='作者')
check('update 生效', store.get('tts_engine') == 'gtts')
check('save 返回成功', store.save() is True)
check('配置文件已写入', os.path.exists(path))
with io.open(path, encoding='utf-8') as fh:
    payload = json.load(fh)
check('落盘内容为合法 JSON 且包含设置',
      payload.get('voice') == 'zh-CN - 中文' and payload.get('output_format') == 'M4B',
      payload)
check('没有残留 .tmp 文件', not os.path.exists(path + '.tmp'))

reloaded = ConfigStore(path)
check('重新加载得到相同设置', reloaded.get('tts_engine') == 'gtts'
      and reloaded.get('chars_per_line') == 20, reloaded.data)

print('\n[2] 损坏 / 异常输入不能阻断启动')
bad_path = os.path.join(work, 'bad.json')
with io.open(bad_path, 'w', encoding='utf-8') as fh:
    fh.write('{ this is not json')
bad_store = ConfigStore(bad_path)
check('损坏 JSON 回退为空配置', bad_store.data == {}, bad_store.data)
check('损坏配置下 save 仍可用', bad_store.save() is True)

list_path = os.path.join(work, 'list.json')
with io.open(list_path, 'w', encoding='utf-8') as fh:
    fh.write('[1, 2, 3]')
check('非字典内容被安全忽略', ConfigStore(list_path).data == {})

unknown_path = os.path.join(work, 'unknown.json')
with io.open(unknown_path, 'w', encoding='utf-8') as fh:
    json.dump({'tts_engine': 'edge-tts', '__evil__': 'x', 'password': 'y'}, fh)
unknown = ConfigStore(unknown_path)
check('白名单外的键被丢弃',
      set(unknown.data) == {'tts_engine'}, unknown.data)

print('\n[3] 保存失败不影响使用')
# 指向一个不可能写入的目录
locked = ConfigStore(os.path.join(work, 'a.txt', 'nested', 'config.json'))
open(os.path.join(work, 'a.txt'), 'w').close()
check('不可写路径下 save 返回 False 而不抛异常', locked.save() is False)

print('\n[4] 日志配置')
log_path = os.path.join(work, 'app.log')
logger = mod.setup_logging(log_path)
logger.info('测试日志 %s', 'OK')
for handler in logger.handlers:
    try:
        handler.flush()
    except Exception:
        pass
check('日志文件已创建', os.path.exists(log_path))
with io.open(log_path, encoding='utf-8') as fh:
    content = fh.read()
check('日志内容可读且包含写入的消息', '测试日志 OK' in content, content[:200])
check('重复调用 setup_logging 不会重复添加 handler',
      mod.setup_logging(log_path) is logger)

print('\n[5] 应用接线（源码断言）')
funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for name in ('_collect_settings', '_load_persisted_settings', 'save_settings',
             'new_temp_path', 'temp_subdir', 'cleanup_session_temp_dir',
             'submit_scheduler_task'):
    check('存在方法 %s' % name, name in funcs)

closing = {n.name: ast.get_source_segment(source, n)
           for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name in ('on_closing', '_teardown')}
# 收尾步骤由 on_closing 委托给 _teardown，断言必须合并两者，否则搬运会被误判成删除
closing_src = '\n'.join(closing.get(n) or '' for n in ('on_closing', '_teardown'))
check('关闭流程含 on_closing 与 _teardown',
      all(closing.get(n) for n in ('on_closing', '_teardown')), sorted(closing))
check('关闭流程保存设置', 'save_settings' in closing_src)
check('关闭流程清理实例临时目录', 'cleanup_session_temp_dir' in closing_src)

main_fn = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
main_src = ast.get_source_segment(source, main_fn) if main_fn else ''
check('main 中初始化日志', 'setup_logging' in main_src, main_src[:120])

check('AppConfig 提供配置与日志路径',
      hasattr(mod.AppConfig, 'CONFIG_PATH') and hasattr(mod.AppConfig, 'LOG_PATH'))

print('\n[6] 配置常量与默认值一致性')
check('默认引擎在注册表中', mod.AppConfig.DEFAULT_TTS_ENGINE in mod.TTS_ENGINES)
for engine_name in ('edge-tts', 'gtts', 'pyttsx3'):
    voices = mod.get_tts_engine(engine_name).get_available_voices()
    check('%s 有可用语音/语言' % engine_name, len(voices) > 0)
check('edge-tts 与 STATIC_VOICES 一致',
      mod.EdgeTTSEngine.get_available_voices() == list(mod.EdgeTTSEngine.STATIC_VOICES))
check('gtts 与 STATIC_VOICES 一致',
      mod.GoogleTTSEngine.get_available_voices() == list(mod.GoogleTTSEngine.STATIC_VOICES))

import shutil
shutil.rmtree(work, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks[0], len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
