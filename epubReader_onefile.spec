# -*- mode: python ; coding: utf-8 -*-
"""epubReader 单文件（onefile）打包配置 —— 给"双击就用"的发布形态。

与 `epubReader.spec`（onedir）的区别：

- **不把 ffmpeg/ffprobe 打进包**。onefile 每次启动都要把包内容解压到临时目录，
  塞进 ~200MB 的 ffmpeg 意味着每次启动解压 200MB，完全不能接受。
  程序本来就会按顺序找：随包目录 → `%APPDATA%\\epubReader\\ffmpeg\\bin`
  → 系统 PATH，找不到再提示下载。用户机器上通常已经有了。
- 产出单个 exe（约 30MB），放桌面双击即可，和早期的 v0.02/v0.03 形态一致。

用法：
    python -m PyInstaller --clean --noconfirm --distpath dist_onefile \
        --workpath build_onefile epubReader_onefile.spec
"""
import os

HERE = SPECPATH

datas = [(os.path.join(HERE, 'app.ico'), '.')]

# 与 onedir 版保持一致：只列真正需要的隐式依赖
hiddenimports = [
    'bs4', 'lxml', 'lxml.etree', 'lxml._elementpath',
    'ebooklib', 'ebooklib.epub',
    'mutagen', 'mutagen.mp3', 'mutagen.id3',
    'edge_tts', 'aiohttp', 'requests',
    'PIL', 'PIL.Image', 'PIL.ImageTk',
    'charset_normalizer', 'multidict', 'yarl', 'frozenlist', 'aiosignal',
    'tabulate', 'typing_extensions',
]

excludes = [
    'numpy', 'pandas', 'matplotlib', 'scipy', 'IPython', 'jupyter',
    'pytest', 'unittest', 'pydoc', 'doctest', 'test',
    'PIL.ImageQt', 'PIL.ImageShow', 'PyQt5', 'PySide2', 'pygame',
    'gtts', 'pyttsx3', 'setuptools', 'pip',
]

a = Analysis(
    ['epubReader.py'],
    pathex=[HERE],
    binaries=[],            # 刻意不打 ffmpeg，见文件头说明
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# onefile：binaries/zipfiles/datas 全部交给 EXE，不再走 COLLECT
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='epub reader v0.04',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, 'app.ico'),
)
