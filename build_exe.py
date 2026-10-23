# -*- coding: utf-8 -*-
"""一键打包：把目标源码同步到本目录，再用 PyInstaller 构建 exe。

不要在 spec 里直接引用 Synology 路径：PyInstaller 对含空格/中文的脚本路径
处理不佳，且外部目录可能被同步进程锁定。

用法：
    .venv\\Scripts\\python.exe build_exe.py [源文件路径]
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = r'D:\SynologyDrive\图片\epub reader.py'
LOCAL_MODULE = os.path.join(HERE, 'epubReader.py')


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    if not os.path.exists(src):
        print('找不到源文件: %s' % src)
        return 2

    shutil.copyfile(src, LOCAL_MODULE)
    with open(LOCAL_MODULE, encoding='utf-8') as fh:
        compile(fh.read(), LOCAL_MODULE, 'exec')
    print('已同步源码 -> %s（%d 字节）' % (LOCAL_MODULE, os.path.getsize(LOCAL_MODULE)))

    for folder in ('build', 'dist'):
        target = os.path.join(HERE, folder)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)

    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
           'epubReader.spec']
    print('执行: %s' % ' '.join(cmd))
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print('构建失败，退出码 %d' % result.returncode)
        return result.returncode

    exe = os.path.join(HERE, 'dist', 'epubReader', 'epubReader.exe')
    if not os.path.exists(exe):
        print('构建结束但未找到 %s' % exe)
        return 3
    total = 0
    for base, _dirs, files in os.walk(os.path.dirname(exe)):
        for name in files:
            total += os.path.getsize(os.path.join(base, name))
    print('构建成功: %s' % exe)
    print('产物大小: %.1f MB' % (total / 1024 / 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
