# -*- coding: utf-8 -*-
"""用真实依赖构造测试用 EPUB（含合法封面、目录、多章节）。

封面用 zlib + CRC 现场生成，保证是真正合法的 PNG——
之前手工填的字节 CRC 不对，Pillow 会拒绝读取，反而暴露出一个问题：
封面异常不应中断章节列表等其余显示逻辑。

用法：
    python make_test_epub.py <输出路径>
"""
import binascii
import os
import struct
import sys
import zlib

from ebooklib import epub


def valid_png(width=16, height=16, rgb=(200, 40, 40)):
    """生成一张合法的最小 PNG（纯色）"""
    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', binascii.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = b''
    for _ in range(height):
        raw += b'\x00' + bytes(rgb) * width
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 9))
            + chunk(b'IEND', b''))


COVER_PNG = valid_png()

CHAPTERS = [
    ("第一章 起点", "这是第一章的开头。他把行李放下，望着窗外的雨。\n"
                    "雨声很轻，像有人在远处翻书。"),
    ("第二章 转折", "第二章写的是那封信。信上只有一句话：明天见。\n"
                    "他反复看了三遍，还是没明白。"),
    ("第三章 终点", "最后一章很短。他合上本子，走了出去。\n"
                    "外面已经放晴了。"),
]


def build(path):
    book = epub.EpubBook()
    book.set_identifier('test-epub-0001')
    book.set_title('测试书目：雨中的信')
    book.set_language('zh')
    book.add_author('测试作者')
    book.add_metadata('DC', 'publisher', '测试出版社')
    book.add_metadata('DC', 'date', '2024-05-06')

    book.set_cover('cover.png', COVER_PNG)

    items = []
    for index, (title, text) in enumerate(CHAPTERS, 1):
        chapter = epub.EpubHtml(
            title=title, file_name='chap_%02d.xhtml' % index, lang='zh')
        chapter.content = (
            '<html><head><title>%s</title></head><body>'
            '<h2>%s</h2><p>%s</p></body></html>'
            % (title, title, text.replace('\n', '</p><p>'))
        )
        book.add_item(chapter)
        items.append(chapter)

    book.toc = tuple(items)
    book.add_item(epub.EpubNcx())
    # spine 只放正文章节，避免导航文档被当成第 4 章
    book.spine = items

    epub.write_epub(path, book, {})
    return path


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else 'test_sample.epub'
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    build(target)
    print('已生成 %s（%d 字节，%d 章，封面 %d 字节）'
          % (target, os.path.getsize(target), len(CHAPTERS), len(COVER_PNG)))


if __name__ == '__main__':
    main()
