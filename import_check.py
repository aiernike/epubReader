# 用桩模块补齐缺失依赖，验证整个模块能否真正 import（阶段 2 改动的回归基线）
import sys, types

def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, item): return _Any()

stub('PIL')
stub('PIL.Image', open=lambda *a, **k: _Any(), Resampling=types.SimpleNamespace(LANCZOS=1))
stub('PIL.ImageTk', PhotoImage=_Any)
stub('bs4', BeautifulSoup=_Any)
stub('ebooklib', ITEM_COVER='cover', ITEM_DOCUMENT='doc')
stub('ebooklib.epub', read_epub=lambda *a, **k: _Any())
stub('mutagen')
stub('mutagen.mp3', MP3=_Any)
stub('mutagen.id3', ID3=_Any, APIC=_Any, TIT2=_Any, TPE1=_Any, TALB=_Any, USLT=_Any)
stub('requests', Session=types.SimpleNamespace(merge_environment_settings=_Any),
     get=_Any, exceptions=types.SimpleNamespace(Timeout=Exception, RequestException=Exception))

import importlib.util
spec = importlib.util.spec_from_file_location('epub_reader', r'D:\SynologyDrive\图片\epub reader.py')
mod = importlib.util.module_from_spec(spec)
sys.modules['epub_reader'] = mod
spec.loader.exec_module(mod)
print('IMPORT OK')
print('模块级名称数量:', len([n for n in vars(mod) if not n.startswith('__')]))
print('TaskScheduler:', mod.TaskScheduler)
print('AppConfig.CHARS_PER_SECOND =', mod.AppConfig.CHARS_PER_SECOND)
print('TEMP_DIR =', mod.TEMP_DIR)
print('可实例化 TaskScheduler:', bool(mod.TaskScheduler(max_concurrent_tts=2)))

