# -*- coding: utf-8 -*-
"""生成应用图标（书 + 声波），输出 app.ico 多尺寸。

用法：.venv\\Scripts\\python.exe make_icon.py
"""
import math
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'app.ico')
PNG_OUT = os.path.join(HERE, 'app_icon.png')

ACCENT = (47, 111, 235)
ACCENT_DARK = (25, 70, 165)
PAPER = (247, 250, 255)
LINES = (200, 214, 238)
SPINE = (214, 224, 244)


def draw_icon(size):
    scale = 8
    big = size * scale
    img = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角底
    draw.rounded_rectangle([0, 0, big - 1, big - 1],
                           radius=int(big * 0.22), fill=ACCENT)

    # 书本（略偏左，给右侧声波留位置）
    left = big * 0.14
    right = big * 0.62
    top = big * 0.26
    bottom = big * 0.74
    draw.rounded_rectangle([left, top, right, bottom],
                           radius=int(big * 0.03), fill=PAPER)

    # 中缝
    cx = (left + right) / 2
    draw.line([(cx, top), (cx, bottom)], fill=SPINE, width=max(1, int(big * 0.010)))

    # 文字行
    for i in range(4):
        y = top + (bottom - top) * (0.24 + i * 0.175)
        draw.line([(left + (right - left) * 0.12, y),
                   (right - (right - left) * 0.12, y)],
                  fill=LINES, width=max(1, int(big * 0.020)))

    # 右侧声波：三条细弧做点缀，圆心在书脊右侧
    ox = right * 0.94
    oy = (top + bottom) / 2
    for radius, width, color in (
            (big * 0.20, big * 0.020, ACCENT_DARK),
            (big * 0.27, big * 0.020, ACCENT_DARK),
            (big * 0.34, big * 0.018, (12, 48, 120))):
        draw.arc([ox - radius, oy - radius, ox + radius, oy + radius],
                 start=-40, end=40, fill=color, width=max(1, int(width)))

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [draw_icon(s) for s in sizes]
    frames[-1].save(OUT, format='ICO',
                    sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    frames[-1].save(PNG_OUT, format='PNG')
    print('已生成 %s（%d 尺寸）与 %s' % (OUT, len(sizes), PNG_OUT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
