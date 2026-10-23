# -*- mode: python ; coding: utf-8 -*-
"""epubReader 打包配置（PyInstaller 5.x / Python 3.8）。

要点：
- 单文件 exe 会把 ~130MB 的 ffmpeg 解压到临时目录，启动很慢，因此用 onedir。
- ffmpeg/ffprobe 一并打包进 exe 目录，程序启动时会优先使用它，
  这样 M4B 导出无需联网下载。
- 排除 numpy/matplotlib/tkinter 测试等无关大包，控制体积。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# spec 文件里没有 __file__，PyInstaller 提供 SPECPATH 指向 spec 所在目录
HERE = SPECPATH

# ---------- ffmpeg ----------
def find_ffmpeg_dir():
    """挑一个要打包进 exe 的 ffmpeg/ffprobe 目录。

    优先 `ffmpeg_essentials\\`（gyan.dev essentials 构建，约 95MB/个，本程序需要的
    能力都有）；没有才回退到 `ffmpeg_local\\` 里下载的构建——后者是 BtbN 静态 GPL
    构建，单个约 160MB，打进去白多 130MB，而且本机上出现过起不来的情况。
    """
    for folder in ('ffmpeg_essentials', 'ffmpeg_local'):
        root = os.path.join(HERE, folder)
        if not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            if 'ffmpeg.exe' in files and 'ffprobe.exe' in files:
                return base
    return None


binaries = []
ffmpeg_dir = find_ffmpeg_dir()
if ffmpeg_dir:
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        path = os.path.join(ffmpeg_dir, name)
        if os.path.exists(path):
            binaries.append((path, 'ffmpeg'))

# ---------- 运行期数据 ----------
datas = [(os.path.join(HERE, 'app.ico'), '.')]
readme = os.path.join(HERE, '使用说明.txt')
if os.path.exists(readme):
    datas.append((readme, '.'))

# ---------- 隐式依赖 ----------
# 只列真正需要的：collect_submodules 会枚举 aiohttp/edge_tts 的全部可选依赖，
# 在这台机器上会让分析阶段长时间卡住，且绝大多数模块根本不会被导入。
hiddenimports = [
    'bs4', 'lxml', 'lxml.etree', 'lxml._elementpath',
    'ebooklib', 'ebooklib.epub',
    'mutagen', 'mutagen.mp3', 'mutagen.id3',
    'edge_tts', 'aiohttp', 'requests',
    'PIL', 'PIL.Image', 'PIL.ImageTk',
    'charset_normalizer', 'multidict', 'yarl', 'frozenlist', 'aiosignal',
    'tabulate', 'typing_extensions',
]

# ---------- 排除 ----------
excludes = [
    'numpy', 'pandas', 'matplotlib', 'scipy', 'IPython', 'jupyter',
    'pytest', 'unittest', 'pydoc', 'doctest', 'test',
    'PIL.ImageQt', 'PIL.ImageShow', 'PyQt5', 'PySide2', 'pygame',
    'gtts', 'pyttsx3', 'setuptools', 'pip',
]

a = Analysis(
    ['epubReader.py'],
    pathex=[HERE],
    binaries=binaries,
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='epubReader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口（GUI 程序）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, 'app.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='epubReader',
)
