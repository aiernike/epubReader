#!/usr/bin/env python3
"""
epubReader - EPUB电子书阅读器和有声书生成工具
功能：
1. 打开并阅读EPUB电子书，支持封面显示、章节列表浏览和章节内容编辑
2. 将章节内容转换成有声书MP3，自动生成LRC字幕，添加ID3标签
3. 支持批量转换模式和单章节转换模式
4. 支持M4B有声书格式导出，包含完整章节信息
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from tkinter import font
from PIL import Image, ImageTk
import zipfile
import io
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import re
import shutil
import ebooklib
from ebooklib import epub
import tempfile
import threading
import asyncio
import edge_tts
import sys, os
import time
from datetime import datetime
import queue
import traceback
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, USLT
import aiohttp
import subprocess
import json
import urllib.request
import zipfile as zipfile_lib
from pathlib import Path
import uuid

# ===== 配置选项 =====
class AppConfig:
    """应用配置类 - 集中管理所有配置项"""
    
    # TTS配置
    DEFAULT_VOICE = "zh-CN-YunjianNeural"  # edge-tts 默认语音
    MAX_RETRIES = 3  # TTS最大重试次数
    RETRY_DELAY_SECONDS = 5  # TTS重试间隔（秒）
    CHARS_PER_SECOND = 0.3  # 估算每个字的朗读时间（秒）
    
    # 文件配置
    DEFAULT_ARTIST = "未知作者"  # MP3默认艺术家
    DEFAULT_CHARS_PER_LINE = 15  # LRC歌词每行字数
    MAX_FILENAME_LENGTH = 200  # 文件名最大长度
    TEMP_DIR = os.path.join(tempfile.gettempdir(), "epub2mp3")  # 临时目录
    LYRIC_LANG = "zho"  # 歌词语言代码
    
    # FFmpeg配置
    FFMPEG_DIR = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'epubReader', 'ffmpeg')
    FFMPEG_PATH = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe') if sys.platform == 'win32' else 'ffmpeg'
    FFMPEG_DOWNLOAD_URLS = [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://mirror.ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://gh.api.99988866.xyz/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ]
    DOWNLOAD_TIMEOUT = 30  # 下载超时时间（秒）
    DOWNLOAD_MAX_RETRIES = 3  # 下载最大重试次数

# 向后兼容：保留全局变量
DEFAULT_VOICE = AppConfig.DEFAULT_VOICE
DEFAULT_ARTIST = AppConfig.DEFAULT_ARTIST
DEFAULT_CHARS_PER_LINE = AppConfig.DEFAULT_CHARS_PER_LINE
MAX_RETRIES = AppConfig.MAX_RETRIES
RETRY_DELAY_SECONDS = AppConfig.RETRY_DELAY_SECONDS
TEMP_DIR = AppConfig.TEMP_DIR
LYRIC_LANG = AppConfig.LYRIC_LANG
FFMPEG_DIR = AppConfig.FFMPEG_DIR
FFMPEG_PATH = AppConfig.FFMPEG_PATH
FFMPEG_DOWNLOAD_URLS = AppConfig.FFMPEG_DOWNLOAD_URLS
DOWNLOAD_TIMEOUT = AppConfig.DOWNLOAD_TIMEOUT
DOWNLOAD_MAX_RETRIES = AppConfig.DOWNLOAD_MAX_RETRIES


# ===== EPUB处理模块 =====
class EpubReader:
    """负责EPUB文件的解析和内容提取"""

    def __init__(self, epub_filepath):
        self.epub_filepath = epub_filepath
        self.epub_zip = None
        self.book = None
        self.opf_dir = None
    
    def __enter__(self):
        """上下文管理器入口"""
        self.open()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()
        return False

    def open(self):
        """打开并解析EPUB文件的基本结构"""
        try:
            # 使用 ebooklib 打开 EPUB 文件
            self.book = epub.read_epub(self.epub_filepath)

            # 我们仍然需要 zipfile 对象来访问原始文件，特别是封面图片
            self.epub_zip = zipfile.ZipFile(self.epub_filepath, 'r')

            # 解析 container.xml 来确定 content.opf 的位置，这主要用于封面图片提取
            try:
                container = self.epub_zip.read('META-INF/container.xml')
                container_root = ET.fromstring(container)
                # 寻找 opf 文件的 full-path
                rootfile = container_root.find(
                    './/{urn:oasis:names:tc:opendocument:xmlns:container}rootfiles/{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')
                if rootfile is not None:
                    content_path = rootfile.get('full-path')
                    self.opf_dir = os.path.dirname(content_path)
                    if self.opf_dir and not self.opf_dir.endswith('/'):
                        self.opf_dir += '/'
                else:
                    # 如果找不到 rootfile 元素，尝试查找任何 full-path 属性
                    content_path_element = container_root.find('.//*[@full-path]')
                    if content_path_element is not None:
                        content_path = content_path_element.get('full-path')
                        self.opf_dir = os.path.dirname(content_path)
                        if self.opf_dir and not self.opf_dir.endswith('/'):
                            self.opf_dir += '/'
                    else:
                        print("Warning: Could not find full-path in container.xml")
                        self.opf_dir = ""  # Default to root
            except (KeyError, ET.ParseError) as e:
                print(f"解析container.xml时出错: {e}")
                # Fallback: attempt to find opf directly
                for name in self.epub_zip.namelist():
                    if name.lower().endswith('.opf'):
                        self.opf_dir = os.path.dirname(name)
                        if self.opf_dir and not self.opf_dir.endswith('/'):
                            self.opf_dir += '/'
                        break
                if not self.opf_dir:
                    print("Warning: Could not find any .opf file.")
                    self.opf_dir = ""  # Default to root

            return True
        except zipfile.BadZipFile as e:
            print(f"打开EPUB文件失败: 文件不是有效的ZIP格式 - {e}")
            traceback.print_exc()
            return False
        except FileNotFoundError as e:
            print(f"打开EPUB文件失败: 文件不存在 - {e}")
            return False
        except PermissionError as e:
            print(f"打开EPUB文件失败: 权限不足 - {e}")
            return False
        except Exception as e:
            print(f"打开EPUB文件失败: {e}")
            traceback.print_exc()
            if self.epub_zip:
                self.epub_zip.close()
            return False

    def close(self):
        """关闭EPUB文件"""
        if self.epub_zip:
            self.epub_zip.close()

    def get_title(self):
        """获取书籍标题"""
        try:
            # ebooklib 的 get_metadata 返回 [(value, {'attribute': 'value'}), ...]
            metadata = self.book.get_metadata('DC', 'title')
            if metadata:
                return metadata[0][0] or "未知书名"
            return "未知书名"
        except Exception as e:
            print(f"获取标题时出错: {e}")
            return "未知书名"

    def get_author(self):
        """获取作者"""
        try:
            metadata = self.book.get_metadata('DC', 'creator')
            if metadata:
                return metadata[0][0] or "未知作者"
            return "未知作者"
        except Exception as e:
            print(f"获取作者时出错: {e}")
            return "未知作者"

    def get_publisher(self):
        """获取出版社"""
        try:
            metadata = self.book.get_metadata('DC', 'publisher')
            if metadata:
                return metadata[0][0] or "未知出版社"
            return "未知出版社"
        except Exception as e:
            print(f"获取出版社时出错: {e}")
            return "未知出版社"

    def get_publish_date(self):
        """获取出版时间，只保留年月日"""
        try:
            metadata = self.book.get_metadata('DC', 'date')
            if metadata:
                raw_date = metadata[0][0] or "未知时间"
                # 用正则提取YYYY-MM-DD
                match = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
                if match:
                    return match.group(1)
                return raw_date
            return "未知时间"
        except Exception as e:
            print(f"获取出版时间时出错: {e}")
            return "未知时间"

    def get_cover_data(self):
        """获取封面图片数据 - 优先使用 ebooklib"""
        try:
            # 首先尝试使用 ebooklib 获取封面
            for item in self.book.get_items():
                if item.get_type() == ebooklib.ITEM_COVER:
                    return item.get_content()

            # 如果 ebooklib 无法获取封面，使用原始方法（通过 zipfile）
            if not self.epub_zip:
                print("Warning: zipfile not open, cannot use fallback cover extraction.")
                return None

            # 方法1: 通过meta标签寻找cover ID
            root = None
            opf_path = self.opf_dir + "content.opf" if self.opf_dir else "content.opf"
            try:
                content_opf = self.epub_zip.read(opf_path)
                root = ET.fromstring(content_opf)
                namespaces = {'opf': 'http://www.idpf.org/2007/opf'}

                metadata = root.find('.//opf:metadata', namespaces) or root.find(
                    './/*{http://www.idpf.org/2007/opf}metadata')
                manifest = root.find('.//opf:manifest', namespaces) or root.find(
                    './/*{http://www.idpf.org/2007/opf}manifest')

                cover_id = None
                if metadata:
                    for meta in metadata.findall('.//opf:meta', namespaces) or metadata.findall(
                            './/*{http://www.idpf.org/2007/opf}meta') or []:
                        if meta.get('name') == 'cover':
                            cover_id = meta.get('content')
                            break

                    # 如果找到cover ID，寻找对应资源
                    if cover_id and manifest:
                        for item in manifest.findall('.//opf:item', namespaces) or manifest.findall(
                                './/*{http://www.idpf.org/2007/opf}item') or []:
                            if item.get('id') == cover_id:
                                cover_href = item.get('href')
                                cover_path = os.path.join(self.opf_dir, cover_href).replace('\\', '/')
                                try:
                                    return self.epub_zip.read(cover_path)
                                except:
                                    try:
                                        return self.epub_zip.read(cover_href)
                                    except:
                                        pass

                # 方法2: 寻找具有cover-image属性的项 (EPUB3)
                if manifest:
                    for item in manifest.findall('.//opf:item', namespaces) or manifest.findall(
                            './/*{http://www.idpf.org/2007/opf}item') or []:
                        if 'cover-image' in item.get('properties', '').split():  # Check for 'cover-image' property
                            cover_href = item.get('href')
                            cover_path = os.path.join(self.opf_dir, cover_href).replace('\\', '/')
                            try:
                                return self.epub_zip.read(cover_path)
                            except:
                                try:
                                    return self.epub_zip.read(cover_href)
                                except:
                                    pass

                # 方法3: 按名称查找
                if manifest:
                    for item in manifest.findall('.//opf:item', namespaces) or manifest.findall(
                            './/*{http://www.idpf.org/2007/opf}item') or []:
                        item_id = item.get('id', '').lower()
                        item_href = item.get('href', '').lower()
                        media_type = item.get('media-type', '')

                        if (media_type.startswith('image/') and
                                ('cover' in item_id or 'cover' in item_href)):
                            cover_href = item.get('href')
                            cover_path = os.path.join(self.opf_dir, cover_href).replace('\\', '/')
                            try:
                                return self.epub_zip.read(cover_path)
                            except:
                                try:
                                    return self.epub_zip.read(cover_href)
                                except:
                                    pass

                # 方法4: 在整个epub中查找cover图片
                for name in self.epub_zip.namelist():
                    if name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                        if 'cover' in name.lower():
                            return self.epub_zip.read(name)

                # 方法5: 查找任何图片作为封面 (最后手段)
                for name in self.epub_zip.namelist():
                    if name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                        return self.epub_zip.read(name)

            except KeyError:
                print(f"Warning: content.opf not found at {opf_path}")
            except Exception as e:
                print(f"获取封面数据（fallback）时出错: {e}")
                traceback.print_exc()

        except Exception as e:
            print(f"获取封面数据时出错: {e}")
            traceback.print_exc()

        return None

    def get_chapters(self):
        """获取所有章节，使用 ebooklib"""
        try:
            chapters = []

            # 获取所有文档项，使用 toc 顺序或默认顺序
            docs = []
            # 首先尝试使用 toc
            toc_items = self.book.toc
            processed_hrefs = set()  # 记录已经通过 toc 处理过的 href

            def process_toc_item(toc_item):
                if isinstance(toc_item, tuple) and len(toc_item) > 1:
                    section_title, href = toc_item[0], toc_item[1]
                    # 查找对应的文档
                    for item in self.book.get_items():
                        # item.get_name() 返回的是内部路径，需要和 toc 中的 href 比较
                        if item.get_type() == ebooklib.ITEM_DOCUMENT and item.get_name() == href:
                            if href not in processed_hrefs:
                                docs.append((section_title, item))
                                processed_hrefs.add(href)
                            break
                # 递归处理子章节 (ebooklib toc 可以有嵌套结构)
                if isinstance(toc_item, tuple) and len(toc_item) > 2:
                    for sub_item in toc_item[2]:
                        process_toc_item(sub_item)

            for toc_item in toc_items:
                process_toc_item(toc_item)

            # 如果 toc 没有覆盖所有文档项，添加剩余的文档项
            all_document_items = [item for item in self.book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]
            for item in all_document_items:
                if item.get_name() not in processed_hrefs:
                    # 对于这些项，我们没有 toc 提供的标题，先用 None
                    docs.append((None, item))

            # 处理所有文档内容
            for title, doc in docs:
                try:
                    # 确保内容是字符串
                    content = doc.get_content()
                    if isinstance(content, bytes):
                        content = content.decode('utf-8', errors='ignore')

                    soup = BeautifulSoup(content, 'html.parser')
                    text_content = soup.get_text(separator='\n', strip=True)

                    # 跳过空内容
                    if not text_content or text_content.strip() == "":
                        continue

                    # 确定章节标题
                    chapter_title = title
                    if not chapter_title:
                        # 尝试从文档内容中获取标题
                        for header in ['h1', 'h2', 'h3', 'h4', 'title']:
                            if soup.find(header):
                                chapter_title = soup.find(header).get_text(strip=True)
                                if chapter_title and chapter_title.strip() != "":
                                    break

                        # 如果找不到标题，使用第一个非空行
                        if not chapter_title or chapter_title.strip() == "":
                            lines = text_content.split('\n')
                            for line in lines:
                                if line.strip():
                                    chapter_title = line.strip()
                                    break

                        # 如果仍然找不到标题，使用文件名
                        if not chapter_title or chapter_title.strip() == "":
                            chapter_title = doc.get_name()

                    chapters.append({
                        'title': chapter_title,
                        'content': text_content
                    })
                except Exception as e:
                    print(f"处理章节 {doc.get_name()} 时出错: {e}")
                    continue

            return chapters
        except Exception as e:
            print(f"获取章节时出错: {e}")
            traceback.print_exc()
            return []


def parse_epub_data(epub_filepath):
    """使用EpubReader类解析EPUB文件"""
    reader = EpubReader(epub_filepath)

    try:
        if reader.open():
            title = reader.get_title()
            author = reader.get_author()
            publisher = reader.get_publisher()
            publish_date = reader.get_publish_date()
            cover_data = reader.get_cover_data()
            chapters = reader.get_chapters()
            total_words = 0
            if chapters:
                for chapter in chapters:
                    total_words += len(chapter.get('content', '').replace('\n', '').replace('\r', '').replace(' ', ''))

            return {
                'title': title,
                'author': author,
                'publisher': publisher,
                'publish_date': publish_date,
                'cover_data': cover_data,
                'chapters': chapters,  # chapters 是一个列表，每个元素是 {'title': '...', 'content': '...'}
                'total_words': total_words
            }
    except Exception as e:
        print(f"解析EPUB数据时出错: {e}")
        traceback.print_exc()
    finally:
        reader.close()

    return None


# ===== 音频转换模块 =====
def convert_srt_to_lrc(srt_content, chars_per_line):
    """SRT->LRC并合并行"""
    srt_blocks = srt_content.strip().split('\n\n')
    all_items = []
    for block in srt_blocks:
        lines = block.split('\n')
        if len(lines) >= 3 and lines[1].strip():
            time_line = lines[1]
            # 支持多行字幕：合并第2行及之后的所有行
            text_lines = lines[2:]
            text_line = ''.join(text_lines)  # 直接拼接，不加空格
            match = re.search(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', time_line)
            if match:
                hours, minutes, seconds, milliseconds = map(int, match.groups())
                time_ms = (hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds
                all_items.append((time_ms, text_line))
    all_items.sort(key=lambda x: x[0])
    merged_items = []
    current_text = ""
    current_time = None
    if not all_items:
        return ""
    for i, (time_ms, text) in enumerate(all_items):
        if not current_text:
            current_time = time_ms
        current_text += text
        if len(current_text) >= chars_per_line or i == len(all_items) - 1:
            if current_text:
                merged_items.append((current_time, current_text))
            current_text = ""
    lrc_lines = []
    for time_ms, text in merged_items:
        minutes = (time_ms // 1000) // 60
        seconds = (time_ms // 1000) % 60
        milliseconds = (time_ms % 1000) // 10
        lrc_time = f"[{minutes:02d}:{seconds:02d}.{milliseconds:02d}]"
        lrc_lines.append(f"{lrc_time}{text}")
    return '\n'.join(lrc_lines)


def sanitize_filename(filename, max_length=None):
    """
    清理文件名，移除非法字符并限制长度
    
    Args:
        filename: 原始文件名
        max_length: 最大长度，默认使用AppConfig.MAX_FILENAME_LENGTH
    
    Returns:
        清理后的文件名，如果清理后为空则返回None
    """
    if max_length is None:
        max_length = AppConfig.MAX_FILENAME_LENGTH
    
    # 移除非法字符
    safe_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    
    # 如果清理后为空，返回None
    if not safe_name or not safe_name.strip():
        return None
    
    # 限制长度
    if len(safe_name) > max_length:
        safe_name = safe_name[:max_length-3] + "..."
    
    return safe_name


def split_text_by_punctuation(text, max_chars_per_line=None):
    """
    按照标点符号智能分割文本，用于生成LRC歌词
    
    Args:
        text: 要分割的文本
        max_chars_per_line: 每行最大字符数，默认使用AppConfig.DEFAULT_CHARS_PER_LINE
    
    Returns:
        分割后的文本行列表
    """
    if max_chars_per_line is None:
        max_chars_per_line = AppConfig.DEFAULT_CHARS_PER_LINE
    
    # 定义句子结束标点（包括中英文）
    sentence_end_marks = r'[。！？.!?;；]'
    # 定义次要停顿标点
    minor_pause_marks = r'[，,、：:]'
    
    lines = []
    current_line = ""
    
    # 先按句子结束标点分割
    sentences = re.split(f'({sentence_end_marks})', text)
    
    for i in range(0, len(sentences), 2):
        if i >= len(sentences):
            break
        
        sentence = sentences[i]
        # 获取标点符号（如果存在）
        punctuation = sentences[i + 1] if i + 1 < len(sentences) else ""
        full_sentence = sentence + punctuation
        
        # 如果句子为空，跳过
        if not full_sentence.strip():
            continue
        
        # 如果当前行加上这个句子不超过最大长度，就追加
        if len(current_line) + len(full_sentence) <= max_chars_per_line:
            current_line += full_sentence
        else:
            # 如果当前行已有内容，先保存
            if current_line.strip():
                lines.append(current_line.strip())
            
            # 如果句子本身就超过最大长度，需要进一步分割
            if len(full_sentence) > max_chars_per_line:
                # 尝试按次要标点分割
                sub_parts = re.split(f'({minor_pause_marks})', full_sentence)
                temp_line = ""
                
                for j in range(0, len(sub_parts), 2):
                    if j >= len(sub_parts):
                        break
                    
                    part = sub_parts[j]
                    sub_punct = sub_parts[j + 1] if j + 1 < len(sub_parts) else ""
                    full_part = part + sub_punct
                    
                    if not full_part.strip():
                        continue
                    
                    if len(temp_line) + len(full_part) <= max_chars_per_line:
                        temp_line += full_part
                    else:
                        if temp_line.strip():
                            lines.append(temp_line.strip())
                        
                        # 如果单个部分仍然太长，强制分割
                        if len(full_part) > max_chars_per_line:
                            for k in range(0, len(full_part), max_chars_per_line):
                                lines.append(full_part[k:k+max_chars_per_line])
                            temp_line = ""
                        else:
                            temp_line = full_part
                
                if temp_line.strip():
                    lines.append(temp_line.strip())
                current_line = ""
            else:
                current_line = full_sentence
    
    # 保存最后一行
    if current_line.strip():
        lines.append(current_line.strip())
    
    # 如果没有分割出任何行，按原来的方式分割
    if not lines and text.strip():
        for i in range(0, len(text), max_chars_per_line):
            line = text[i:i+max_chars_per_line]
            if line.strip():
                lines.append(line)
    
    return lines


def generate_simple_lrc(text, chars_per_line):
    """
    生成简单的LRC文件（使用估算时间戳）
    
    Args:
        text: 要转换的文本
        chars_per_line: 每行字符数（用于估算时间）
    
    Returns:
        LRC格式的字符串
    """
    # 使用智能分割
    lines = split_text_by_punctuation(text, chars_per_line)
    
    lrc_lines = []
    cumulative_chars = 0
    
    for line in lines:
        # 根据累计字符数估算时间
        estimated_seconds = cumulative_chars * AppConfig.CHARS_PER_SECOND
        minutes = int(estimated_seconds // 60)
        seconds = int(estimated_seconds % 60)
        milliseconds = int((estimated_seconds % 1) * 100)
        lrc_time = f"[{minutes:02d}:{seconds:02d}.{milliseconds:02d}]"
        lrc_lines.append(f"{lrc_time}{line}")
        
        # 累加字符数
        cumulative_chars += len(line)
    
    return '\n'.join(lrc_lines)


async def process_text_to_mp3(text, output_audio, output_lrc, voice, chars_per_line=DEFAULT_CHARS_PER_LINE,
                              max_retries=MAX_RETRIES):
    """根据文本内容生成MP3和LRC文件"""
    for attempt in range(max_retries):
        try:
            if not text or not text.strip():
                print(f"警告：文本为空，无法生成音频。", file=sys.stderr)
                return False

            communicate = edge_tts.Communicate(text, voice)
            submaker = edge_tts.SubMaker()
            os.makedirs(os.path.dirname(output_audio), exist_ok=True)

            with open(output_audio, "wb") as audio_file:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_file.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        submaker.feed(chunk)

            srt_content = submaker.get_srt()
            
            # 检查SRT内容是否为空
            if not srt_content or not srt_content.strip():
                print(f"警告：edge-tts未返回字幕数据，使用降级方案生成简单LRC", file=sys.stderr)
                # 降级方案：生成简单的LRC文件（智能断句）
                lrc_content = generate_simple_lrc(text, chars_per_line)
            else:
                lrc_content = convert_srt_to_lrc(srt_content, chars_per_line)
                
                # 再次检查转换后的LRC是否为空
                if not lrc_content or not lrc_content.strip():
                    print(f"警告：SRT转LRC失败，使用降级方案", file=sys.stderr)
                    # 使用智能断句的降级方案
                    lrc_content = generate_simple_lrc(text, chars_per_line)

            with open(output_lrc, "w", encoding="utf-8") as lrc_file:
                lrc_file.write(lrc_content)

            return True
        except aiohttp.ClientError as e:
            if attempt < max_retries - 1:
                print(f"网络连接错误 (第 {attempt + 1}/{max_retries} 次尝试)：{e}", file=sys.stderr)
                print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"网络连接错误，已达到最大重试次数 ({max_retries})：{e}", file=sys.stderr)
                traceback.print_exc()
        except IOError as e:
            if attempt < max_retries - 1:
                print(f"文件IO错误 (第 {attempt + 1}/{max_retries} 次尝试)：{e}", file=sys.stderr)
                print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"文件IO错误，已达到最大重试次数 ({max_retries})：{e}", file=sys.stderr)
                traceback.print_exc()
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"生成音频过程中发生错误 (第 {attempt + 1}/{max_retries} 次尝试)：{e}", file=sys.stderr)
                print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"生成音频过程中发生错误，已达到最大重试次数 ({max_retries})：{e}", file=sys.stderr)
                traceback.print_exc()
    return False


def update_mp3_tag(mp3_path, lrc_path, cover_data, title, artist, album, lyric_lang=LYRIC_LANG):
    """为MP3添加ID3标签"""
    try:
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        # 添加封面图片
        if cover_data:
            # 根据文件头检测图片类型
            mime_type = "image/jpeg"  # 默认
            if cover_data.startswith(b'\x89PNG'):
                mime_type = "image/png"
            elif cover_data.startswith(b'GIF8'):
                mime_type = "image/gif"

            audio.tags['APIC'] = APIC(
                encoding=3,
                mime=mime_type,
                type=3,  # 3 表示封面图片
                desc='Cover',
                data=cover_data
            )

        # 添加标题、艺术家和专辑
        audio.tags['TIT2'] = TIT2(encoding=3, text=title)
        audio.tags['TPE1'] = TPE1(encoding=3, text=artist)
        audio.tags['TALB'] = TALB(encoding=3, text=album)

        # 添加歌词（如果存在）
        if os.path.exists(lrc_path):
            with open(lrc_path, "r", encoding="utf-8") as f:
                lrc_content = f.read()
            audio.tags['USLT::' + lyric_lang] = USLT(
                encoding=3,
                lang=lyric_lang,
                desc='Lyrics',
                text=lrc_content
            )

        audio.save()
        return True
    except FileNotFoundError as e:
        print(f"添加MP3标签时发生错误: 文件不存在 - {e}", file=sys.stderr)
        return False
    except PermissionError as e:
        print(f"添加MP3标签时发生错误: 权限不足 - {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"添加MP3标签时发生错误: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


def check_ffmpeg_installed():
    """检查FFmpeg是否已安装
    
    Returns:
        tuple: (is_installed: bool, ffmpeg_path: str)
    """
    # 首先检查系统PATH中的ffmpeg
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        result = subprocess.run(['ffmpeg', '-version'], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              timeout=5,
                              creationflags=creation_flags)
        if result.returncode == 0:
            return True, 'ffmpeg'
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # 检查本地安装目录
    if sys.platform == 'win32':
        local_ffmpeg = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe')
        if os.path.exists(local_ffmpeg):
            try:
                result = subprocess.run([local_ffmpeg, '-version'],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      timeout=5,
                                      creationflags=creation_flags)
                if result.returncode == 0:
                    return True, local_ffmpeg
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                pass
    
    return False, None


def install_ffmpeg_from_zip(zip_file_path, progress_callback=None):
    """从本地ZIP文件安装FFmpeg
    
    Args:
        zip_file_path: 本地ZIP文件路径
        progress_callback: 进度回调函数 callback(current, total, status_msg)
    
    Returns:
        tuple: (success: bool, ffmpeg_path: str, error_msg: str)
    """
    if sys.platform != 'win32':
        return False, None, "自动安装仅支持Windows系统，请手动安装FFmpeg"
    
    # 为此任务创建独立的临时目录
    task_temp_dir = tempfile.mkdtemp(prefix='epubReader_ffmpeg_')
    
    try:
        # 验证文件存在
        if not os.path.exists(zip_file_path):
            return False, None, "文件不存在"
        
        # 验证文件大小
        file_size = os.path.getsize(zip_file_path)
        if file_size < 1024 * 1024:  # 小于1MB
            return False, None, "文件太小，可能不是有效的FFmpeg压缩包"
        
        if progress_callback:
            progress_callback(0, 100, "正在验证ZIP文件...")
        
        # 验证是否为有效的ZIP文件
        try:
            with zipfile_lib.ZipFile(zip_file_path, 'r') as test_zip:
                namelist = test_zip.namelist()
                if not namelist:
                    return False, None, "ZIP文件为空"
        except zipfile_lib.BadZipFile:
            return False, None, "无效的ZIP文件"
        
        # 创建安装目录
        os.makedirs(FFMPEG_DIR, exist_ok=True)
        
        if progress_callback:
            progress_callback(30, 100, "正在解压FFmpeg...")
        
        # 解压文件
        with zipfile_lib.ZipFile(zip_file_path, 'r') as zip_ref:
            # 获取压缩包内的根目录名
            namelist = zip_ref.namelist()
            root_dir = namelist[0].split('/')[0] if '/' in namelist[0] else namelist[0].split('\\')[0]
            
            # 解压到独立的临时目录
            temp_extract = os.path.join(task_temp_dir, 'ffmpeg_extract')
            if os.path.exists(temp_extract):
                shutil.rmtree(temp_extract)
            
            zip_ref.extractall(temp_extract)
            
            if progress_callback:
                progress_callback(60, 100, "正在安装FFmpeg...")
            
            # 查找bin目录
            source_bin = os.path.join(temp_extract, root_dir, 'bin')
            if not os.path.exists(source_bin):
                # 尝试直接查找bin
                source_bin = os.path.join(temp_extract, 'bin')
                if not os.path.exists(source_bin):
                    shutil.rmtree(temp_extract)
                    return False, None, "ZIP文件中未找到bin目录，请确保是FFmpeg完整包"
            
            target_bin = os.path.join(FFMPEG_DIR, 'bin')
            
            # 删除旧的bin目录
            if os.path.exists(target_bin):
                shutil.rmtree(target_bin)
            
            # 移动bin目录到目标位置
            shutil.move(source_bin, target_bin)
        
        if progress_callback:
            progress_callback(90, 100, "正在验证安装...")
        
        ffmpeg_exe = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe')
        
        # 验证安装
        if not os.path.exists(ffmpeg_exe):
            return False, None, "FFmpeg.exe未找到，安装失败"
        
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            result = subprocess.run([ffmpeg_exe, '-version'],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=5,
                                  creationflags=creation_flags)
            if result.returncode == 0:
                if progress_callback:
                    progress_callback(100, 100, "安装完成")
                return True, ffmpeg_exe, None
            else:
                return False, None, "FFmpeg安装后验证失败"
        except Exception as e:
            return False, None, f"FFmpeg验证失败: {str(e)}"
    
    except Exception as e:
        return False, None, f"安装失败: {str(e)}"
    finally:
        # 清理任务临时目录
        try:
            if os.path.exists(task_temp_dir):
                shutil.rmtree(task_temp_dir)
        except Exception as e:
            print(f"清理临时目录失败: {e}")


def download_ffmpeg(progress_callback=None, cancel_check=None):
    """下载并安装FFmpeg (Windows)
    
    Args:
        progress_callback: 进度回调函数 callback(current, total, status_msg)
        cancel_check: 取消检查函数，返回True表示取消
    
    Returns:
        tuple: (success: bool, ffmpeg_path: str, error_msg: str)
    """
    if sys.platform != 'win32':
        return False, None, "自动安装仅支持Windows系统，请手动安装FFmpeg"
    
    # 为此任务创建独立的临时目录
    task_temp_dir = tempfile.mkdtemp(prefix='epubReader_ffmpeg_dl_')
    zip_path = None  # 在try外部定义，以便finally块可以访问
    
    try:
        # 创建安装目录
        os.makedirs(FFMPEG_DIR, exist_ok=True)
        
        # 下载文件
        zip_path = os.path.join(task_temp_dir, 'ffmpeg.zip')
        
        # 尝试多个下载源
        download_success = False
        last_error = None
        
        for url_index, download_url in enumerate(FFMPEG_DOWNLOAD_URLS):
            if cancel_check and cancel_check():
                return False, None, "用户取消安装"
            
            mirror_name = "官方镜像" if url_index == 0 else f"镜像{url_index + 1}"
            
            if progress_callback:
                progress_callback(0, 100, f"正在尝试连接 {mirror_name}...")
            
            try:
                # 使用requests库下载（更可靠）
                import requests
                
                # 设置请求头
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                
                # 开始下载
                response = requests.get(download_url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT)
                response.raise_for_status()
                
                total_size = int(response.headers.get('content-length', 0))
                downloaded_size = 0
                
                if progress_callback:
                    progress_callback(0, total_size or 100, f"开始下载 ({mirror_name})...")
                
                # 写入文件
                with open(zip_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if cancel_check and cancel_check():
                            return False, None, "用户取消安装"
                        
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            
                            if progress_callback and total_size > 0:
                                percent = int((downloaded_size / total_size) * 100)
                                mb_downloaded = downloaded_size / (1024 * 1024)
                                mb_total = total_size / (1024 * 1024)
                                progress_callback(
                                    downloaded_size, 
                                    total_size, 
                                    f"下载中: {percent}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)"
                                )
                
                download_success = True
                break  # 下载成功，跳出循环
                
            except requests.exceptions.Timeout:
                last_error = f"{mirror_name} 连接超时"
                if progress_callback:
                    progress_callback(0, 100, f"{last_error}，尝试下一个源...")
                time.sleep(1)
                continue
                
            except requests.exceptions.RequestException as e:
                last_error = f"{mirror_name} 下载失败: {str(e)}"
                if progress_callback:
                    progress_callback(0, 100, f"{last_error}，尝试下一个源...")
                time.sleep(1)
                continue
            
            except ImportError:
                # 如果没有requests库，回退到urllib
                if progress_callback:
                    progress_callback(0, 100, f"使用备用下载方式 ({mirror_name})...")
                
                try:
                    def download_progress(block_count, block_size, total_size):
                        if cancel_check and cancel_check():
                            raise Exception("用户取消")
                        
                        if progress_callback and total_size > 0:
                            downloaded = block_count * block_size
                            percent = min(int((downloaded / total_size) * 100), 100)
                            mb_downloaded = downloaded / (1024 * 1024)
                            mb_total = total_size / (1024 * 1024)
                            progress_callback(
                                downloaded, 
                                total_size, 
                                f"下载中: {percent}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)"
                            )
                    
                    # 设置超时
                    import socket
                    socket.setdefaulttimeout(DOWNLOAD_TIMEOUT)
                    
                    urllib.request.urlretrieve(download_url, zip_path, download_progress)
                    download_success = True
                    break
                    
                except Exception as e:
                    last_error = f"{mirror_name} 下载失败: {str(e)}"
                    if progress_callback:
                        progress_callback(0, 100, f"{last_error}，尝试下一个源...")
                    continue
        
        if not download_success:
            return False, None, f"所有下载源均失败。最后错误: {last_error}"
        
        if cancel_check and cancel_check():
            return False, None, "用户取消安装"
        
        if progress_callback:
            progress_callback(100, 100, "下载完成，正在解压...")
        
        # 验证下载的文件
        if not os.path.exists(zip_path) or os.path.getsize(zip_path) < 1024 * 1024:  # 小于1MB可能有问题
            return False, None, "下载的文件不完整或损坏"
        
        # 解压文件
        with zipfile_lib.ZipFile(zip_path, 'r') as zip_ref:
            # 获取压缩包内的根目录名
            namelist = zip_ref.namelist()
            root_dir = namelist[0].split('/')[0] if '/' in namelist[0] else namelist[0].split('\\')[0]
            
            # 解压到临时目录
            temp_extract = os.path.join(task_temp_dir, 'ffmpeg_extract')
            zip_ref.extractall(temp_extract)
            
            # 移动bin目录到目标位置
            source_bin = os.path.join(temp_extract, root_dir, 'bin')
            target_bin = os.path.join(FFMPEG_DIR, 'bin')
            
            if os.path.exists(target_bin):
                shutil.rmtree(target_bin)
            
            shutil.move(source_bin, target_bin)
            
            # 清理临时文件
            shutil.rmtree(temp_extract)
        
        os.remove(zip_path)
        
        if progress_callback:
            progress_callback(100, 100, "安装完成")
        
        ffmpeg_exe = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe')
        
        # 验证安装
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            result = subprocess.run([ffmpeg_exe, '-version'],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=5,
                                  creationflags=creation_flags)
            if result.returncode == 0:
                return True, ffmpeg_exe, None
            else:
                return False, None, "FFmpeg安装后验证失败"
        except Exception as e:
            return False, None, f"FFmpeg验证失败: {str(e)}"
    
    except urllib.error.URLError as e:
        return False, None, f"下载失败: 无法连接到下载服务器 ({str(e)})"
    except Exception as e:
        return False, None, f"安装失败: {str(e)}"
    finally:
        # 清理任务临时目录
        try:
            if os.path.exists(task_temp_dir):
                shutil.rmtree(task_temp_dir)
        except Exception as e:
            print(f"清理临时目录失败: {e}")


def merge_mp3_to_m4b(mp3_files, output_m4b, metadata, cover_data=None, ffmpeg_path=None, chapter_titles=None):
    """将多个MP3文件合并为M4B有声书格式，并嵌入章节信息
    
    Args:
        mp3_files: MP3文件路径列表，按章节顺序排列
        output_m4b: 输出的M4B文件路径
        metadata: 字典，包含 title, author, album 等信息
        cover_data: 封面图片数据（二进制）
        ffmpeg_path: FFmpeg可执行文件路径
        chapter_titles: 章节标题列表，与mp3_files一一对应
    
    Returns:
        tuple: (success: bool, error_msg: str) 成功返回(True, None)，失败返回(False, 错误信息)
    """
    # 为此任务创建独立的临时目录
    task_temp_dir = tempfile.mkdtemp(prefix='epubReader_m4b_')
    
    try:
        # Windows下隐藏控制台窗口的参数
        creation_flags = 0
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW
        
        # 使用传入的ffmpeg路径或默认路径
        if ffmpeg_path is None:
            ffmpeg_path = FFMPEG_PATH
        
        # 检查FFmpeg是否可用
        try:
            subprocess.run([ffmpeg_path, "-version"], 
                         stdout=subprocess.PIPE, 
                         stderr=subprocess.PIPE, 
                         check=True,
                         creationflags=creation_flags)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = f"未找到FFmpeg或无法执行: {str(e)}"
            print(f"错误: {error_msg}", file=sys.stderr)
            return False, error_msg
        
        if not mp3_files:
            error_msg = "没有MP3文件可合并"
            print(f"错误: {error_msg}", file=sys.stderr)
            return False, error_msg
        
        # 获取每个章节的时长，用于生成章节标记
        print("正在获取章节时长...") 
        chapter_durations = []
        for mp3_file in mp3_files:
            duration = None
            
            # 方案1: 使用FFmpeg获取时长
            try:
                result = subprocess.run(
                    [ffmpeg_path, '-i', mp3_file],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    creationflags=creation_flags
                )
                output = result.stderr.decode('utf-8', errors='ignore')
                
                # 从输出中提取时长
                import re
                duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', output)
                if duration_match:
                    hours = int(duration_match.group(1))
                    minutes = int(duration_match.group(2))
                    seconds = float(duration_match.group(3))
                    duration = hours * 3600 + minutes * 60 + seconds
                    print(f"FFmpeg获取 {os.path.basename(mp3_file)} 时长: {duration:.2f}秒")
            except Exception as e:
                print(f"FFmpeg获取 {os.path.basename(mp3_file)} 时长失败: {e}")
            
            # 方案2: 如果FFmpeg失败，使用mutagen作为备用方案
            if duration is None or duration <= 0:
                try:
                    audio = MP3(mp3_file)
                    if audio.info and audio.info.length > 0:
                        duration = audio.info.length
                        print(f"Mutagen获取 {os.path.basename(mp3_file)} 时长: {duration:.2f}秒")
                except Exception as e:
                    print(f"Mutagen获取 {os.path.basename(mp3_file)} 时长失败: {e}")
            
            # 最终处理: 如果两种方法都失败，使用最小时长1秒
            if duration is None or duration <= 0:
                duration = 1.0
                print(f"警告: 无法获取 {os.path.basename(mp3_file)} 的时长，使用最小时长1秒")
            
            chapter_durations.append(duration)
        
        # 创建临时文件列表
        list_file = os.path.join(task_temp_dir, "m4b_filelist.txt")
        with open(list_file, 'w', encoding='utf-8') as f:
            for mp3_file in mp3_files:
                # FFmpeg的concat demuxer需要这种格式
                escaped_path = mp3_file.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
        
        # 生成章节元数据文件 (FFMETADATA格式)
        metadata_file = os.path.join(task_temp_dir, "m4b_chapters.txt")
        with open(metadata_file, 'w', encoding='utf-8') as f:
            f.write(";FFMETADATA1\n")
            
            # 添加全局元数据
            if metadata.get('title'):
                f.write(f"title={metadata['title']}\n")
            if metadata.get('author'):
                f.write(f"artist={metadata['author']}\n")
            if metadata.get('album'):
                f.write(f"album={metadata['album']}\n")
            
            # 添加章节标记
            current_time_ms = 0
            for i, (duration, mp3_file) in enumerate(zip(chapter_durations, mp3_files)):
                # 获取章节标题
                if chapter_titles and i < len(chapter_titles):
                    chapter_title = chapter_titles[i]
                else:
                    chapter_title = f"第{i+1}章"
                
                # 转换为毫秒
                duration_ms = int(duration * 1000)
                
                f.write("\n[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={current_time_ms}\n")
                f.write(f"END={current_time_ms + duration_ms}\n")
                f.write(f"title={chapter_title}\n")
                
                current_time_ms += duration_ms
        
        # 创建临时M4A文件（不带元数据）
        temp_m4a = os.path.join(task_temp_dir, "temp_audiobook.m4a")
        
        # 第一步：合并所有MP3为M4A，并嵌入章节信息
        print("正在合并音频文件并添加章节信息...")
        concat_cmd = [
            ffmpeg_path,
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-i", metadata_file,  # 添加章节元数据
            "-map_metadata", "1",  # 使用metadata_file的元数据
            "-c:a", "aac",
            "-b:a", "64k",
            "-ar", "44100",
            "-y",
            temp_m4a
        ]
        
        result = subprocess.run(concat_cmd, 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              creationflags=creation_flags)
        
        if result.returncode != 0:
            error_output = result.stderr.decode('utf-8', errors='ignore')
            error_msg = f"FFmpeg合并音频失败\n错误代码: {result.returncode}\n详细信息: {error_output[-500:]}"
            print(f"FFmpeg合并失败: {error_output}", file=sys.stderr)
            return False, error_msg
        
        # 第二步：添加封面，转换为M4B
        print("正在添加封面...")
        
        metadata_cmd = [
            ffmpeg_path,
            "-i", temp_m4a,
        ]
        
        # 如果有封面，添加封面输入
        cover_file = None
        if cover_data:
            cover_file = os.path.join(task_temp_dir, "m4b_cover.jpg")
            with open(cover_file, 'wb') as f:
                f.write(cover_data)
            metadata_cmd.extend(["-i", cover_file])
        
        # 添加编码和映射参数
        metadata_cmd.extend([
            "-c:a", "copy",
        ])
        
        if cover_data:
            metadata_cmd.extend([
                "-c:v", "copy",
                "-map", "0:a",
                "-map", "0:t?",  # 映射章节信息
                "-map", "1:v",
                "-disposition:v:0", "attached_pic",  # 设置封面为附加图片
            ])
        else:
            metadata_cmd.extend([
                "-map", "0:a",
                "-map", "0:t?",  # 映射章节信息
            ])
        
        # 添加元数据（保持原有章节信息）
        metadata_cmd.extend([
            "-metadata", "media_type=2",  # 2表示有声书
            "-f", "mp4",
            "-y",
            output_m4b
        ])
        
        result = subprocess.run(metadata_cmd,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              creationflags=creation_flags)
        
        if result.returncode != 0:
            error_output = result.stderr.decode('utf-8', errors='ignore')
            error_msg = f"FFmpeg添加封面/元数据失败\n错误代码: {result.returncode}\n详细信息: {error_output[-500:]}"
            print(f"FFmpeg添加元数据失败: {error_output}", file=sys.stderr)
            return False, error_msg
        
        print(f"M4B文件生成成功: {output_m4b}")
        return True, None
        
    except Exception as e:
        error_msg = f"生成M4B文件时发生异常: {str(e)}\n{traceback.format_exc()[-500:]}"
        print(f"生成M4B文件时发生错误: {e}", file=sys.stderr)
        traceback.print_exc()
        return False, error_msg
    finally:
        # 清理任务临时目录
        try:
            if os.path.exists(task_temp_dir):
                shutil.rmtree(task_temp_dir)
        except Exception as e:
            print(f"清理临时目录失败: {e}")


# ===== 主GUI应用 =====
class EpubToMp3App:
    def __init__(self, root):
        self.root = root
        self.root.title("epubReader V0.01")
        self.root.geometry("1024x700")

        # 存储解析后的数据
        self.epub_data = None
        self.current_chapter_index = -1  # -1 表示没有章节被选中
        self.processing = False  # 标记是否有转换任务正在进行
        self.failed_chapters = []  # 记录失败的章节 [{"index": int, "title": str, "reason": str}]
        self.estimated_time_var = None  # 将在create_widgets中初始化
        self.estimated_time_var_epub_tab = None  # 将在create_widgets中初始化
        self.ffmpeg_path = None  # FFmpeg路径
        self.ffmpeg_installed = False  # FFmpeg安装状态

        # 创建临时目录
        os.makedirs(TEMP_DIR, exist_ok=True)

        # 创建通信队列
        self.message_queue = queue.Queue()

        # 创建基本字体
        self.base_font = font.Font(family='Microsoft YaHei', size=11)
        self.small_font = font.Font(family='Microsoft YaHei', size=9)

        # 创建主界面
        self.create_widgets()

        # 检查FFmpeg
        self.root.after(500, self.check_ffmpeg_on_startup)

        # 启动消息处理
        self.process_messages()

    def center_window(self):
        """将窗口居中显示"""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def create_widgets(self):
        """创建GUI组件"""
        # 配置root窗口的grid布局
        self.root.rowconfigure(0, weight=1)  # 主内容区域可扩展
        self.root.rowconfigure(1, weight=0)  # 状态栏固定高度不扩展
        self.root.columnconfigure(0, weight=1)  # 列宽可扩展

        # 创建主标签页容器
        self.notebook = ttk.Notebook(self.root)
        # 使用grid替代pack，放在第0行
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=5)

        # 创建EPUB阅读与编辑页
        self.epub_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.epub_frame, text="浏览与编辑")

        # 创建设置与自定义
        self.mp3_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.mp3_frame, text="设置与自定义")

        # 底部状态栏
        self.create_status_bar()

        # 初始化EPUB阅读界面
        self.create_epub_reader_ui()

        # 初始化有声书转换界面
        self.create_mp3_converter_ui()

    def create_status_bar(self):
        """创建底部状态栏"""
        status_frame = ttk.Frame(self.root)
        # 使用grid替代pack，放在第1行
        status_frame.grid(row=1, column=0, sticky="ew")

        # 配置status_frame的列权重，使状态标签可以扩展
        status_frame.columnconfigure(0, weight=1)  # 状态标签可扩展
        status_frame.columnconfigure(1, weight=0)  # 进度条固定宽度

        self.status_label = ttk.Label(
            status_frame,
            text="状态: 等待操作...",
            relief=tk.SUNKEN,
            anchor=tk.W,
            font=self.small_font
        )
        # 使用grid替代pack，放在第0列
        self.status_label.grid(row=0, column=0, sticky="ew", padx=5, pady=2)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            status_frame,
            orient=tk.HORIZONTAL,
            length=200,
            mode='determinate',
            variable=self.progress_var
        )
        # 使用grid替代pack，放在第1列
        self.progress_bar.grid(row=0, column=1, padx=5, pady=2)
        self.progress_bar["maximum"] = 100
        self.progress_bar["value"] = 0

    def create_epub_reader_ui(self):
        """创建EPUB阅读与编辑UI"""
        # 顶部工具栏
        top_frame = ttk.Frame(self.epub_frame)
        top_frame.pack(fill=tk.X, padx=5, pady=5)

        # 选择电子书按钮
        self.select_button = ttk.Button(
            top_frame,
            text="选择电子书",
            command=self.select_epub_file
        )
        self.select_button.pack(side=tk.LEFT, padx=(0, 5))

        # 文件路径输入框
        self.filepath_var = tk.StringVar()
        self.filepath_entry = ttk.Entry(
            top_frame,
            textvariable=self.filepath_var,
            state='readonly',
            width=40
        )
        self.filepath_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        # # 导出所有章节为TXT（注释掉）
        # self.export_all_button = ttk.Button(
        #     top_frame,
        #     text="导出所有章节",
        #     command=self.export_all_chapters,
        #     state="disabled"
        # )
        # self.export_all_button.pack(side=tk.LEFT, padx=(0, 5))

        # 新增"显示预计耗时"标签
        self.estimated_time_var_epub_tab = tk.StringVar(value="预计耗时：00:00:00")
        self.estimated_time_label_epub_tab = ttk.Label(
            top_frame,
            textvariable=self.estimated_time_var_epub_tab,
            font=self.small_font
        )
        self.estimated_time_label_epub_tab.pack(side=tk.LEFT, padx=(0, 5))

        # 新增"生成电子书"按钮
        self.generate_ebook_button_epub_tab = ttk.Button(
            top_frame,
            text="生成电子书",
            command=self.start_generate_ebook,
            state="disabled"
        )
        self.generate_ebook_button_epub_tab.pack(side=tk.LEFT, padx=(0, 5))

        # 批量导出TXT按钮
        self.export_all_button = ttk.Button(
            top_frame,
            text="批量导出TXT",
            command=self.export_all_chapters,
            state="disabled"
        )
        self.export_all_button.pack(side=tk.LEFT, padx=(0, 5))

        # 主工作区 - 使用PanedWindow
        main_pane = ttk.PanedWindow(self.epub_frame, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧信息区域
        left_frame = ttk.Frame(main_pane, width=220)
        left_frame.pack_propagate(False)  # 防止frame根据内容自动缩放
        main_pane.add(left_frame, weight=20)

        # 封面图片区域
        self.cover_label = ttk.Label(left_frame, text="图书封面", anchor=tk.CENTER, background="#f0f0f0")
        self.cover_label.pack(fill=tk.X, padx=5, pady=5)
        self.cover_label.configure(width=220)

        # 书籍信息
        info_frame = ttk.LabelFrame(left_frame, text="书籍信息")
        info_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(info_frame, text="书名:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.title_label = ttk.Label(info_frame, text="")
        self.title_label.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(info_frame, text="作者:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.author_label = ttk.Label(info_frame, text="")
        self.author_label.grid(row=1, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(info_frame, text="出版社:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        self.publisher_label = ttk.Label(info_frame, text="")
        self.publisher_label.grid(row=2, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(info_frame, text="出版时间:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=2)
        self.publish_date_label = ttk.Label(info_frame, text="")
        self.publish_date_label.grid(row=3, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(info_frame, text="全书字数:").grid(row=4, column=0, sticky=tk.W, padx=5, pady=2)
        self.total_words_label = ttk.Label(info_frame, text="")
        self.total_words_label.grid(row=4, column=1, sticky=tk.W, padx=5, pady=2)

        # 中间章节列表区域
        middle_frame = ttk.Frame(main_pane, width=300)
        main_pane.add(middle_frame, weight=30)

        ttk.Label(middle_frame, text="章节列表").pack(pady=(0, 5))

        # 章节列表框架
        list_frame = ttk.Frame(middle_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 章节列表
        self.chapter_listbox = tk.Listbox(
            list_frame,
            exportselection=False,
            activestyle='none',
            font=self.base_font
        )
        chapter_scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.chapter_listbox.yview)
        self.chapter_listbox.configure(yscrollcommand=chapter_scrollbar.set)

        self.chapter_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chapter_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 绑定章节列表事件
        self.chapter_listbox.bind('<ButtonRelease-1>', self.on_chapter_select)
        self.chapter_listbox.bind('<Button-3>', self.on_chapter_right_click)

        # 右侧章节内容区域
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=50)

        # 章节操作工具栏
        chapter_tools_frame = ttk.Frame(right_frame)
        chapter_tools_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(chapter_tools_frame, text="章节内容").pack(pady=(0, 5))

        self.chapter_save_button = ttk.Button(
            chapter_tools_frame,
            text="保存修改",
            command=self.save_chapter_content,
            state="disabled"
        )
        self.chapter_save_button.pack(side=tk.LEFT, padx=(5, 0))

        self.convert_chapter_button = ttk.Button(
            chapter_tools_frame,
            text="转有声书",
            command=self.convert_current_chapter,
            state="disabled"
        )
        self.convert_chapter_button.pack(side=tk.LEFT, padx=(5, 0))

        # 章节内容文本框
        self.chapter_text = scrolledtext.ScrolledText(
            right_frame,
            wrap=tk.WORD,
            font=self.base_font,
            padx=5,
            pady=5
        )
        self.chapter_text.pack(fill=tk.BOTH, expand=True)

    def create_mp3_converter_ui(self):
        """创建有声书转换UI"""
        # 使用Grid布局管理器
        mp3_main_frame = ttk.Frame(self.mp3_frame, padding=10)
        mp3_main_frame.pack(fill=tk.BOTH, expand=True)

        # 输入输出目录设置
        dir_frame = ttk.LabelFrame(mp3_main_frame, text="目录设置", padding=5)
        dir_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(dir_frame, text="输出目录:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.output_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "有声书目录"))
        output_entry = ttk.Entry(dir_frame, textvariable=self.output_dir_var, width=40)
        output_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(dir_frame, text="浏览...", command=lambda: self.browse_directory(self.output_dir_var)).grid(row=1,
                                                                                                             column=2,
                                                                                                             padx=5,
                                                                                                             pady=5)

        # TTS配置设置
        tts_frame = ttk.LabelFrame(mp3_main_frame, text="TTS配置", padding=5)
        tts_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(tts_frame, text="语音:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.voice_var = tk.StringVar(value=DEFAULT_VOICE)
        voice_combo = ttk.Combobox(tts_frame, textvariable=self.voice_var, width=30)
        voice_combo['values'] = [
            "zh-CN-YunjianNeural",  # 男声
            "zh-CN-XiaoxiaoNeural",  # 女声
            "zh-CN-YunxiNeural",  # 男童声
            "zh-CN-XiaoyiNeural",  # 女声
            "zh-CN-YunyangNeural",  # 男声
            "zh-CN-liaoning-XiaobeiNeural",  # 女声，辽宁口音
            "en-US-BrianMultilingualNeural",  # 英文
            "en-US-BrianNeural",
            "en-US-AndrewMultilingualNeural",
            "en-US-AndrewNeural",
            "en-US-EmmaMultilingualNeural",
            "en-US-EmmaNeural",
        ]
        voice_combo.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        # ====== 新增试听按钮（用pygame播放）======
        def tts_preview():
            if self.processing:
                messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
                return
            text = "腹有诗书气自华，读书万卷始通神"
            voice = self.voice_var.get()
            self.update_status("正在生成试听音频...")
            self.append_log(f"试听语音: {voice}")
            # 为试听创建独立的临时文件
            preview_mp3 = tempfile.mktemp(suffix='.mp3', prefix='epubReader_preview_')
            try:
                import asyncio
                async def gen_preview():
                    communicate = edge_tts.Communicate(text, voice)
                    with open(preview_mp3, "wb") as f:
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":
                                f.write(chunk["data"])

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(gen_preview())
            except Exception as e:
                self.update_status(f"试听生成失败: {e}")
                messagebox.showerror("试听失败", f"试听音频生成失败: {e}")
                return

            try:
                import pygame
            except ImportError:
                messagebox.showerror("缺少依赖", "需要安装 pygame 库才能试听音频。\n请运行：pip install pygame")
                return

            if not os.path.exists(preview_mp3):
                messagebox.showerror("试听失败", "试听音频文件未生成")
                return

            def play_with_pygame():
                try:
                    pygame.init()
                    pygame.mixer.init()
                    pygame.mixer.music.load(preview_mp3)
                    pygame.mixer.music.play()
                    self.update_status("试听播放中...")
                    while pygame.mixer.music.get_busy():
                        pygame.time.wait(100)
                    pygame.mixer.music.stop()
                except Exception as e:
                    messagebox.showerror("试听失败", f"音频播放失败: {e}")
                finally:
                    pygame.quit()
                    self.update_status("试听结束")

            import threading
            threading.Thread(target=play_with_pygame, daemon=True).start()

        # ====== 试听按钮结束 ======
        preview_btn = ttk.Button(tts_frame, text="试听", command=tts_preview)
        preview_btn.grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)

        ttk.Label(tts_frame, text="歌词每行字数:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.chars_per_line_var = tk.IntVar(value=DEFAULT_CHARS_PER_LINE)
        spinbox = ttk.Spinbox(tts_frame, from_=5, to=50, textvariable=self.chars_per_line_var, width=5)
        spinbox.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)

        # MP3标签设置
        tag_frame = ttk.LabelFrame(mp3_main_frame, text="MP3标签", padding=5)
        tag_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(tag_frame, text="艺术家:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.artist_var = tk.StringVar(value=DEFAULT_ARTIST)
        self.artist_entry = ttk.Entry(tag_frame, textvariable=self.artist_var, width=30, state='disabled')
        self.artist_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(tag_frame, text="专辑:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
        self.album_var = tk.StringVar(value="有声书")
        self.album_entry = ttk.Entry(tag_frame, textvariable=self.album_var, width=30, state='disabled')
        self.album_entry.grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)

        ttk.Label(tag_frame, text="封面图片:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.cover_path_var = tk.StringVar()
        self.cover_entry = ttk.Entry(tag_frame, textvariable=self.cover_path_var, width=30, state='disabled')
        self.cover_entry.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        self.cover_button = ttk.Button(tag_frame, text="选择...", command=self.browse_cover_image, state='disabled')
        self.cover_button.grid(row=1, column=2, sticky=tk.W, padx=5, pady=5)

        # 输出格式选择
        format_frame = ttk.LabelFrame(mp3_main_frame, text="输出格式", padding=5)
        format_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(format_frame, text="选择格式:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.output_format_var = tk.StringVar(value="MP3")
        format_radio_frame = ttk.Frame(format_frame)
        format_radio_frame.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        
        ttk.Radiobutton(format_radio_frame, text="MP3 (分章节)", 
                       variable=self.output_format_var, value="MP3").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(format_radio_frame, text="M4B (合并为单个有声书)", 
                       variable=self.output_format_var, value="M4B").pack(side=tk.LEFT, padx=5)
        
        # 并发线程数设置
        ttk.Label(format_frame, text="并发线程数:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.concurrent_threads_var = tk.IntVar(value=3)
        thread_spinbox = ttk.Spinbox(format_frame, from_=1, to=5, textvariable=self.concurrent_threads_var, width=10)
        thread_spinbox.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        ttk.Label(format_frame, text="(1-5个章节同时处理，提升速度)", font=self.small_font).grid(row=1, column=2, sticky=tk.W, padx=5, pady=5)
        
        # FFmpeg状态显示和安装按钮
        ffmpeg_status_frame = ttk.Frame(format_frame)
        ffmpeg_status_frame.grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=5, pady=(5, 5))
        
        self.ffmpeg_status_label = ttk.Label(ffmpeg_status_frame, 
                                             text="FFmpeg状态: 检测中...",
                                             font=self.small_font)
        self.ffmpeg_status_label.pack(side=tk.LEFT, padx=(0, 10))
        
        self.ffmpeg_install_button = ttk.Button(ffmpeg_status_frame,
                                               text="安装FFmpeg",
                                               command=self.install_ffmpeg_dialog,
                                               state="disabled")
        self.ffmpeg_install_button.pack(side=tk.LEFT, padx=(0, 5))
        
        self.ffmpeg_manual_install_button = ttk.Button(ffmpeg_status_frame,
                                                       text="从本地ZIP安装",
                                                       command=self.install_ffmpeg_from_local,
                                                       state="disabled")
        self.ffmpeg_manual_install_button.pack(side=tk.LEFT)

        # 转换按钮和进度
        self.batch_button_frame = ttk.Frame(mp3_main_frame, padding=5)
        self.batch_button_frame.pack(fill=tk.X, pady=(0, 5))

        # 添加预计耗时标签
        self.estimated_time_var = tk.StringVar(value="预计耗时：00:00:00")
        self.estimated_time_label = ttk.Label(
            self.batch_button_frame,
            textvariable=self.estimated_time_var,
            font=self.small_font
        )
        self.estimated_time_label.pack(side=tk.LEFT, padx=(0, 10))

        self.generate_ebook_button = ttk.Button(
            self.batch_button_frame,
            text="生成电子书",
            command=self.start_generate_ebook,
            style="Accent.TButton"
        )
        self.generate_ebook_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # 合并MP3为M4B按钮
        self.merge_to_m4b_button = ttk.Button(
            self.batch_button_frame,
            text="合并MP3为M4B",
            command=self.merge_mp3_to_m4b_dialog,
            style="Accent.TButton"
        )
        self.merge_to_m4b_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # TXT批量转换按钮
        self.batch_txt_button = ttk.Button(
            self.batch_button_frame,
            text="TXT批量转换",
            command=self.batch_convert_txt_dialog,
            style="Accent.TButton"
        )
        self.batch_txt_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # 重新处理失败章节按钮（初始隐藏）
        self.retry_failed_button = ttk.Button(
            self.batch_button_frame,
            text="重新处理失败章节",
            command=self.retry_failed_chapters,
            style="Accent.TButton"
        )
        # 初始状态不显示

        # 转换进度和日志区域
        log_frame = ttk.LabelFrame(mp3_main_frame, text="运行日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=self.small_font,
            background="#f8f8f8"
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)  # 只读

    def browse_directory(self, var):
        """浏览并选择目录"""
        directory = filedialog.askdirectory(initialdir=var.get())
        if directory:
            var.set(directory)

    def browse_cover_image(self):
        """浏览并选择封面图片"""
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.gif"),
                ("所有文件", "*.*")
            ]
        )
        if file_path:
            self.cover_path_var.set(file_path)

    def select_epub_file(self):
        """选择EPUB文件并解析"""
        file_path = filedialog.askopenfilename(
            title="选择EPUB文件",
            filetypes=[
                ("EPUB Files", "*.epub"),
                ("All Files", "*.*")
            ]
        )

        if not file_path:
            return

        # 更新状态
        self.update_status(f"正在解析 {os.path.basename(file_path)}...")
        self.filepath_var.set(file_path)

        # 清空显示
        self.clear_display()

        # 在后台线程中解析EPUB
        threading.Thread(target=self._parse_epub_in_thread, args=(file_path,), daemon=True).start()

    def _parse_epub_in_thread(self, file_path):
        """在后台线程中解析EPUB文件"""
        try:
            self.epub_data = parse_epub_data(file_path)

            # 使用队列将结果传递回主线程
            if self.epub_data:
                self.message_queue.put(("display_epub", None))
                self.message_queue.put(("status", f"已加载 {os.path.basename(file_path)}"))
            else:
                self.message_queue.put(("status", f"解析失败: {os.path.basename(file_path)}"))
                self.message_queue.put(("error", "无法解析选定的EPUB文件。"))

        except Exception as e:
            self.message_queue.put(("status", f"解析出错: {str(e)}"))
            self.message_queue.put(("error", f"解析EPUB文件时发生错误: {str(e)}"))
            traceback.print_exc()

    def process_messages(self):
        """处理消息队列的消息，更新UI"""
        try:
            while not self.message_queue.empty():
                message_type, data = self.message_queue.get_nowait()

                if message_type == "status":
                    self.update_status(data)
                elif message_type == "error":
                    messagebox.showerror("错误", data)
                elif message_type == "display_epub":
                    self.display_epub_data()
                elif message_type == "progress":
                    self.progress_var.set(data)
                elif message_type == "log":
                    self.append_log(data)
                elif message_type == "batch_complete":
                    self.on_batch_complete()
                elif message_type == "failed_chapter":
                    # 记录失败的章节
                    self.failed_chapters.append(data)
                elif message_type == "show_retry_button":
                    # 显示重试按钮
                    self.retry_failed_button.pack(side=tk.LEFT, padx=(0, 10))
                elif message_type == "hide_retry_button":
                    # 隐藏重试按钮
                    self.retry_failed_button.pack_forget()
                elif message_type == "clear_failed_chapters":
                    # 清空失败章节列表
                    self.failed_chapters.clear()

        except queue.Empty:
            pass
        finally:
            # 继续检查队列
            self.root.after(100, self.process_messages)

    def update_status(self, message):
        """更新状态栏信息"""
        self.status_label.config(text=f"状态: {message}")

    def append_log(self, message):
        """向日志文本框添加消息"""
        self.log_text.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)  # 滚动到最后
        self.log_text.config(state=tk.DISABLED)

    def clear_display(self):
        """清空界面显示"""
        # 清空书籍信息
        self.title_label.config(text="")
        self.author_label.config(text="")
        self.publisher_label.config(text="")
        self.publish_date_label.config(text="")
        self.total_words_label.config(text="")

        # 重置预计耗时
        if hasattr(self, 'estimated_time_var') and self.estimated_time_var:
            self.estimated_time_var.set("预计耗时：00:00:00")
        if hasattr(self, 'estimated_time_var_epub_tab') and self.estimated_time_var_epub_tab:
            self.estimated_time_var_epub_tab.set("预计耗时：00:00:00")

        # 清空封面
        self.cover_label.config(image="", text="图书封面")
        self.cover_label.image = None  # 释放引用

        # 清空章节列表
        self.chapter_listbox.delete(0, tk.END)

        # 清空章节内容
        self.chapter_text.delete("1.0", tk.END)

        # 重置状态
        self.current_chapter_index = -1
        self.chapter_save_button.config(state="disabled")
        self.convert_chapter_button.config(state="disabled")
        self.generate_ebook_button_epub_tab.config(state="disabled")
        self.export_all_button.config(state="disabled")

        # 禁用MP3标签相关控件
        self.artist_entry.config(state='disabled')
        self.album_entry.config(state='disabled')
        self.cover_entry.config(state='disabled')
        self.cover_button.config(state='disabled')

    def display_epub_data(self):
        """在GUI中显示解析后的EPUB数据，并自动创建有声书目录"""
        if not self.epub_data:
            return

        # 显示书籍信息
        self.title_label.config(text=self.epub_data['title'])
        self.author_label.config(text=self.epub_data['author'])
        self.publisher_label.config(text=self.epub_data['publisher'])
        self.publish_date_label.config(text=self.epub_data.get('publish_date', ''))
        if 'total_words' in self.epub_data:
            self.total_words_label.config(text=str(self.epub_data['total_words']))
        else:
            self.total_words_label.config(text="")

        # 更新专辑和艺术家
        self.album_var.set(self.epub_data['title'])
        self.artist_var.set(self.epub_data['author'])

        # 启用MP3标签相关控件
        self.artist_entry.config(state='normal')
        self.album_entry.config(state='normal')
        self.cover_entry.config(state='normal')
        self.cover_button.config(state='normal')

        # 显示封面图片
        if self.epub_data['cover_data']:
            try:
                image_data = self.epub_data['cover_data']
                img = Image.open(io.BytesIO(image_data))

                # 计算合适的尺寸
                max_width = 180
                max_height = 250
                img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

                photo = ImageTk.PhotoImage(img)
                self.cover_label.config(image=photo, text="")
                self.cover_label.image = photo  # 保持引用

                # 保存封面文件到临时目录（使用独立文件名）
                cover_path = tempfile.mktemp(suffix='.jpg', prefix='epubReader_cover_')
                with open(cover_path, "wb") as f:
                    f.write(image_data)
                self.cover_path_var.set(cover_path)

            except Exception as e:
                print(f"显示封面图片时出错: {e}")
                self.cover_label.config(text="无法显示封面")
        else:
            self.cover_label.config(text="无封面图片")

        # 显示章节列表
        self.chapter_listbox.delete(0, tk.END)
        if self.epub_data.get('chapters'):
            for i, chapter in enumerate(self.epub_data['chapters']):
                title = chapter.get('title', f"章节 {i + 1}")
                self.chapter_listbox.insert(tk.END, f" {title}")

            # 启用导出按钮
            self.generate_ebook_button_epub_tab.config(state="normal")
            self.export_all_button.config(state="normal")

            # 默认选择第一个章节
            if len(self.epub_data['chapters']) > 0:
                self.chapter_listbox.select_set(0)
                self.on_chapter_select(None)

        # ---- 新增：自动创建有声书目录并更新输出目录 ----
        # 获取EPUB文件路径
        epub_path = self.filepath_var.get()
        if epub_path:
            epub_dir = os.path.dirname(epub_path)
            # 清理书名中的特殊字符
            book_title = self.epub_data['title'] if self.epub_data['title'] else "有声书"
            safe_book_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
            if not safe_book_title.strip():
                safe_book_title = "有声书"
            target_dir = os.path.join(epub_dir, f"有声书目录_{safe_book_title}")
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as e:
                print(f"创建有声书目录失败: {e}")
            self.output_dir_var.set(target_dir)

        # ---- 新增：计算并显示预计耗时 ----
        estimated_time = self.calculate_estimated_time()
        self.estimated_time_var.set(f"预计耗时：{estimated_time}")
        self.estimated_time_var_epub_tab.set(f"预计耗时：{estimated_time}")

    def on_chapter_select(self, event):
        """处理章节列表选择事件"""
        selected_indices = self.chapter_listbox.curselection()

        if not selected_indices:
            return

        index = selected_indices[0]

        if index != self.current_chapter_index and self.epub_data and 'chapters' in self.epub_data:
            if len(self.epub_data['chapters']) > index:
                self.current_chapter_index = index
                chapter = self.epub_data['chapters'][index]
                self.display_chapter_content(chapter.get('content', ''))
                self.chapter_save_button.config(state="normal")
                self.convert_chapter_button.config(state="normal")
                self.update_status(f"已选择: {self.chapter_listbox.get(index)}")

    def display_chapter_content(self, content):
        """显示章节内容"""
        self.chapter_text.delete("1.0", tk.END)
        self.chapter_text.insert(tk.END, content)
        self.chapter_text.see("1.0")  # 滚动到顶部

    def save_chapter_content(self):
        """保存章节内容修改"""
        if self.current_chapter_index < 0 or not self.epub_data or 'chapters' not in self.epub_data:
            return

        # 获取文本框中的内容
        edited_content = self.chapter_text.get("1.0", tk.END).strip()

        # 更新内存中的数据
        try:
            if 0 <= self.current_chapter_index < len(self.epub_data['chapters']):
                self.epub_data['chapters'][self.current_chapter_index]['content'] = edited_content
                self.update_status("章节内容已更新")
        except Exception as e:
            self.update_status(f"保存失败: {str(e)}")
            messagebox.showerror("保存错误", f"保存章节内容时出错: {str(e)}")

    def on_chapter_right_click(self, event):
        """章节列表右键菜单"""
        # 获取点击位置的项目索引
        index = self.chapter_listbox.nearest(event.y)
        if index < 0 or index >= self.chapter_listbox.size():
            return

        # 创建右键菜单
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="删除章节", command=lambda: self.delete_chapter(index))
        menu.add_command(label="导出为TXT", command=lambda: self.export_chapter_as_txt(index))
        menu.add_command(label="转换为MP3", command=lambda: self.convert_chapter_to_mp3(index))

        # 显示菜单
        menu.post(event.x_root, event.y_root)

    def delete_chapter(self, index):
        """删除指定的章节"""
        if not self.epub_data or 'chapters' not in self.epub_data:
            return

        if index < 0 or index >= len(self.epub_data['chapters']):
            return

        # 获取章节标题
        chapter_title = self.epub_data['chapters'][index]['title']

        # 确认删除
        if not messagebox.askyesno("确认删除", f"确定要删除章节 '{chapter_title}' 吗？"):
            return

        # 删除章节
        del self.epub_data['chapters'][index]

        # --- 新增代码开始 ---
        # 重新计算总字数（如果在epub_data中存储了这个信息）
        # 或者更准确地说，重新计算所有剩余章节的总字数
        total_words = 0
        if self.epub_data.get('chapters'):
            for chapter in self.epub_data['chapters']:
                total_words += len(chapter.get('content', '').replace('\n', '').replace('\r', '').replace(' ', ''))
        self.epub_data['total_words'] = total_words  # 更新内存中的总字数

        # 更新GUI上的总字数显示
        self.total_words_label.config(text=str(self.epub_data['total_words']))

        # 更新预计耗时标签
        estimated_time = self.calculate_estimated_time()
        if hasattr(self, 'estimated_time_var') and self.estimated_time_var:
            self.estimated_time_var.set(f"预计耗时：{estimated_time}")
        if hasattr(self, 'estimated_time_var_epub_tab') and self.estimated_time_var_epub_tab:
            self.estimated_time_var_epub_tab.set(f"预计耗时：{estimated_time}")
        # --- 新增代码结束 ---

        # 更新界面 (删除列表项、清空章节内容等)
        self.chapter_listbox.delete(index)

        # 更新当前章节索引
        if self.current_chapter_index == index:
            self.current_chapter_index = -1
            self.chapter_text.delete("1.0", tk.END)
            self.chapter_save_button.config(state="disabled")
            self.convert_chapter_button.config(state="disabled")
        elif self.current_chapter_index > index:
            self.current_chapter_index -= 1

        self.update_status(f"已删除章节: {chapter_title}")

    def export_all_chapters(self):
        """导出所有章节为TXT文件"""
        if not self.epub_data or 'chapters' not in self.epub_data or not self.epub_data['chapters']:
            messagebox.showerror("错误", "没有可导出的章节。")
            return

        # 选择导出目录
        export_dir = filedialog.askdirectory(title="选择导出目录")
        if not export_dir:
            return

        # 导出章节
        try:
            os.makedirs(export_dir, exist_ok=True)

            # 使用书名作为子目录
            book_dir = os.path.join(export_dir, self.epub_data['title'])
            os.makedirs(book_dir, exist_ok=True)

            # 导出每一章
            for i, chapter in enumerate(self.epub_data['chapters'], 1):
                title = chapter.get('title', f"章节 {i}")
                # 清理文件名，删除非法字符
                safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
                if not safe_title:
                    safe_title = f"章节{i}"

                # 确保文件名不超过系统限制 (Windows 通常是 255 字符)
                if len(safe_title) > 200:
                    safe_title = safe_title[:197] + "..."

                # 添加序号前缀确保顺序
                filename = f"{i:03d}-{safe_title}.txt"
                filepath = os.path.join(book_dir, filename)

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(chapter.get('content', ''))

            # 保存封面
            if self.epub_data['cover_data']:
                with open(os.path.join(book_dir, "cover.jpg"), 'wb') as f:
                    f.write(self.epub_data['cover_data'])

            self.update_status(f"已导出 {len(self.epub_data['chapters'])} 个章节到 {book_dir}")
            messagebox.showinfo("导出成功", f"成功导出 {len(self.epub_data['chapters'])} 个章节到\n{book_dir}")

        except Exception as e:
            self.update_status(f"导出失败: {str(e)}")
            messagebox.showerror("导出错误", f"导出章节时出错: {str(e)}")
            traceback.print_exc()

    def export_chapter_as_txt(self, index):
        """将单个章节导出为TXT文件"""
        if not self.epub_data or 'chapters' not in self.epub_data:
            return

        if index < 0 or index >= len(self.epub_data['chapters']):
            return

        chapter = self.epub_data['chapters'][index]
        title = chapter.get('title', f"章节 {index+1}")

        # 选择保存位置
        filename = re.sub(r'[\\/*?:"<>|]', "", title)
        if not filename:
            filename = f"章节{index+1}"

        filepath = filedialog.asksaveasfilename(
            title="保存章节为TXT",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            initialfile=filename
        )

        if not filepath:
            return

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(chapter.get('content', ''))

            self.update_status(f"已导出章节: {title}")

        except Exception as e:
            self.update_status(f"导出失败: {str(e)}")
            messagebox.showerror("导出错误", f"导出章节时出错: {str(e)}")

    def convert_current_chapter(self):
        """转换当前选中的章节为MP3"""
        if self.current_chapter_index < 0 or not self.epub_data or 'chapters' not in self.epub_data:
            messagebox.showerror("错误", "请先选择一个章节。")
            return

        self.convert_chapter_to_mp3(self.current_chapter_index)

    def convert_chapter_to_mp3(self, index):
        """将选定章节转换为MP3"""
        # 切换到有声书转换标签页
        # self.notebook.select(self.mp3_frame)

        if index < 0 or not self.epub_data or 'chapters' not in self.epub_data:
            messagebox.showerror("错误", "请先选择有效的章节。")
            return

        if index >= len(self.epub_data['chapters']):
            messagebox.showerror("错误", "章节索引无效。")
            return

        # 获取章节信息
        chapter = self.epub_data['chapters'][index]
        title = chapter.get('title', f"章节 {index + 1}")
        content = chapter.get('content', '')

        if not content.strip():
            messagebox.showerror("错误", f"章节 '{title}' 没有内容可转换。")
            return

        # 为此章节创建独立的临时目录
        temp_dir = tempfile.mkdtemp(prefix='epubReader_chapter_')

        # 保存章节内容到临时文件
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title:
            safe_title = f"章节{index + 1}"

        # 保存内容到临时TXT (仅用于显示)
        txt_path = os.path.join(temp_dir, f"{safe_title}.txt")
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # 设置输出目录
        output_dir = self.output_dir_var.get()

        if not output_dir:
            # 让用户选择输出目录
            output_dir = filedialog.askdirectory(title="选择MP3保存位置")
            if not output_dir:
                return
            self.output_dir_var.set(output_dir)

        os.makedirs(output_dir, exist_ok=True)

        # 设置输出文件路径
        output_mp3 = os.path.join(output_dir, f"{safe_title}.mp3")
        output_lrc = os.path.join(output_dir, f"{safe_title}.lrc")

        # 确认对话框
        message = (f"将章节 '{title}' 转换为MP3\n"
                   f"输出文件: {output_mp3}\n"
                   f"使用语音: {self.voice_var.get()}\n"
                   f"歌词每行字数: {self.chars_per_line_var.get()}\n"
                   f"\n确定要开始转换吗？ 如果有错误，请在【设置与自定义】中进行修改。")

        if not messagebox.askyesno("确认转换", message):
            return

        # 准备参数
        params = {
            "chapter_title": title,
            "content": content,
            "output_mp3": output_mp3,
            "output_lrc": output_lrc,
            "voice": self.voice_var.get(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_path": self.cover_path_var.get()
        }

        # 开始转换
        threading.Thread(
            target=self._convert_single_chapter_thread,
            args=(params,),
            daemon=True
        ).start()

    def _convert_single_chapter_thread(self, params):
        """在后台线程中转换单章节"""
        import time
        start_time = time.time()

        # 清空日志并添加开始信息
        self.message_queue.put(("log", f"开始转换章节: {params['chapter_title']}"))
        self.message_queue.put(("status", f"正在转换: {params['chapter_title']}..."))
        self.message_queue.put(("progress", 0))

        # 禁用转换按钮
        self.processing = True
        self.root.after(0, lambda: self.convert_chapter_button.config(state="disabled"))

        try:
            # 读取封面数据
            cover_data = None
            if params['cover_path'] and os.path.exists(params['cover_path']):
                with open(params['cover_path'], 'rb') as f:
                    cover_data = f.read()
            elif self.epub_data and 'cover_data' in self.epub_data and self.epub_data['cover_data']:
                cover_data = self.epub_data['cover_data']

            # 使用asyncio运行edge-tts
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            self.message_queue.put(("log", "正在生成MP3..."))

            # 执行TTS转换
            result = loop.run_until_complete(
                process_text_to_mp3(
                    params['content'],
                    params['output_mp3'],
                    params['output_lrc'],
                    params['voice'],
                    params['chars_per_line']
                )
            )

            elapsed = time.time() - start_time
            elapsed_str = f"{elapsed:.2f} 秒"

            if result:
                self.message_queue.put(("progress", 80))
                self.message_queue.put(("log", "MP3生成完成，添加标签..."))

                # 添加ID3标签
                if update_mp3_tag(
                        params['output_mp3'],
                        params['output_lrc'],
                        cover_data,
                        params['chapter_title'],
                        params['artist'],
                        params['album']
                ):
                    self.message_queue.put(("progress", 100))
                    self.message_queue.put(("log", f"处理完成: {os.path.basename(params['output_mp3'])}"))
                    self.message_queue.put(("log", f"本次转换耗时：{elapsed_str}"))
                    self.message_queue.put(("status", f"转换完成（耗时 {elapsed_str}）"))

                    def ask_open_dir():
                        # 确认是否打开输出目录
                        if messagebox.askyesno(
                                "转换完成",
                                f"已成功将章节 '{params['chapter_title']}' 转换为MP3。\n"
                                f"输出文件位置: {params['output_mp3']}\n\n"
                                f"是否打开输出目录？"
                        ):
                            # 打开目录
                            try:
                                os.startfile(os.path.dirname(params['output_mp3']))
                            except Exception as e:
                                messagebox.showerror("打开目录失败", str(e))

                    self.root.after(0, ask_open_dir)
                else:
                    self.message_queue.put(("log", "添加ID3标签失败"))
                    self.message_queue.put(("status", "转换部分完成: ID3标签失败"))
            else:
                self.message_queue.put(("log", "MP3生成失败"))
                self.message_queue.put(("status", "转换失败"))
                self.root.after(0, lambda: messagebox.showerror("转换失败", "生成MP3失败，请查看日志了解详情。"))

        except Exception as e:
            self.message_queue.put(("log", f"转换失败: {str(e)}"))
            self.message_queue.put(("status", f"错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("转换错误", str(e)))

        finally:
            # 启用转换按钮
            self.processing = False
            self.root.after(0, lambda: self.convert_chapter_button.config(state="normal"))

    def check_ffmpeg_on_startup(self):
        """启动时检查FFmpeg"""
        is_installed, ffmpeg_path = check_ffmpeg_installed()
        self.ffmpeg_installed = is_installed
        self.ffmpeg_path = ffmpeg_path
        
        if is_installed:
            self.ffmpeg_status_label.config(text=f"FFmpeg状态: ✓ 已安装", foreground="green")
            self.ffmpeg_install_button.config(state="disabled")
            self.ffmpeg_manual_install_button.config(state="disabled")
        else:
            if sys.platform == 'win32':
                self.ffmpeg_status_label.config(text="FFmpeg状态: ✗ 未安装", foreground="red")
                self.ffmpeg_install_button.config(state="normal")
                self.ffmpeg_manual_install_button.config(state="normal")
            else:
                self.ffmpeg_status_label.config(text="FFmpeg状态: ✗ 未安装 (请手动安装)", foreground="orange")
                self.ffmpeg_install_button.config(state="disabled")
                self.ffmpeg_manual_install_button.config(state="disabled")
    
    def install_ffmpeg_dialog(self):
        """显示FFmpeg安装对话框"""
        if self.processing:
            messagebox.showwarning("处理中", "有任务正在进行，请等待完成。")
            return
        
        msg = (
            "M4B格式需要FFmpeg支持。\n\n"
            "程序将自动下载并安装FFmpeg（约80-120MB）。\n"
            "下载可能需要几分钟时间，支持多镜像源和断点续传。\n\n"
            "是否继续？"
        )
        
        if not messagebox.askyesno("安装FFmpeg", msg):
            return
        
        # 创建进度对话框
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("安装FFmpeg")
        progress_dialog.geometry("450x180")
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()
        
        # 居中显示
        progress_dialog.update_idletasks()
        x = (progress_dialog.winfo_screenwidth() - 450) // 2
        y = (progress_dialog.winfo_screenheight() - 180) // 2
        progress_dialog.geometry(f"450x180+{x}+{y}")
        
        status_label = ttk.Label(progress_dialog, text="准备下载...", font=self.base_font)
        status_label.pack(pady=15)
        
        progress_bar = ttk.Progressbar(progress_dialog, length=400, mode='determinate')
        progress_bar.pack(pady=10)
        
        # 取消标志
        cancel_flag = {'cancelled': False}
        
        def cancel_download():
            if messagebox.askyesno("确认取消", "确定要取消安装吗？"):
                cancel_flag['cancelled'] = True
                progress_dialog.destroy()
        
        cancel_button = ttk.Button(progress_dialog, text="取消", command=cancel_download)
        cancel_button.pack(pady=5)
        
        # 禁用窗口关闭按钮（强制使用取消按钮）
        progress_dialog.protocol("WM_DELETE_WINDOW", cancel_download)
        
        # 上次更新时间（用于检测卡顿）
        last_update = {'time': time.time(), 'size': 0}
        
        def update_progress(current, total, status_msg):
            try:
                status_label.config(text=status_msg)
                if total > 0:
                    progress_bar['maximum'] = total
                    progress_bar['value'] = current
                
                # 更新时间戳
                last_update['time'] = time.time()
                last_update['size'] = current
                
                progress_dialog.update()
            except tk.TclError:
                # 对话框已关闭
                pass
        
        def check_cancel():
            return cancel_flag['cancelled']
        
        def install_thread():
            success, ffmpeg_path, error_msg = download_ffmpeg(update_progress, check_cancel)
            
            # 关闭进度对话框
            try:
                progress_dialog.destroy()
            except:
                pass
            
            if cancel_flag['cancelled']:
                return
            
            if success:
                self.ffmpeg_installed = True
                self.ffmpeg_path = ffmpeg_path
                self.ffmpeg_status_label.config(text="FFmpeg状态: ✓ 已安装", foreground="green")
                self.ffmpeg_install_button.config(state="disabled")
                messagebox.showinfo("安装成功", "FFmpeg安装成功！\n现在可以使用M4B格式导出了。")
            else:
                error_detail = error_msg or "未知错误"
                messagebox.showerror(
                    "安装失败", 
                    f"FFmpeg安装失败:\n{error_detail}\n\n"
                    "可能的原因：\n"
                    "1. 网络连接不稳定\n"
                    "2. 下载源暂时不可用\n"
                    "3. 防火墙阻止了下载\n\n"
                    "建议：\n"
                    "- 检查网络连接后重试\n"
                    "- 或手动下载安装FFmpeg"
                )
        
        threading.Thread(target=install_thread, daemon=True).start()
    
    def install_ffmpeg_from_local(self):
        """从本地ZIP文件安装FFmpeg"""
        if self.processing:
            messagebox.showwarning("处理中", "有任务正在进行，请等待完成。")
            return
        
        # 选择ZIP文件
        zip_file = filedialog.askopenfilename(
            title="选择FFmpeg ZIP文件",
            filetypes=[
                ("ZIP文件", "*.zip"),
                ("所有文件", "*.*")
            ]
        )
        
        if not zip_file:
            return
        
        # 创建进度对话框
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("安装FFmpeg")
        progress_dialog.geometry("450x150")
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()
        
        # 居中显示
        progress_dialog.update_idletasks()
        x = (progress_dialog.winfo_screenwidth() - 450) // 2
        y = (progress_dialog.winfo_screenheight() - 150) // 2
        progress_dialog.geometry(f"450x150+{x}+{y}")
        
        status_label = ttk.Label(progress_dialog, text="准备安装...", font=self.base_font)
        status_label.pack(pady=20)
        
        progress_bar = ttk.Progressbar(progress_dialog, length=400, mode='determinate')
        progress_bar.pack(pady=10)
        
        def update_progress(current, total, status_msg):
            try:
                status_label.config(text=status_msg)
                if total > 0:
                    progress_bar['maximum'] = total
                    progress_bar['value'] = current
                progress_dialog.update()
            except tk.TclError:
                pass
        
        def install_thread():
            success, ffmpeg_path, error_msg = install_ffmpeg_from_zip(zip_file, update_progress)
            
            # 关闭进度对话框
            try:
                progress_dialog.destroy()
            except:
                pass
            
            if success:
                self.ffmpeg_installed = True
                self.ffmpeg_path = ffmpeg_path
                self.ffmpeg_status_label.config(text="FFmpeg状态: ✓ 已安装", foreground="green")
                self.ffmpeg_install_button.config(state="disabled")
                self.ffmpeg_manual_install_button.config(state="disabled")
                messagebox.showinfo("安装成功", "FFmpeg安装成功！\n现在可以使用M4B格式导出了。")
            else:
                error_detail = error_msg or "未知错误"
                messagebox.showerror(
                    "安装失败", 
                    f"FFmpeg安装失败:\n{error_detail}\n\n"
                    "请确保选择的是有效的FFmpeg ZIP文件。"
                )
        
        threading.Thread(target=install_thread, daemon=True).start()
    
    def merge_mp3_to_m4b_dialog(self):
        """显示MP3合并为M4B的对话框"""
        if self.processing:
            messagebox.showwarning("处理中", "有任务正在进行，请等待完成。")
            return
        
        # 检查FFmpeg是否安装
        if not self.ffmpeg_installed:
            msg = "M4B格式需要安装FFmpeg。\n\n是否现在安装？"
            if messagebox.askyesno("需要FFmpeg", msg):
                self.install_ffmpeg_dialog()
            return
        
        # 选择MP3文件所在目录
        mp3_dir = filedialog.askdirectory(title="选择包含MP3文件的目录")
        if not mp3_dir:
            return
        
        # 查找目录中的所有MP3文件
        mp3_files = []
        for file in sorted(os.listdir(mp3_dir)):
            if file.lower().endswith('.mp3'):
                mp3_files.append(os.path.join(mp3_dir, file))
        
        if not mp3_files:
            messagebox.showerror("错误", f"目录 '{mp3_dir}' 中没有找到MP3文件。")
            return
        
        # 尝试从第一个MP3文件中提取元数据
        extracted_title = ""
        extracted_author = ""
        extracted_album = ""
        extracted_cover_path = None  # 保存提取的封面临时文件路径
        
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, APIC
            
            first_mp3 = mp3_files[0]
            audio = MP3(first_mp3, ID3=ID3)
            
            if audio.tags:
                # 提取书名（从专辑或标题）
                if 'TALB' in audio.tags:  # 专辑
                    extracted_album = str(audio.tags['TALB'].text[0])
                    extracted_title = extracted_album
                elif 'TIT2' in audio.tags:  # 标题
                    extracted_title = str(audio.tags['TIT2'].text[0])
                    # 尝试从标题中移除章节后缀
                    extracted_title = re.sub(r'[\s-]*第.+[章节集].*$', '', extracted_title)
                    extracted_title = re.sub(r'[\s-]*Chapter.+$', '', extracted_title, flags=re.IGNORECASE)
                
                # 提取作者
                if 'TPE1' in audio.tags:  # 艺术家/作者
                    extracted_author = str(audio.tags['TPE1'].text[0])
                
                # 提取封面图片
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):  # APIC是封面图片标签
                        # 保存封面到临时文件
                        import tempfile
                        # 根据MIME类型确定文件扩展名
                        ext = '.jpg'
                        if tag.mime == 'image/png':
                            ext = '.png'
                        elif tag.mime == 'image/gif':
                            ext = '.gif'
                        
                        temp_cover = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                        temp_cover.write(tag.data)
                        temp_cover.close()
                        extracted_cover_path = temp_cover.name
                        print(f"从 MP3 提取封面: {extracted_cover_path} ({len(tag.data)} 字节)")
                        break  # 只取第一个封面
                
                print(f"从 MP3 提取元数据: 书名={extracted_title}, 作者={extracted_author}, 封面={'是' if extracted_cover_path else '否'}")
        except Exception as e:
            print(f"提取 MP3 元数据失败: {e}")
            # 失败不影响后续操作，使用默认值
        
        # 让用户输入元数据
        merge_dialog = tk.Toplevel(self.root)
        merge_dialog.title("合并MP3为M4B")
        merge_dialog.geometry("550x600")  # 增加高度以容纳提示信息
        merge_dialog.transient(self.root)
        merge_dialog.grab_set()
        
        # 居中显示
        merge_dialog.update_idletasks()
        x = (merge_dialog.winfo_screenwidth() - 550) // 2
        y = (merge_dialog.winfo_screenheight() - 600) // 2
        merge_dialog.geometry(f"550x600+{x}+{y}")
        
        # 信息框架
        info_frame = ttk.LabelFrame(merge_dialog, text="MP3文件信息", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        ttk.Label(info_frame, text=f"找到 {len(mp3_files)} 个MP3文件", font=self.base_font).pack(anchor=tk.W, pady=5)
        
        # 文件列表框
        list_frame = ttk.Frame(info_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        file_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=self.small_font)
        file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=file_listbox.yview)
        
        for mp3_file in mp3_files:
            file_listbox.insert(tk.END, os.path.basename(mp3_file))
        
        # 元数据输入框架
        meta_frame = ttk.LabelFrame(merge_dialog, text="M4B元数据", padding=10)
        meta_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        # 决定默认值：优先使用从MP3提取的，其次是EPUB数据，最后是全局设置
        default_title = extracted_title if extracted_title else (self.album_var.get() if self.epub_data else "有声书")
        default_author = extracted_author if extracted_author else self.artist_var.get()
        default_cover = extracted_cover_path if extracted_cover_path else (self.cover_path_var.get() if self.cover_path_var.get() else "未选择")
        
        # 显示提示信息
        extracted_items = []
        if extracted_title:
            extracted_items.append("书名")
        if extracted_author:
            extracted_items.append("作者")
        if extracted_cover_path:
            extracted_items.append("封面")
        
        if extracted_items:
            hint_text = f"✔ 已从 MP3 文件自动提取: {', '.join(extracted_items)}"
            hint_label = ttk.Label(meta_frame, text=hint_text, foreground="green", font=self.small_font)
            hint_label.grid(row=0, column=0, columnspan=3, sticky=tk.W, padx=5, pady=(0, 5))
        
        ttk.Label(meta_frame, text="书名:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        title_var = tk.StringVar(value=default_title)
        ttk.Entry(meta_frame, textvariable=title_var, width=40).grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        
        ttk.Label(meta_frame, text="作者:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        author_var = tk.StringVar(value=default_author)
        ttk.Entry(meta_frame, textvariable=author_var, width=40).grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        
        # 封面选择
        ttk.Label(meta_frame, text="封面图片:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        cover_var = tk.StringVar(value=default_cover)
        cover_entry = ttk.Entry(meta_frame, textvariable=cover_var, width=30, state='readonly')
        cover_entry.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)
        
        def browse_cover():
            file_path = filedialog.askopenfilename(
                title="选择封面图片",
                filetypes=[
                    ("图片文件", "*.jpg *.jpeg *.png *.gif"),
                    ("所有文件", "*.*")
                ]
            )
            if file_path:
                cover_var.set(file_path)
        
        ttk.Button(meta_frame, text="选择...", command=browse_cover).grid(row=3, column=2, sticky=tk.W, padx=5, pady=5)
        
        # 按钮框架
        button_frame = ttk.Frame(merge_dialog)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def start_merge():
            book_title = title_var.get().strip()
            author = author_var.get().strip()
            
            if not book_title:
                messagebox.showerror("错误", "请输入书名。")
                return
            
            merge_dialog.destroy()
            
            # 选择输出文件
            safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
            if not safe_title:
                safe_title = "有声书"
            
            output_file = filedialog.asksaveasfilename(
                title="保存M4B文件",
                defaultextension=".m4b",
                filetypes=[("M4B文件", "*.m4b"), ("所有文件", "*.*")],
                initialfile=f"{safe_title}.m4b",
                initialdir=mp3_dir
            )
            
            if not output_file:
                return
            
            # 读取封面数据
            cover_data = None
            selected_cover = cover_var.get()
            
            # 添加日志输出
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"[封面信息] 封面路径: {selected_cover}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
            
            # 优先使用对话框中选择的封面
            if selected_cover and selected_cover != "未选择" and os.path.exists(selected_cover):
                try:
                    with open(selected_cover, 'rb') as f:
                        cover_data = f.read()
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, f"[封面信息] 从选择的文件读取封面成功，大小: {len(cover_data)} 字节\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
                except Exception as e:
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, f"[封面信息] 读取封面图片失败: {str(e)}\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
            # 如果未选择，尝试使用全局封面路径
            elif self.cover_path_var.get() and os.path.exists(self.cover_path_var.get()):
                try:
                    with open(self.cover_path_var.get(), 'rb') as f:
                        cover_data = f.read()
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, f"[封面信息] 从全局设置读取封面成功，大小: {len(cover_data)} 字节\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
                except Exception as e:
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, f"[封面信息] 读取封面图片失败: {str(e)}\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
            # 最后尝试使用EPUB封面
            elif self.epub_data and self.epub_data.get('cover_data'):
                cover_data = self.epub_data['cover_data']
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, f"[封面信息] 从EPUB数据读取封面成功，大小: {len(cover_data)} 字节\n")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            else:
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, "[封面信息] 未找到封面数据，M4B将不包含封面\n")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            
            # 准备元数据
            metadata = {
                'title': book_title,
                'author': author,
                'album': book_title
            }
            
            # 提取章节标题（从文件名）
            chapter_titles = []
            for mp3_file in mp3_files:
                filename = os.path.basename(mp3_file)
                # 移除扩展名和前缀序号（如001-）
                title = os.path.splitext(filename)[0]
                # 尝试移除序号前缀
                title = re.sub(r'^\d{3}-', '', title)
                chapter_titles.append(title)
            
            # 启动后台线程合并
            self.processing = True
            self.generate_ebook_button.config(state="disabled")
            self.merge_to_m4b_button.config(state="disabled")
            
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, "\n" + "="*50 + "\n")
            self.log_text.insert(tk.END, f"[合并M4B] 开始合并 {len(mp3_files)} 个MP3文件...\n")
            self.log_text.insert(tk.END, "="*50 + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
            
            params = {
                "mp3_files": mp3_files,
                "output_m4b": output_file,
                "metadata": metadata,
                "cover_data": cover_data,
                "chapter_titles": chapter_titles
            }
            
            threading.Thread(target=self._merge_mp3_thread, args=(params,), daemon=True).start()
        
        ttk.Button(button_frame, text="开始合并", command=start_merge).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="取消", command=merge_dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _merge_mp3_thread(self, params):
        """后台线程：合并MP3为M4B"""
        import time
        start_time = time.time()
        
        mp3_files = params["mp3_files"]
        output_m4b = params["output_m4b"]
        metadata = params["metadata"]
        cover_data = params.get("cover_data")
        chapter_titles = params.get("chapter_titles")
        
        self.message_queue.put(("log", f"合并 {len(mp3_files)} 个MP3文件为M4B格式..."))
        self.message_queue.put(("status", "正在合并为M4B..."))
        self.message_queue.put(("progress", 10))
        
        try:
            success, error_msg = merge_mp3_to_m4b(
                mp3_files,
                output_m4b,
                metadata,
                cover_data,
                self.ffmpeg_path,
                chapter_titles
            )
            
            elapsed = time.time() - start_time
            elapsed_str = f"{elapsed:.2f} 秒"
            
            if success:
                self.message_queue.put(("progress", 100))
                self.message_queue.put(("log", f"M4B文件生成成功: {os.path.basename(output_m4b)}"))
                self.message_queue.put(("log", f"合并耗时：{elapsed_str}"))
                self.message_queue.put(("status", f"M4B合并完成（耗时 {elapsed_str}）"))
                
                def show_success():
                    msg = f"M4B文件已成功生成！\n\n输出文件: {output_m4b}\n耗时: {elapsed_str}\n\n是否打开文件所在目录？"
                    if messagebox.askyesno("合并成功", msg):
                        try:
                            os.startfile(os.path.dirname(output_m4b))
                        except Exception as e:
                            messagebox.showerror("打开目录失败", str(e))
                
                self.root.after(0, show_success)
            else:
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put(("log", "M4B合并失败"))
                if error_msg:
                    self.message_queue.put(("log", f"失败原因: {error_msg}"))
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put(("status", "M4B合并失败"))
                
                def show_error():
                    error_detail = error_msg if error_msg else "未知错误"
                    messagebox.showerror("合并失败", f"M4B文件生成失败\n\n错误信息:\n{error_detail}\n\n请查看日志了解更多详情。")
                
                self.root.after(0, show_error)
        
        except Exception as e:
            self.message_queue.put(("log", f"M4B合并出错: {str(e)}"))
            self.message_queue.put(("status", f"错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("合并错误", str(e)))
        
        finally:
            self.processing = False
            self.root.after(0, lambda: self.generate_ebook_button.config(state="normal"))
            self.root.after(0, lambda: self.merge_to_m4b_button.config(state="normal"))
    
    def start_generate_ebook(self):
        """从内存章节数据批量生成有声书（MP3+LRC）"""
        if self.processing:
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
        
        # 清空之前的失败记录
        self.message_queue.put(("clear_failed_chapters", None))
        self.message_queue.put(("hide_retry_button", None))
        
        # 检查epub_data
        if not self.epub_data or not self.epub_data.get('chapters'):
            messagebox.showerror("错误", "没有可用的电子书章节。请先加载EPUB文件。")
            return
        
        # 检查M4B格式是否需要FFmpeg
        if self.output_format_var.get() == "M4B" and not self.ffmpeg_installed:
            msg = "M4B格式需要安装FFmpeg。\n\n是否现在安装？"
            if messagebox.askyesno("需要FFmpeg", msg):
                self.install_ffmpeg_dialog()
            return
        output_dir = self.output_dir_var.get()
        if not output_dir:
            output_dir = filedialog.askdirectory(title="选择输出目录")
            if not output_dir:
                return
            self.output_dir_var.set(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        # 读取封面
        cover_data = None
        cover_path = self.cover_path_var.get()
        if cover_path and os.path.exists(cover_path):
            try:
                with open(cover_path, 'rb') as f:
                    cover_data = f.read()
            except Exception as e:
                self.message_queue.put(("log", f"读取封面图片失败: {str(e)}"))
        elif self.epub_data.get('cover_data'):
            cover_data = self.epub_data['cover_data']
        # 确认
        total = len(self.epub_data['chapters'])
        output_format = self.output_format_var.get()
        format_desc = "MP3+LRC（分章节）" if output_format == "MP3" else "M4B有声书（合并）"
        
        msg = (
            f"将当前加载的电子书共 {total} 个章节生成有声书\n"
            f"输出格式: {format_desc}\n"
            f"输出目录: {output_dir}\n"
            f"使用语音: {self.voice_var.get()}\n"
            f"歌词每行字数: {self.chars_per_line_var.get()}\n"
            f"艺术家: {self.artist_var.get()}\n"
            f"专辑: {self.album_var.get()}\n"
            f"\n确定要开始转换吗？ 如果有错误，请在【设置与自定义】中进行修改。"
        )
        if not messagebox.askyesno("确认生成", msg):
            return
        # 禁用按钮
        self.processing = True
        self.generate_ebook_button.config(state="disabled")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        # 参数
        params = {
            "output_dir": output_dir,
            "chapters": self.epub_data['chapters'],
            "voice": self.voice_var.get(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data,
            "output_format": self.output_format_var.get(),
            "book_title": self.epub_data['title'],
            "ffmpeg_path": self.ffmpeg_path,
            "concurrent_threads": self.concurrent_threads_var.get()  # 添加并发线程数
        }
        threading.Thread(target=self._generate_ebook_thread, args=(params,), daemon=True).start()

    # def start_batch_convert(self):
    #     """开始批量转换"""
    #     if self.processing:
    #         messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
    #         return

    #     # 检查输入和输出目录
    #     input_dir = self.input_dir_var.get()
    #     output_dir = self.output_dir_var.get()

    #     if not input_dir or not os.path.isdir(input_dir):
    #         messagebox.showerror("错误", f"输入目录 '{input_dir}' 不存在或不是有效目录。")
    #         return

    #     # 检查输入目录中是否有TXT文件
    #     txt_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.txt')]
    #     if not txt_files:
    #         messagebox.showerror("错误", f"输入目录 '{input_dir}' 中没有找到任何TXT文件。")
    #         return

    #     # 创建输出目录
    #     try:
    #         os.makedirs(output_dir, exist_ok=True)
    #     except Exception as e:
    #         messagebox.showerror("错误", f"无法创建输出目录: {str(e)}")
    #         return

    #     # 读取封面数据
    #     cover_data = None
    #     cover_path = self.cover_path_var.get()
    #     if cover_path and os.path.exists(cover_path):
    #         try:
    #             with open(cover_path, 'rb') as f:
    #                 cover_data = f.read()
    #         except Exception as e:
    #             self.message_queue.put(("log", f"读取封面图片失败: {str(e)}"))

    #     # 确认对话框
    #     message = (f"将批量转换目录 '{input_dir}' 中的 {len(txt_files)} 个TXT文件为MP3\n"
    #               f"输出目录: {output_dir}\n"
    #               f"使用语音: {self.voice_var.get()}\n"
    #               f"歌词每行字数: {self.chars_per_line_var.get()}\n"
    #               f"艺术家: {self.artist_var.get()}\n"
    #               f"专辑: {self.album_var.get()}\n"
    #               f"\n确定要开始批量转换吗？")

    #     if not messagebox.askyesno("确认批量转换", message):
    #         return

    #     # 禁用转换按钮并标记处理中
    #     self.processing = True
    #     self.convert_button.config(state="disabled")

    #     # 清空日志
    #     self.log_text.config(state=tk.NORMAL)
    #     self.log_text.delete("1.0", tk.END)
    #     self.log_text.config(state=tk.DISABLED)

    #     # 准备参数
    #     params = {
    #         "input_dir": input_dir,
    #         "output_dir": output_dir,
    #         "txt_files": txt_files,
    #         "voice": self.voice_var.get(),
    #         "chars_per_line": self.chars_per_line_var.get(),
    #         "artist": self.artist_var.get(),
    #         "album": self.album_var.get(),
    #         "cover_data": cover_data
    #     }

    #     # 启动批处理线程
    #     threading.Thread(target=self._batch_convert_thread, args=(params,), daemon=True).start()

    def _generate_ebook_thread(self, params):
        """后台线程：从内存章节批量生成有声书（优化版：使用单一事件循环）"""
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading as th
        
        start_time = time.time()

        output_dir = params["output_dir"]
        chapters = params["chapters"]
        voice = params["voice"]
        chars_per_line = params["chars_per_line"]
        artist = params["artist"]
        album = params["album"]
        cover_data = params["cover_data"]
        output_format = params["output_format"]
        book_title = params["book_title"]
        ffmpeg_path = params.get("ffmpeg_path")
        concurrent_threads = params.get("concurrent_threads", 3)  # 默认3个线程
        
        self.message_queue.put(("log", f"开始生成有声电子书，共 {len(chapters)} 个章节"))
        self.message_queue.put(("log", f"输出格式: {output_format}"))
        self.message_queue.put(("log", f"并发线程数: {concurrent_threads}"))
        self.message_queue.put(("status", f"处理中: 0/{len(chapters)}"))
        
        mp3_files = [None] * len(chapters)  # 使用索引存储，保持顺序
        completed_count = 0
        lock = th.Lock()  # 线程锁保护共享资源
        
        # 创建单一的事件循环用于所有异步任务（在专用线程中复用）
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
        
        def process_single_chapter(index, chapter):
            """处理单个章节（复用事件循环）"""
            nonlocal completed_count
            
            i = index + 1  # 用于显示和文件命名
            title = chapter.get('title', f"章节{i}")
            content = chapter.get('content', '')
            
            # 生成安全的文件名
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = f"章节{i}"
            if len(safe_title) > 200:
                safe_title = safe_title[:197] + "..."
            
            output_mp3 = os.path.join(output_dir, f"{i:03d}-{safe_title}.mp3")
            output_lrc = os.path.join(output_dir, f"{i:03d}-{safe_title}.lrc")
            
            # 检查空章节
            if not content.strip():
                self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 跳过空章节: {title}"))
                with lock:
                    completed_count += 1
                    progress_pct = int(completed_count / len(chapters) * 100)
                    self.message_queue.put(("progress", progress_pct))
                    self.message_queue.put(("status", f"处理中: {completed_count}/{len(chapters)}"))
                return None
            
            try:
                self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 处理: {title}"))
                
                # 使用共享的事件循环执行TTS转换（线程安全）
                # 通过 run_coroutine_threadsafe 在事件循环线程中执行协程
                future = asyncio.run_coroutine_threadsafe(
                    process_text_to_mp3(
                        content,
                        output_mp3,
                        output_lrc,
                        voice,
                        chars_per_line
                    ),
                    event_loop
                )
                result = future.result()  # 阻塞等待结果
                
                if result:
                    # 添加ID3标签
                    if update_mp3_tag(output_mp3, output_lrc, cover_data, title, artist, album):
                        self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 完成: {title}"))
                        return_value = output_mp3
                    else:
                        self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 生成成功但标签失败: {title}，稍后将自动重试添加标签"))
                        return_value = output_mp3
                        # 记录标签失败
                        self.message_queue.put(("failed_chapter", {
                            "index": index, 
                            "title": title, 
                            "reason": "ID3标签添加失败", 
                            "mp3_path": output_mp3, 
                            "lrc_path": output_lrc
                        }))
                else:
                    self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 生成失败: {title}，稍后将自动重试"))
                    # 记录失败
                    self.message_queue.put(("failed_chapter", {
                        "index": index, 
                        "title": title, 
                        "reason": "MP3生成失败"
                    }))
                    return_value = None
                
                with lock:
                    completed_count += 1
                    progress_pct = int(completed_count / len(chapters) * 100)
                    self.message_queue.put(("progress", progress_pct))
                    self.message_queue.put(("status", f"处理中: {completed_count}/{len(chapters)}"))
                
                return return_value
                
            except Exception as e:
                self.message_queue.put(("log", f"[线程{th.current_thread().name[-1]}] 处理出错: {title} - {str(e)}"))
                self.message_queue.put(("failed_chapter", {
                    "index": index, 
                    "title": title, 
                    "reason": f"异常: {str(e)}"
                }))
                with lock:
                    completed_count += 1
                    progress_pct = int(completed_count / len(chapters) * 100)
                    self.message_queue.put(("progress", progress_pct))
                    self.message_queue.put(("status", f"处理中: {completed_count}/{len(chapters)}"))
                return None
        
        try:
            # 使用线程池并行处理
            with ThreadPoolExecutor(max_workers=concurrent_threads, thread_name_prefix="TTS-Worker") as executor:
                # 提交所有任务
                future_to_index = {executor.submit(process_single_chapter, idx, ch): idx 
                                  for idx, ch in enumerate(chapters)}
                
                # 等待完成并收集结果
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        result_mp3 = future.result()
                        if result_mp3:
                            mp3_files[index] = result_mp3
                    except Exception as e:
                        self.message_queue.put(("log", f"线程异常: 章节{index+1} - {str(e)}"))
            
            # 过滤掉None值，只保留成功生成的MP3，同时保留对应的章节信息
            valid_mp3_data = [(mp3, chapters[i]) for i, mp3 in enumerate(mp3_files) if mp3 is not None]
            mp3_files = [item[0] for item in valid_mp3_data]
            valid_chapter_titles = [item[1]['title'] for item in valid_mp3_data]
            
            # 如果选择M4B格式，合并所有MP3
            if output_format == "M4B" and mp3_files:
                self.message_queue.put(("log", "="*50))
                self.message_queue.put(("log", f"开始合并 {len(mp3_files)} 个章节为M4B格式..."))
                self.message_queue.put(("status", "正在合并为M4B..."))
                
                # 生成安全的文件名
                safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
                if not safe_title:
                    safe_title = "有声书"
                m4b_output = os.path.join(output_dir, f"{safe_title}.m4b")
                
                metadata = {
                    'title': book_title,
                    'author': artist,
                    'album': album
                }
                
                success, error_msg = merge_mp3_to_m4b(mp3_files, m4b_output, metadata, cover_data, ffmpeg_path, 
                                   valid_chapter_titles)
                
                if success:
                    self.message_queue.put(("log", f"M4B文件生成成功: {os.path.basename(m4b_output)}"))
                    self.message_queue.put(("log", "正在清理临时MP3文件..."))
                    
                    # 删除临时MP3和LRC文件
                    for mp3_file in mp3_files:
                        try:
                            if os.path.exists(mp3_file):
                                os.remove(mp3_file)
                            lrc_file = mp3_file.replace('.mp3', '.lrc')
                            if os.path.exists(lrc_file):
                                os.remove(lrc_file)
                        except Exception as e:
                            self.message_queue.put(("log", f"清理文件失败: {e}"))
                    
                    self.message_queue.put(("log", "临时文件清理完成"))
                else:
                    self.message_queue.put(("log", "="*50))
                    self.message_queue.put(("log", "M4B合并失败，保留原始MP3文件"))
                    if error_msg:
                        self.message_queue.put(("log", f"失败原因: {error_msg}"))
                    self.message_queue.put(("log", "="*50))
            
            elapsed = time.time() - start_time
            elapsed_str = f"{elapsed:.2f} 秒"
            self.message_queue.put(("progress", 100))
            
            # 统计失败和成功的章节数
            failed_count = len(self.failed_chapters)
            success_count = len(mp3_files)
            
            # 自动重试失败的章节
            if failed_count > 0 and not params.get("is_retry"):
                self.message_queue.put(("log", "="*50))
                self.message_queue.put(("log", f"检测到 {failed_count} 个章节处理失败，开始自动重试..."))
                self.message_queue.put(("log", "="*50))
                
                # 记录自动重试前的失败章节
                retry_failed = list(self.failed_chapters)
                # 清空失败列表，准备记录新的失败
                self.failed_chapters.clear()
                
                # 为重试创建一个新的事件循环
                retry_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(retry_loop)
                
                try:
                    for fc in retry_failed:
                        chapter_index = fc.get('index')
                        if chapter_index is None or chapter_index < 0 or chapter_index >= len(chapters):
                            continue
                        
                        chapter = chapters[chapter_index]
                        title = chapter.get('title', f"章节{chapter_index+1}")
                        content = chapter.get('content', '')
                        
                        if not content.strip():
                            continue
                        
                        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
                        if not safe_title:
                            safe_title = f"章节{chapter_index+1}"
                        if len(safe_title) > 200:
                            safe_title = safe_title[:197] + "..."
                        
                        output_mp3 = os.path.join(output_dir, f"{chapter_index+1:03d}-{safe_title}.mp3")
                        output_lrc = os.path.join(output_dir, f"{chapter_index+1:03d}-{safe_title}.lrc")
                        
                        self.message_queue.put(("log", f"[自动重试] {title}"))
                        
                        # 如果是标签失败，直接重试添加标签
                        if fc.get('reason') == "ID3标签添加失败" and fc.get('mp3_path'):
                            if update_mp3_tag(fc['mp3_path'], fc['lrc_path'], cover_data, title, artist, album):
                                self.message_queue.put(("log", f"[自动重试成功] 标签已添加: {title}"))
                            else:
                                self.message_queue.put(("log", f"[自动重试失败] 标签仍然失败: {title}"))
                                self.failed_chapters.append(fc)  # 仍然失败，重新记录
                        # 如果是MP3生成失败，重新生成
                        else:
                            result = retry_loop.run_until_complete(
                                process_text_to_mp3(
                                    content,
                                    output_mp3,
                                    output_lrc,
                                    voice,
                                    chars_per_line
                                )
                            )
                            
                            if result:
                                if update_mp3_tag(output_mp3, output_lrc, cover_data, title, artist, album):
                                    self.message_queue.put(("log", f"[自动重试成功] {title}"))
                                    mp3_files.append(output_mp3)  # 添加到成功列表
                                else:
                                    self.message_queue.put(("log", f"[自动重试成功] 但标签失败: {title}"))
                                    mp3_files.append(output_mp3)
                                    self.failed_chapters.append({"index": chapter_index, "title": title, "reason": "ID3标签添加失败", "mp3_path": output_mp3, "lrc_path": output_lrc})
                            else:
                                self.message_queue.put(("log", f"[自动重试失败] {title}"))
                                self.failed_chapters.append(fc)  # 仍然失败，重新记录
                finally:
                    retry_loop.close()
                
                # 更新统计
                failed_count = len(self.failed_chapters)
                success_count = len(mp3_files)
                
                if failed_count > 0:
                    self.message_queue.put(("log", f"自动重试完成，仍有 {failed_count} 个章节失败"))
                else:
                    self.message_queue.put(("log", "自动重试完成，所有章节处理成功！"))
            
            if output_format == "M4B":
                self.message_queue.put(("status", f"M4B生成完成（耗时 {elapsed_str}）"))
                self.message_queue.put(("log", f"M4B有声书生成完成!"))
            else:
                self.message_queue.put(("status", f"批量生成完成: {success_count}/{len(chapters)}（耗时 {elapsed_str}）"))
                self.message_queue.put(("log", f"所有章节有声书生成完成!"))
            
            self.message_queue.put(("log", f"本次批量生成总耗时：{elapsed_str}"))
            if failed_count > 0:
                self.message_queue.put(("log", f"警告: 自动重试后仍有 {failed_count} 个章节处理失败"))
                self.message_queue.put(("show_retry_button", None))  # 显示手动重试按钮
            else:
                self.message_queue.put(("hide_retry_button", None))  # 隐藏重试按钮

            def ask_open_dir():
                # 构建提示消息
                msg = f"已完成 {success_count}/{len(chapters)} 个章节生成\n输出目录: {output_dir}"
                if failed_count > 0:
                    msg += f"\n\n警告: 自动重试后仍有 {failed_count} 个章节处理失败\n可以点击'重新处理失败章节'按钮手动重试"
                msg += "\n\n是否打开输出目录？"
                
                if messagebox.askyesno("生成完成", msg):
                    try:
                        os.startfile(output_dir)
                    except Exception as e:
                        messagebox.showerror("打开目录失败", str(e))

            self.root.after(0, ask_open_dir)

        except Exception as e:
            self.message_queue.put(("log", f"批量生成任务出错: {str(e)}"))
            self.message_queue.put(("status", f"批量生成错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("批量生成错误", str(e)))
        finally:
            self.processing = False
            self.root.after(0, lambda: self.generate_ebook_button.config(state="normal"))

    def batch_convert_txt_dialog(self):
        """显示TXT批量转换对话框"""
        if self.processing:
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
        
        # 选择包含TXT文件的目录
        input_dir = filedialog.askdirectory(title="选择包含TXT文件的目录")
        if not input_dir:
            return
        
        # 查找目录中的所有TXT文件
        txt_files = []
        for file in sorted(os.listdir(input_dir)):
            if file.lower().endswith('.txt'):
                txt_files.append(file)
        
        if not txt_files:
            messagebox.showerror("错误", f"目录 '{input_dir}' 中没有找到TXT文件。")
            return
        
        # 选择输出目录
        output_dir = filedialog.askdirectory(title="选择MP3输出目录", initialdir=input_dir)
        if not output_dir:
            return
        
        # 读取封面数据
        cover_data = None
        cover_path = self.cover_path_var.get()
        if cover_path and os.path.exists(cover_path):
            try:
                with open(cover_path, 'rb') as f:
                    cover_data = f.read()
            except Exception as e:
                print(f"读取封面图片失败: {e}")
        
        # 准备参数
        params = {
            "input_dir": input_dir,
            "output_dir": output_dir,
            "txt_files": txt_files,
            "voice": self.voice_var.get(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data
        }
        
        # 启动后台线程
        self.processing = True
        self.generate_ebook_button.config(state="disabled")
        self.batch_txt_button.config(state="disabled")
        self.merge_to_m4b_button.config(state="disabled")
        
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "\n" + "="*50 + "\n")
        self.log_text.insert(tk.END, f"[TXT批量转换] 开始处理 {len(txt_files)} 个TXT文件...\n")
        self.log_text.insert(tk.END, "="*50 + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        
        threading.Thread(target=self._batch_convert_thread, args=(params,), daemon=True).start()

    def _batch_convert_thread(self, params):
        """在后台线程中执行批量转换"""
        input_dir = params["input_dir"]
        output_dir = params["output_dir"]
        txt_files = params["txt_files"]
        voice = params["voice"]
        chars_per_line = params["chars_per_line"]
        artist = params["artist"]
        album = params["album"]
        cover_data = params["cover_data"]

        self.message_queue.put(("log", f"开始批量转换 {len(txt_files)} 个文件"))
        self.message_queue.put(("status", f"批处理中: 0/{len(txt_files)}"))

        # 创建异步任务循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # 遍历处理每个文件
            for i, txt_file in enumerate(txt_files):
                # 更新进度
                progress_pct = int((i / len(txt_files)) * 100)
                self.message_queue.put(("progress", progress_pct))
                self.message_queue.put(("status", f"批处理中: {i}/{len(txt_files)}"))

                # 获取文件基本名称和路径
                base_name = os.path.splitext(txt_file)[0]
                txt_path = os.path.join(input_dir, txt_file)
                output_mp3 = os.path.join(output_dir, f"{base_name}.mp3")
                output_lrc = os.path.join(output_dir, f"{base_name}.lrc")

                # 如果MP3已存在，跳过或只添加标签
                if os.path.exists(output_mp3):
                    self.message_queue.put(("log", f"跳过已存在文件: {txt_file}"))

                    # 尝试添加/更新标签
                    if os.path.exists(output_lrc):
                        update_mp3_tag(output_mp3, output_lrc, cover_data, base_name, artist, album)

                    continue

                # 读取文本内容
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    if not content.strip():
                        self.message_queue.put(("log", f"跳过空文件: {txt_file}"))
                        continue

                    # 生成MP3和LRC
                    self.message_queue.put(("log", f"处理: {txt_file}"))

                    # 异步执行TTS转换
                    result = loop.run_until_complete(
                        process_text_to_mp3(
                            content,
                            output_mp3,
                            output_lrc,
                            voice,
                            chars_per_line
                        )
                    )

                    if result:
                        # 添加ID3标签
                        if update_mp3_tag(output_mp3, output_lrc, cover_data, base_name, artist, album):
                            self.message_queue.put(("log", f"完成: {txt_file}"))
                        else:
                            self.message_queue.put(("log", f"生成成功但标签失败: {txt_file}"))
                    else:
                        self.message_queue.put(("log", f"生成失败: {txt_file}"))

                except Exception as e:
                    self.message_queue.put(("log", f"处理文件 '{txt_file}' 时出错: {str(e)}"))
                    traceback.print_exc()

            # 完成所有处理
            self.message_queue.put(("progress", 100))
            self.message_queue.put(("status", f"批处理完成: {len(txt_files)}/{len(txt_files)}"))
            self.message_queue.put(("log", "批量转换完成!"))
            
            # 弹窗提示
            def show_complete_msg():
                messagebox.showinfo("批量转换完成",
                                  f"完成 {len(txt_files)} 个TXT文件批量转换\n"
                                  f"输出目录: {output_dir}")
            self.root.after(0, show_complete_msg)

        except Exception as e:
            self.message_queue.put(("log", f"批处理任务出错: {str(e)}"))
            self.message_queue.put(("status", f"批处理错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("批处理错误", str(e)))

        finally:
            loop.close()
            # 恢复UI状态
            self.processing = False
            self.root.after(0, lambda: self.generate_ebook_button.config(state="normal"))
            self.root.after(0, lambda: self.batch_txt_button.config(state="normal"))
            self.root.after(0, lambda: self.merge_to_m4b_button.config(state="normal"))

    def on_batch_complete(self):
        """批处理完成后的操作"""
        messagebox.showinfo("批处理完成",
                            f"完成文件批量转换\n"
                            f"输出目录: {self.output_dir_var.get()}")

    def retry_failed_chapters(self):
        """重新处理失败的章节"""
        if self.processing:
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
        
        if not self.failed_chapters:
            messagebox.showinfo("提示", "没有失败的章节需要重试。")
            return
        
        if not self.epub_data or not self.epub_data.get('chapters'):
            messagebox.showerror("错误", "没有可用的电子书章节。请先加载EPUB文件。")
            return
        
        # 显示失败章节列表
        failed_list = "\n".join([f"  - {fc['title']} ({fc.get('reason', '未知错误')})" 
                                 for fc in self.failed_chapters])
        msg = (
            f"共有 {len(self.failed_chapters)} 个章节处理失败：\n\n"
            f"{failed_list}\n\n"
            f"是否重新处理这些失败的章节？"
        )
        
        if not messagebox.askyesno("重新处理失败章节", msg):
            return
        
        # 准备重试的章节
        retry_chapters_data = []
        for fc in self.failed_chapters:
            chapter_index = fc.get('index')
            if chapter_index is not None and 0 <= chapter_index < len(self.epub_data['chapters']):
                retry_chapters_data.append(self.epub_data['chapters'][chapter_index])
        
        if not retry_chapters_data:
            messagebox.showerror("错误", "无法找到失败章节的有效数据。")
            return
        
        # 清空旧的失败记录
        self.message_queue.put(("clear_failed_chapters", None))
        
        # 隐藏重试按钮
        self.message_queue.put(("hide_retry_button", None))
        
        # 读取封面数据
        cover_data = None
        cover_path = self.cover_path_var.get()
        if cover_path and os.path.exists(cover_path):
            try:
                with open(cover_path, 'rb') as f:
                    cover_data = f.read()
            except Exception as e:
                self.message_queue.put(("log", f"读取封面图片失败: {str(e)}"))
        elif self.epub_data.get('cover_data'):
            cover_data = self.epub_data['cover_data']
        
        # 准备参数
        output_dir = self.output_dir_var.get()
        if not output_dir:
            messagebox.showerror("错误", "请先选择输出目录。")
            return
        
        params = {
            "output_dir": output_dir,
            "chapters": retry_chapters_data,
            "voice": self.voice_var.get(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data,
            "output_format": "MP3",  # 重试时只生成MP3，不合并
            "book_title": self.epub_data['title'],
            "ffmpeg_path": self.ffmpeg_path,
            "is_retry": True  # 标记这是重试任务
        }
        
        # 禁用按钮
        self.processing = True
        self.generate_ebook_button.config(state="disabled")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "\n" + "="*50 + "\n")
        self.log_text.insert(tk.END, f"[重试] 开始重新处理 {len(retry_chapters_data)} 个失败章节...\n")
        self.log_text.insert(tk.END, "="*50 + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        
        # 启动后台线程重试
        threading.Thread(target=self._generate_ebook_thread, args=(params,), daemon=True).start()

    def on_closing(self):
        """处理窗口关闭事件，始终弹窗确认"""
        if self.processing:
            msg = "有转换任务正在进行。\n确定要退出吗？"
        else:
            msg = "确定要退出吗？"
        if not messagebox.askyesno("确认退出", msg):
            return

        # 清理临时文件夹
        try:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR)
        except:
            pass

        self.root.destroy()

    def calculate_estimated_time(self):
        """计算预计耗时，基于字数"""
        if not self.epub_data or 'total_words' not in self.epub_data:
            return "00:00:00"

        total_words = self.epub_data['total_words']
        total_seconds = int(total_words / 80 + 0.5)  # 四舍五入

        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def main():
    """主函数"""
    # 创建并启动应用
    root = tk.Tk()
    root.attributes("-topmost", True)
    app = EpubToMp3App(root)

    # 居中窗口
    app.center_window()
    root.update_idletasks()

    # 取消置顶，仅在首次显示时置顶
    root.attributes("-topmost", False)

    # 设置窗口关闭处理
    root.protocol("WM_DELETE_WINDOW", app.on_closing)

    # 启动主循环
    root.mainloop()


if __name__ == "__main__":
    if sys.platform == "win32" and sys.executable.endswith("pythonw.exe"):
        sys.stdout = open(os.path.join(os.getenv("TEMP"), "epubReader_stdout.log"), "w")
        sys.stderr = open(os.path.join(os.getenv("TEMP"), "epubReader_stderr.log"), "w")
    main()