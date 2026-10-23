#!/usr/bin/env python3
"""
epubReader - EPUB电子书阅读器和有声书生成工具
功能：
1. 打开并阅读EPUB电子书，支持封面显示、章节列表浏览和章节内容编辑
2. 将章节内容转换成有声书MP3，自动生成LRC字幕，添加ID3标签
3. 支持批量转换模式和单章节转换模式
4. 支持M4B有声书格式导出，包含完整章节信息
"""

from __future__ import annotations

import posixpath
import json
import atexit
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
try:
    import requests
except ImportError:
    requests = None
import subprocess
import urllib.request
from urllib.error import URLError
import zipfile as zipfile_lib
from pathlib import Path
import uuid
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Any, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed


# ===== 统一任务调度层 =====
class TaskType(Enum):
    """任务类型"""
    TTS = "tts"                  # TTS转换任务
    FFMPEG = "ffmpeg"            # FFmpeg操作任务
    DOWNLOAD = "download"        # 下载任务
    PARSE_EPUB = "parse_epub"    # EPUB解析
    GENERAL = "general"          # 通用任务


class TaskCancelled(Exception):
    """任务被取消（协作式取消的标准异常）"""


@dataclass
class Task:
    """任务定义

    func 可以是普通函数或协程函数。若函数签名接受 cancel_event 关键字参数，
    调度器会自动注入一个 threading.Event，函数可通过轮询它实现协作式取消，
    或在取消时调用 task.raise_if_cancelled()。
    """
    task_id: str
    task_type: TaskType
    func: Callable                      # 要执行的函数（可以是普通函数或协程）
    args: tuple = field(default_factory=tuple)
    kwargs: Dict = field(default_factory=dict)
    callback: Optional[Callable] = None          # 成功回调
    error_callback: Optional[Callable] = None    # 错误回调
    progress_callback: Optional[Callable] = None # 进度回调
    cancellable: bool = True                     # 是否可取消
    timeout: Optional[float] = None              # 超时秒数，None 表示不限
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def cancel(self):
        """标记取消（由调度器调用）"""
        self.cancel_event.set()

    def is_cancelled(self):
        return self.cancel_event.is_set()

    def raise_if_cancelled(self):
        """在长循环中调用，取消时抛出 TaskCancelled"""
        if self.cancel_event.is_set():
            raise TaskCancelled("任务已被取消")


class TaskScheduler:
    """统一的后台任务调度器

    特性：
    - 同步任务在线程池中真正并发执行（并发上限 = max_workers）
    - 协程任务在专用事件循环线程中执行，与线程池互不阻塞
    - 协作式取消：cancel_task() 会设置 Task.cancel_event
    - 超时：Task.timeout 到期后取消并回调 error_callback
    - 进度：Task.progress_callback 经结果队列转发到主线程执行
    - stop() 用哨兵唤醒 worker，不依赖事件循环，可预期地退出
    """

    # 关闭哨兵：放入队列即可唤醒 worker
    _SENTINEL = object()

    def __init__(self, max_concurrent_tts=None, max_workers=None, queue_size=1000):
        # 兼容旧参数名 max_concurrent_tts；实际语义是"并发工作线程数"
        workers = max_workers if max_workers is not None else (
            max_concurrent_tts if max_concurrent_tts is not None else 3)
        self.max_workers = max(1, int(workers))
        self.max_concurrent_tts = self.max_workers  # 向后兼容属性

        self.task_queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self.result_queue = queue.Queue()
        self.dropped_callbacks = 0

        # 任务管理
        self.active_tasks: Dict[str, Task] = {}  # task_id -> Task
        self.cancelled_tasks = set()             # 已取消的 task_id
        self._futures: Dict[str, Any] = {}       # task_id -> Future（关闭时用于取消）
        self.task_lock = threading.RLock()

        # 线程池与事件循环
        self.worker_thread = None
        self.executor = None
        self.event_loop = None
        self.loop_thread = None
        self.running = False
        self._shutdown_timeout = 3.0

    # ---------- 生命周期 ----------
    def start(self):
        """启动调度器（幂等）"""
        with self.task_lock:
            if self.running:
                return
            self.running = True

        self.executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="TaskScheduler")
        self.worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TaskScheduler-Dispatch")
        self.worker_thread.start()

        self.event_loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=self._loop_loop, daemon=True, name="TaskScheduler-EventLoop")
        self.loop_thread.start()

    def _loop_loop(self):
        asyncio.set_event_loop(self.event_loop)
        try:
            self.event_loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self.event_loop)
                for item in pending:
                    item.cancel()
                if pending:
                    self.event_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.event_loop.close()
            except Exception:
                pass

    def stop(self, timeout=None):
        """停止调度器：唤醒 worker、清空待执行任务、关闭线程池与事件循环"""
        wait = self._shutdown_timeout if timeout is None else timeout
        with self.task_lock:
            if not self.running:
                return
            self.running = False

        # 取消尚未执行的任务
        with self.task_lock:
            pending_ids = [tid for tid, task in self.active_tasks.items()
                           if task.cancellable]
            in_flight = list(self._futures.items())
        for task_id in pending_ids:
            self.cancel_task(task_id)

        # 取消尚未开始执行的 future（Python 3.8 无 shutdown(cancel_futures=True)）
        for _task_id, future in in_flight:
            try:
                if not future.running():
                    future.cancel()
            except Exception:
                pass

        try:
            self.task_queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=wait)

        if self.executor:
            # 先 cancel 掉排队中的工作，再等一小段时间让真正在跑的收尾，
            # 避免"关不掉"和"被卡死任务无限阻塞退出"两种极端
            try:
                self.executor.shutdown(wait=False)
            except Exception:
                pass
            for _task_id, future in in_flight:
                try:
                    future.result(timeout=wait)
                except Exception:
                    pass

        if self.event_loop and self.loop_thread and self.loop_thread.is_alive():
            try:
                self.event_loop.call_soon_threadsafe(self.event_loop.stop)
            except Exception:
                pass
            self.loop_thread.join(timeout=wait)

    # ---------- 任务提交与取消 ----------
    def submit_task(self, task: Task) -> str:
        """提交任务（线程安全）

        Returns:
            task_id: 任务ID

        Raises:
            RuntimeError: 调度器未启动或队列已满
        """
        if not self.running:
            raise RuntimeError("TaskScheduler 未启动，请先调用 start()")

        with self.task_lock:
            self.active_tasks[task.task_id] = task
            self.cancelled_tasks.discard(task.task_id)

        try:
            self.task_queue.put_nowait(task)
        except queue.Full:
            with self.task_lock:
                self.active_tasks.pop(task.task_id, None)
            raise RuntimeError("任务队列已满，请等待当前任务完成")
        return task.task_id

    def cancel_task(self, task_id: str) -> bool:
        """取消任务（设置 cancel_event，并尽量取消未开始执行的任务）"""
        with self.task_lock:
            task = self.active_tasks.get(task_id)
            if task is None or not task.cancellable:
                return False
            self.cancelled_tasks.add(task_id)
            task.cancel()
            future = getattr(task, '_future', None)
        # future.cancel() 只对尚未开始执行的任务有效
        if future is not None:
            try:
                future.cancel()
            except Exception:
                pass
        return True

    def is_task_cancelled(self, task_id: str) -> bool:
        """检查任务是否被取消"""
        with self.task_lock:
            return task_id in self.cancelled_tasks

    def get_cancel_event(self, task_id: str):
        """取出任务的取消事件，便于把取消信号传进长循环"""
        with self.task_lock:
            task = self.active_tasks.get(task_id)
            return task.cancel_event if task is not None else None

    def get_result(self):
        """获取结果（非阻塞）

        Returns:
            tuple: ("callback", callback, args) | ("progress", callback, args) | None
        """
        try:
            return self.result_queue.get_nowait()
        except queue.Empty:
            return None

    def pending_count(self):
        """待执行 + 执行中的任务数"""
        with self.task_lock:
            return len(self.active_tasks)

    # ---------- 内部实现 ----------
    def _emit(self, callback, args):
        """把回调投递到结果队列，由主线程执行"""
        if callback is None:
            return
        try:
            self.result_queue.put_nowait(("callback", callback, args))
        except queue.Full:
            self.dropped_callbacks += 1
            print(f"警告: 回调队列已满，已丢弃 {self.dropped_callbacks} 个回调",
                  file=sys.stderr)

    def _emit_progress(self, task, args):
        """转发任务进度回调（经结果队列，主线程执行）"""
        if task.progress_callback is None:
            return
        try:
            self.result_queue.put_nowait(("progress", task.progress_callback, args))
        except queue.Full:
            self.dropped_callbacks += 1

    def _worker_loop(self):
        """派发线程：只负责领取任务并派发，不阻塞等待任务完成"""
        while True:
            try:
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                if not self.running:
                    break
                continue

            if task is self._SENTINEL:
                break

            # 注意：任务完成前必须留在 active_tasks 中，
            # 否则 cancel_task() 会找不到它、完成回调也无从判断是否重复上报。
            with self.task_lock:
                cancelled = task.task_id in self.cancelled_tasks
            if cancelled or task.cancel_event.is_set():
                self._finish_cancelled(task)
                continue

            try:
                self._dispatch(task)
            except Exception as e:
                print(f"派发任务 {task.task_id} 失败: {e}", file=sys.stderr)
                traceback.print_exc()
                self._finish(task, task.error_callback, (e,))

    def _finish(self, task, callback, args):
        """上报任务结果（保证每个任务只上报一次）"""
        with self.task_lock:
            if task.task_id not in self.active_tasks:
                return  # 已被超时看门狗处理
            self.active_tasks.pop(task.task_id, None)
            self.cancelled_tasks.discard(task.task_id)
            self._futures.pop(task.task_id, None)
        self._emit(callback, args)

    def _build_kwargs(self, task):
        """按需注入 cancel_event / progress_callback"""
        kwargs = dict(task.kwargs)
        if 'cancel_event' in kwargs:
            return kwargs
        try:
            import inspect
            params = inspect.signature(task.func).parameters
        except (TypeError, ValueError):
            return kwargs
        if 'cancel_event' in params:
            kwargs['cancel_event'] = task.cancel_event
        if 'progress_callback' in params and task.progress_callback is not None:
            kwargs['progress_callback'] = lambda *a: self._emit_progress(task, a)
        return kwargs

    def _dispatch(self, task):
        """按同步/异步分别派发到线程池或事件循环"""
        kwargs = self._build_kwargs(task)

        if asyncio.iscoroutinefunction(task.func):
            if self.event_loop is None or self.event_loop.is_closed():
                raise RuntimeError("事件循环不可用，无法执行协程任务")
            future = asyncio.run_coroutine_threadsafe(
                task.func(*task.args, **kwargs), self.event_loop)
        else:
            def run_sync():
                return task.func(*task.args, **kwargs)
            future = self.executor.submit(run_sync)

        task._future = future
        with self.task_lock:
            self._futures[task.task_id] = future
        future.add_done_callback(lambda f: self._on_task_done(task, f))
        self._watch_timeout(task, future)

    def _watch_timeout(self, task, future):
        """超时看门狗：到期未完成则取消并回调错误"""
        if not task.timeout or task.timeout <= 0:
            return

        def watchdog():
            try:
                future.result(timeout=task.timeout)
            except Exception:
                pass
            if future.done():
                return  # 正常结束路径会负责上报
            self.cancel_task(task.task_id)
            try:
                future.cancel()
            except Exception:
                pass
            self._finish(
                task, task.error_callback,
                (TimeoutError(f"任务执行超时（>{task.timeout}s）: {task.task_id}"),))

        threading.Thread(target=watchdog, daemon=True,
                         name="TaskScheduler-Timeout").start()

    def _on_task_done(self, task, future):
        """任务结束（成功/失败/取消）的统一收尾"""
        with self.task_lock:
            in_flight = task.task_id in self.active_tasks
            was_cancelled = task.task_id in self.cancelled_tasks
        if not in_flight:
            return  # 超时看门狗已上报

        if was_cancelled or task.cancel_event.is_set():
            self._finish(task, task.error_callback,
                         (TaskCancelled("任务已被取消"),))
            return

        try:
            result = future.result()
        except TaskCancelled as e:
            self._finish(task, task.error_callback, (e,))
        except Exception as e:
            print(f"Task {task.task_id} failed: {e}", file=sys.stderr)
            traceback.print_exc()
            self._finish(task, task.error_callback, (e,))
        else:
            self._finish(task, task.callback, (result,))

    def _finish_cancelled(self, task):
        """任务在开始执行前就被取消"""
        self._finish(task, task.error_callback, (TaskCancelled("任务已被取消"),))


# ===== 配置选项 =====
class AppConfig:
    """应用配置类 - 集中管理所有配置项"""
    
    # TTS配置
    DEFAULT_VOICE = "zh-CN-YunjianNeural"  # edge-tts 默认语音
    DEFAULT_TTS_ENGINE = "edge-tts"  # 默认TTS引擎
    MAX_RETRIES = 3  # TTS最大重试次数
    RETRY_DELAY_SECONDS = 5  # TTS重试间隔（秒）
    # 朗读速度的唯一来源：约 4.5 个汉字/秒（edge-tts 默认语速实测值）。
    # 旧代码同时存在 0.3 秒/字 与 80 字/秒 两个互相矛盾的估算，此处统一。
    CHARS_PER_SECOND = 4.5  # 每秒朗读的字符数
    READING_SECONDS_PER_CHAR = 1.0 / 4.5  # 每个字符的朗读时间（秒）
    # edge-tts 单次请求的文本长度上限，超出必须分块（否则长章节必然中途断流）
    MAX_TTS_CHARS = 1200
    
    # 文件配置
    DEFAULT_ARTIST = "未知作者"  # MP3默认艺术家
    DEFAULT_CHARS_PER_LINE = 15  # LRC歌词每行字数
    MAX_FILENAME_LENGTH = 200  # 文件名最大长度
    # 兼容常量：实际临时文件已改为实例级目录（见 EpubToMp3App.session_temp_dir），
    # 此常量不再作为任何清理目标，避免两个实例互删文件。
    TEMP_DIR = os.path.join(tempfile.gettempdir(), "epub2mp3")
    LYRIC_LANG = "zho"  # 歌词语言代码

    # 配置持久化位置
    CONFIG_DIR = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'epubReader')
    CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
    LOG_PATH = os.path.join(CONFIG_DIR, 'epubReader.log')
    
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

    @staticmethod
    def bundled_ffmpeg_dir():
        """PyInstaller 打包运行时随包附带的 ffmpeg 目录（无则返回 None）"""
        base = getattr(sys, '_MEIPASS', None)
        if not base:
            return None
        candidate = os.path.join(base, 'ffmpeg')
        return candidate if os.path.isdir(candidate) else None

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


# ===== 日志与配置持久化 =====
def _retry_io(func, attempts=4, base_delay=0.25):
    """执行可能因短暂占用而失败的文件操作（Windows 上偶发 Permission denied）

    Returns:
        tuple: (success: bool, result_or_error)
    """
    last_error = None
    for index in range(attempts):
        try:
            return True, func()
        except PermissionError as exc:
            last_error = exc
            time.sleep(base_delay * (index + 1))
        except OSError as exc:
            last_error = exc
            if getattr(exc, 'errno', None) not in (13, 11, 32, 33):
                break
            time.sleep(base_delay * (index + 1))
    return False, last_error



def setup_logging(log_path=None, level=None):
    """配置文件日志（轮转），并把 print 的诊断信息纳入统一日志

    Returns:
        logging.Logger: 已配置的 logger
    """
    import logging
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger('epubReader')
    if logger.handlers:
        return logger

    logger.setLevel(level or logging.INFO)
    target = log_path or AppConfig.LOG_PATH

    def build_handler():
        os.makedirs(os.path.dirname(target), exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(threadName)s %(message)s'))
        return handler

    # 短暂占用（同步客户端/杀毒扫描）很常见，重试几次即可
    ok, result = _retry_io(build_handler)
    if ok:
        logger.addHandler(result)
    else:
        print(f"无法建立日志文件（可忽略）: {result}", file=sys.stderr)

    # 同时输出到 stderr，便于命令行运行时观察
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(stream)
    return logger


class ConfigStore:
    """把用户设置持久化到 %APPDATA%\\epubReader\\config.json

    只保存"纯数据"设置；损坏或缺失时静默回退到默认值，绝不让配置问题阻断启动。
    """

    KEYS = (
        'tts_engine', 'voice', 'chars_per_line',
        'output_format', 'concurrent_threads', 'output_dir', 'album', 'artist',
    )

    def __init__(self, path=None):
        self.path = path or AppConfig.CONFIG_PATH
        self.data = {}
        self.load()

    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                self.data = {k: v for k, v in payload.items() if k in self.KEYS}
        except FileNotFoundError:
            self.data = {}
        except (ValueError, OSError) as e:
            print(f"读取配置失败，使用默认值: {e}", file=sys.stderr)
            self.data = {}
        return self.data

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, **values):
        self.data.update({k: v for k, v in values.items() if k in self.KEYS})

    def save(self):
        """保存配置；失败只记录日志，不影响使用"""
        def write():
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True

        ok, result = _retry_io(write)
        if not ok:
            print(f"保存配置失败（可忽略）: {result}", file=sys.stderr)
        return ok


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
        """关闭EPUB文件，释放 zipfile 与 ebooklib 持有的资源"""
        if self.epub_zip:
            try:
                self.epub_zip.close()
            except Exception as e:
                print(f"关闭EPUB压缩包失败: {e}", file=sys.stderr)
            finally:
                self.epub_zip = None
        # ebooklib 的 EpubBook 可能持有内部文件句柄，尽量一并释放
        book = self.book
        if book is not None:
            closer = getattr(book, 'close', None)
            if callable(closer):
                try:
                    closer()
                except Exception as e:
                    print(f"关闭EPUB书籍对象失败: {e}", file=sys.stderr)
            self.book = None

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
            # 智能查找 content.opf 文件
            opf_path = None
            if self.opf_dir:
                opf_path = self.opf_dir + "content.opf"
            else:
                # 如果没有目录，尝试直接查找
                opf_path = "content.opf"
            
            # 尝试读取 opf 文件
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
                                cover_path = posixpath.join(self.opf_dir, cover_href).lstrip('/')
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
                        if 'cover-image' in (item.get('properties') or '').split():  # Check for 'cover-image' property
                            cover_href = item.get('href')
                            cover_path = posixpath.join(self.opf_dir, cover_href).lstrip('/')
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
                        item_id = (item.get('id') or '').lower()
                        item_href = (item.get('href') or '').lower()
                        media_type = item.get('media-type') or ''

                        if (media_type.startswith('image/') and
                                ('cover' in item_id or 'cover' in item_href)):
                            cover_href = item.get('href')
                            cover_path = posixpath.join(self.opf_dir, cover_href).lstrip('/')
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
                # content.opf 不在预期位置，尝试查找
                found_opf = None
                for name in self.epub_zip.namelist():
                    if name.lower().endswith('.opf'):  # 查找任何 .opf 文件
                        found_opf = name
                        break
                
                if found_opf:
                    # 找到了，使用正确的路径重试
                    try:
                        content_opf = self.epub_zip.read(found_opf)
                        root = ET.fromstring(content_opf)
                        # 更新 opf_dir 以供后续使用
                        self.opf_dir = os.path.dirname(found_opf)
                        if self.opf_dir and not self.opf_dir.endswith('/'):
                            self.opf_dir += '/'
                        # 成功找到并读取，继续处理封面逻辑
                        # (注意：这里需要重复上面的封面查找逻辑)
                    except Exception:
                        pass  # 静默失败，ebooklib 已经成功读取了书籍
                # 如果找不到也不显示警告，因为 ebooklib 已经成功读取了书籍内容
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
        # 根据累计字符数估算时间（仅在 TTS 未提供时间轴时使用）
        estimated_seconds = cumulative_chars * AppConfig.READING_SECONDS_PER_CHAR
        minutes = int(estimated_seconds // 60)
        seconds = int(estimated_seconds % 60)
        milliseconds = int((estimated_seconds % 1) * 100)
        lrc_time = f"[{minutes:02d}:{seconds:02d}.{milliseconds:02d}]"
        lrc_lines.append(f"{lrc_time}{line}")
        
        # 累加字符数
        cumulative_chars += len(line)
    
    return '\n'.join(lrc_lines)


# ===== 长文本分块与 SRT 时间轴工具 =====
# edge-tts 对单次请求的文本长度有上限，超长章节必须分块合成后拼接，
# 否则会在流的中途断开，表现为“重试 3 次后仍然生成失败”。
_SENTENCE_END = re.compile(r'(?<=[。！？!?；;])')
_MINOR_PAUSE = re.compile(r'(?<=[，,、：:）)】」』])')
_SRT_TIME = re.compile(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})')


def _split_by_boundary(text, pattern, limit):
    """按边界正则切分文本，并把碎片合并成不超过 limit 的块"""
    result = []
    current = ""
    for segment in pattern.split(text):
        if len(current) + len(segment) <= limit:
            current += segment
        else:
            if current:
                result.append(current)
            current = segment
    if current:
        result.append(current)
    return result


def _hard_split(text, limit):
    """无标点的超长段落兜底硬切"""
    return [text[i:i + limit] for i in range(0, len(text), limit)] if text else []


def chunk_text(text, limit=None):
    """把长文本切成不超过 limit 字符的块（优先在句子/停顿标点处断开）

    Returns:
        list: 文本块列表；输入为空时返回空列表
    """
    if limit is None:
        limit = AppConfig.MAX_TTS_CHARS
    if limit < 1:
        limit = AppConfig.MAX_TTS_CHARS
    if not text:
        return []

    chunks = []
    buffer = ""
    for paragraph in text.splitlines():
        if not paragraph.strip():
            continue
        if len(buffer) + len(paragraph) + 1 <= limit:
            buffer = f"{buffer}\n{paragraph}" if buffer else paragraph
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        for piece in _split_by_boundary(paragraph, _SENTENCE_END, limit):
            for sub in _split_by_boundary(piece, _MINOR_PAUSE, limit):
                if len(sub) <= limit:
                    if sub:
                        chunks.append(sub)
                else:
                    chunks.extend(_hard_split(sub, limit))
    if buffer:
        chunks.append(buffer)
    return chunks


def _format_srt_time(total_ms):
    """毫秒 -> SRT 时间戳 00:00:00,000"""
    if total_ms < 0:
        total_ms = 0
    hours, remainder = divmod(int(total_ms), 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def shift_srt(srt_content, offset_ms, index_base=0):
    """整体平移 SRT 的时间轴与序号，用于分块合成后拼接

    Args:
        srt_content: 单个文本块的 SRT 内容
        offset_ms: 该块在整章音频中的起始毫秒偏移
        index_base: 前面已产生的字幕条数（用于重新编号）

    Returns:
        tuple: (平移后的SRT文本, 新的字幕条数)
    """
    if not srt_content or not srt_content.strip():
        return "", index_base

    blocks = [b for b in srt_content.strip().split('\n\n') if b.strip()]
    output = []
    index = index_base
    for block in blocks:
        lines = block.split('\n')
        time_line_pos = next(
            (pos for pos, line in enumerate(lines) if '-->' in line), None)
        if time_line_pos is None:
            continue
        matches = list(_SRT_TIME.finditer(lines[time_line_pos]))
        if not matches:
            continue
        start = (int(matches[0].group(1)) * 3600 + int(matches[0].group(2)) * 60
                 + int(matches[0].group(3))) * 1000 + int(matches[0].group(4))
        end = None
        if len(matches) > 1:
            end = (int(matches[1].group(1)) * 3600 + int(matches[1].group(2)) * 60
                   + int(matches[1].group(3))) * 1000 + int(matches[1].group(4))
        text = '\n'.join(lines[time_line_pos + 1:])
        if not text.strip():
            continue
        index += 1
        start += offset_ms
        end = (end + offset_ms) if end is not None else start + 3000
        output.append(f"{index}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}")
    if not output:
        return "", index_base
    return '\n\n'.join(output) + '\n', index


# ===== MP3 时长探测（纯标准库，不依赖 mutagen） =====
# 注意：edge-tts 输出的是 24kHz 的 MPEG-2 Layer III 帧（帧长 144 字节），
# 而不是 MPEG-1；按 MPEG-1 解析会把时长算成真实值的一半，进而让字幕错位。
_MPEG1_BITRATES = {
    1: [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],  # Layer I
    2: [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],      # Layer II
    3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],       # Layer III
}
_MPEG2_BITRATES = {
    1: [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    3: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000)


def mp3_duration_ms(path):
    """遍历 MPEG 音频帧计算时长（毫秒），失败返回 0

    这是分块拼接时推进字幕时间轴的关键：用真实音频长度而不是估算值，
    避免 LRC 与音频逐渐错位。
    """
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return 0

    position = 0
    total_samples = 0
    sample_rate = 0
    size = len(data)
    while position + 4 <= size:
        if data[position] != 0xFF or (data[position + 1] & 0xE0) != 0xE0:
            position += 1
            continue

        version_bits = (data[position + 1] >> 3) & 0x03
        layer_bits = (data[position + 1] >> 1) & 0x03
        bitrate_index = (data[position + 2] >> 4) & 0x0F
        rate_index = (data[position + 2] >> 2) & 0x03
        padding = (data[position + 2] >> 1) & 0x01

        # version_bits: 3=MPEG-1, 2=MPEG-2, 0=MPEG-2.5, 1=保留
        # layer_bits:   3=Layer I, 2=Layer II, 1=Layer III, 0=保留
        if version_bits == 1 or layer_bits == 0 or rate_index == 3:
            position += 1
            continue
        layer = 4 - layer_bits
        if version_bits == 3:
            bitrate = _MPEG1_BITRATES[layer][bitrate_index] * 1000
            rate = _MPEG1_SAMPLE_RATES[rate_index]
        else:
            factor = 2 if version_bits == 2 else 4
            bitrate = _MPEG2_BITRATES[layer][bitrate_index] * 1000
            rate = _MPEG1_SAMPLE_RATES[rate_index] // factor
        if not bitrate:
            position += 1
            continue

        if layer == 1:
            samples = 384
            frame_length = (12 * bitrate // rate + padding) * 4
        elif layer == 3 and version_bits != 3:
            # MPEG-2/2.5 Layer III：每帧 576 个采样，一个"槽"即 1 字节
            samples = 576
            frame_length = 72 * bitrate // rate + padding
        else:
            samples = 1152
            frame_length = 144 * bitrate // rate + padding

        if frame_length <= 4:
            position += 1
            continue
        total_samples += samples
        sample_rate = rate
        position += frame_length

    if not total_samples or not sample_rate:
        return 0
    return int(total_samples * 1000.0 / sample_rate)


def get_audio_duration_ms(path):
    """获取音频文件时长（毫秒）：优先 mutagen，其次自带 MPEG 解析，最后返回 0"""
    try:
        audio = MP3(path)
        if audio.info and audio.info.length > 0:
            return int(audio.info.length * 1000)
    except Exception:
        pass
    return mp3_duration_ms(path)


async def run_coros_with_limit(coros, limit):
    """并发执行多个协程，最多同时 limit 个（单个失败不影响其他任务）

    Args:
        coros: 协程的可迭代对象
        limit: 最大并发数

    Returns:
        list: 每个协程的结果；抛异常的条目为异常对象本身
    """
    semaphore = asyncio.Semaphore(max(1, int(limit or 1)))

    async def guarded(coro):
        async with semaphore:
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return exc

    coro_list = list(coros)
    if not coro_list:
        return []
    return await asyncio.gather(*(guarded(c) for c in coro_list))


# ===== TTS引擎抽象层 =====
class TTSEngine:
    """基础TTS引擎抽象类"""
    
    @staticmethod
    async def generate_audio(text, output_audio, voice, **kwargs):
        """
        生成音频文件
        
        Args:
            text: 要转换的文本
            output_audio: 输出MP3文件路径
            voice: 语音名称
            **kwargs: 其他参数
        
        Returns:
            tuple: (success: bool, srt_content: str or None)
        """
        raise NotImplementedError()
    
    @staticmethod
    def get_available_voices():
        """获取可用语音列表"""
        raise NotImplementedError()


def _describe_edge_tts_failure(error):
    """把 edge-tts 的底层错误翻译成用户能理解、能行动的提示

    403 是当前最常见的情况，而且**原因已经查清，不是网络问题**：
    微软的朗读端点会校验握手 URL 里的 `Sec-MS-GEC-Version`，要求它形如
    `2-<Chromium 版本>`；edge-tts 7.2.x 及更早发的是 `1-...`，被一律拒绝
    （同一时刻用同一 token 访问 /voices/list 仍返回 200，说明网络和凭据都正常）。
    edge-tts 7.3.0 起改发 `2-...`，实测即可成功合成。

    实测对照（本机，其他条件完全相同）：
        edge-tts 7.2.8 → Sec-MS-GEC-Version=1-143.0.3650.75 → 403
        edge-tts 7.3.1 → Sec-MS-GEC-Version=2-143.0.3650.75 → 合成成功
    """
    text = str(error)
    if '403' in text or 'Invalid response status' in text:
        return (
            "诊断：微软语音服务返回 403（拒绝 WebSocket 握手）。\n"
            "  原因：edge-tts 版本过旧。服务端要求 Sec-MS-GEC-Version 以 '2-' 开头，\n"
            "  7.2.x 及更早发的是 '1-'，因此被一律拒绝（不是网络、代理或密钥问题）。\n"
            "  解决：升级到 edge-tts 7.3.1 或更高（pip install -U edge-tts）；\n"
            "  若使用的是打包好的 exe，需要升级依赖后重新打包。\n"
            "  替代：改用本机语音引擎（pyttsx3，离线可用，需 Windows + FFmpeg）。"
        )
    if '401' in text or 'Unauthorized' in text:
        return "诊断：语音服务拒绝了凭据，可能是 edge-tts 版本过旧，请升级后重试。"
    if 'Connection' in text or 'timeout' in text.lower() or 'Timeout' in text:
        return "诊断：连接语音服务超时，请检查网络、代理或稍后重试。"
    return None


# 最近一次 TTS 失败的诊断信息。
#
# 三个引擎的 generate_audio 只返回 (success, srt)，没有渠道回传失败原因；
# 而打包成窗口程序后 stderr 是空的（`__main__` 里那段重定向只在 pythonw.exe 下生效），
# 于是失败原因彻底丢失 —— 用户只看到"生成失败: 章节名"，弹窗却让他"查看日志了解详情"。
# 这里留一份给调用方读取，用于写进界面日志与失败记录。
LAST_TTS_FAILURE = None


def _record_tts_failure(error):
    """记录 TTS 失败原因：写进应用日志，并存一份给调用方展示

    Returns:
        str: 可直接展示的失败原因
    """
    global LAST_TTS_FAILURE
    hint = _describe_edge_tts_failure(error)
    detail = hint or str(error)
    LAST_TTS_FAILURE = detail
    # 窗口模式下 stderr 什么都收不到，必须走日志文件（logging 在本文件里一直是局部导入）
    try:
        import logging
        logging.getLogger('epubReader').warning(
            "TTS 失败: %s", ' '.join(detail.split()))
    except Exception:
        pass
    print(detail, file=sys.stderr)
    return detail


class EdgeTTSEngine(TTSEngine):
    """Microsoft Edge TTS引擎"""

    # 内置静态列表：标记存在即表示无需后台枚举
    STATIC_VOICES = [
        ("zh-CN-YunjianNeural", "云健 (男声)"),
        ("zh-CN-XiaoxiaoNeural", "晓晓 (女声)"),
        ("zh-CN-YunxiNeural", "云希 (男童声)"),
        ("zh-CN-XiaoyiNeural", "晓伊 (女声)"),
        ("zh-CN-YunyangNeural", "云扬 (男声)"),
        ("zh-CN-liaoning-XiaobeiNeural", "晓北 (女声,辽宁口音)"),
        ("en-US-BrianMultilingualNeural", "Brian (英文,多语言)"),
        ("en-US-AndrewMultilingualNeural", "Andrew (英文,多语言)"),
        ("en-US-EmmaMultilingualNeural", "Emma (英文,多语言)"),
    ]

    @staticmethod
    async def generate_audio(text, output_audio, voice, **kwargs):
        """Edge-TTS生成音频（超长文本自动分块合成后拼接）

        额外支持的 kwargs:
            max_chars: 单个文本块的最大字符数（默认 AppConfig.MAX_TTS_CHARS）
            rate / pitch: 直接透传给 edge_tts.Communicate
            progress_callback: callback(done_blocks, total_blocks)

        Returns:
            tuple: (success: bool, srt_content: str or None)
        """
        try:
            output_dir = os.path.dirname(output_audio)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            if not text or not text.strip():
                print("Edge-TTS: 文本为空，跳过", file=sys.stderr)
                return False, None

            chunks = chunk_text(text, kwargs.get('max_chars')) or [text]
            progress_callback = kwargs.get('progress_callback')

            task_temp_dir = tempfile.mkdtemp(prefix='epubReader_tts_')
            part_files = []
            srt_blocks = []
            index_base = 0
            offset_ms = 0

            try:
                # edge-tts 7.x 默认 boundary='SentenceBoundary'，而旧代码只认 WordBoundary，
                # 结果是字幕永远为空、LRC 退化成“按字数估算时间”。这里显式请求 WordBoundary，
                # 并同时兼容两种边界事件，版本变化时不会再次静默失效。
                boundary_mode = kwargs.get('boundary', 'WordBoundary')
                for position, chunk in enumerate(chunks):
                    part_path = os.path.join(task_temp_dir, f"{position:04d}.part.mp3")
                    communicate = edge_tts.Communicate(
                        chunk, voice,
                        rate=kwargs.get('rate', '+0%'),
                        pitch=kwargs.get('pitch', '+0Hz'),
                        boundary=boundary_mode,
                    )
                    submaker = edge_tts.SubMaker()
                    audio_bytes = 0
                    boundary_events = 0
                    with open(part_path, "wb") as audio_file:
                        async for piece in communicate.stream():
                            piece_type = piece["type"]
                            if piece_type == "audio":
                                audio_file.write(piece["data"])
                                audio_bytes += len(piece["data"])
                            elif piece_type in ("WordBoundary", "SentenceBoundary"):
                                boundary_events += 1
                                submaker.feed(piece)

                    # 单块为空说明该块被服务端拒绝，必须报错而不是产出静音
                    if audio_bytes == 0:
                        raise RuntimeError(
                            f"第 {position + 1}/{len(chunks)} 块未产生音频（{len(chunk)} 字）")
                    if boundary_events == 0:
                        print(f"警告: 第 {position + 1} 块未收到任何字幕边界事件，"
                              f"该块字幕将退化为按字数估算", file=sys.stderr)

                    shifted_srt, index_base = shift_srt(
                        submaker.get_srt(), offset_ms, index_base)
                    if shifted_srt:
                        srt_blocks.append(shifted_srt)

                    # 用该块的真实时长推进时间轴偏移，保证字幕与音频严格对齐。
                    # 不依赖 mutagen：缺失时 MP3() 未定义，原实现会退化成
                    # len(chunk)/0.3 秒的估算（≈3.3 字/秒，与实际差 24 倍），字幕整体错位。
                    duration_ms = get_audio_duration_ms(part_path)
                    if duration_ms <= 0:
                        # 兜底：按中文约 4.5 字/秒估算
                        duration_ms = int(len(chunk) / 4.5 * 1000)
                        print(f"警告: 无法读取第 {position + 1} 块时长，"
                              f"使用估算 {duration_ms}ms", file=sys.stderr)
                    offset_ms += duration_ms

                    part_files.append(part_path)
                    if progress_callback:
                        try:
                            progress_callback(position + 1, len(chunks))
                        except Exception:
                            pass

                # 按顺序拼接为单一 MP3（写完后立即关闭，便于失败时清理临时目录）
                merged_buffer = open(output_audio, "wb")
                try:
                    for part_path in part_files:
                        with open(part_path, "rb") as source:
                            shutil.copyfileobj(source, merged_buffer)
                finally:
                    merged_buffer.close()

                srt_content = '\n'.join(srt_blocks) if srt_blocks else ''
                return True, srt_content

            finally:
                # 失败时清理半成品，避免留下损坏的 MP3 被后续流程误用
                if not os.path.exists(output_audio):
                    try:
                        if os.path.exists(output_audio):
                            os.remove(output_audio)
                    except OSError:
                        pass
                shutil.rmtree(task_temp_dir, ignore_errors=True)

        except Exception as e:
            print(f"Edge-TTS生成失败: {e}", file=sys.stderr)
            traceback.print_exc()
            # 记进应用日志并留一份给调用方展示（窗口模式下 stderr 是空的）
            _record_tts_failure(e)
            return False, None
    
    @staticmethod
    def get_available_voices():
        """获取Edge-TTS可用语音"""
        return list(EdgeTTSEngine.STATIC_VOICES)


class GoogleTTSEngine(TTSEngine):
    """Google TTS引擎 (gTTS)"""

    STATIC_VOICES = [
        ("zh-CN", "中文 (简体)"),
        ("zh-TW", "中文 (繁体)"),
        ("en", "English"),
        ("ja", "日本語"),
        ("ko", "한국어"),
        ("fr", "Français"),
        ("de", "Deutsch"),
        ("es", "Español"),
    ]
    
    @staticmethod
    async def generate_audio(text, output_audio, voice, **kwargs):
        """gTTS生成音频"""
        try:
            from gtts import gTTS
        except ImportError as e:
            error_msg = "gTTS未安装，请运行: pip install gtts"
            print(error_msg, file=sys.stderr)
            raise ImportError(error_msg) from e
        
        try:
            # gTTS使用语言代码，不是语音名
            # voice参数格式: "zh-CN" 或 "en"
            lang = voice if voice else "zh-CN"
            
            os.makedirs(os.path.dirname(output_audio), exist_ok=True)
            
            # gTTS是同步的，在异步环境中运行
            def _generate_with_gtts():
                """生成gTTS音频，强制禁用代理"""
                import os as os_module
                from unittest.mock import patch
                
                # 保存原始代理设置
                saved_proxies = {}
                proxy_keys = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 
                             'NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy']
                for key in proxy_keys:
                    if key in os_module.environ:
                        saved_proxies[key] = os_module.environ[key]
                
                try:
                    # 清除所有代理设置
                    for key in proxy_keys:
                        os_module.environ.pop(key, None)
                    
                    if requests:
                        # 使用 monkey patch 强制禁用 requests 的代理
                        original_merge_environment_settings = requests.Session.merge_environment_settings
                        
                        def no_proxy_merge_environment_settings(self, url, proxies, stream, verify, cert):
                            # 调用原始方法，但强制 proxies 为空
                            return original_merge_environment_settings(self, url, {}, stream, verify, cert)
                        
                        requests.Session.merge_environment_settings = no_proxy_merge_environment_settings
                        
                        try:
                            # 创建gTTS对象并生成音频
                            tts = gTTS(text=text, lang=lang, slow=False)
                            tts.save(output_audio)
                        finally:
                            # 恢复原始方法
                            requests.Session.merge_environment_settings = original_merge_environment_settings
                    else:
                        # 创建gTTS对象并生成音频
                        tts = gTTS(text=text, lang=lang, slow=False)
                        tts.save(output_audio)
                    
                finally:
                    # 恢复代理设置
                    for key, value in saved_proxies.items():
                        os_module.environ[key] = value
            
            await asyncio.get_event_loop().run_in_executor(
                None,
                _generate_with_gtts
            )
            
            return True, None  # gTTS不提供字幕
        except Exception as e:
            error_str = str(e)
            print(f"gTTS生成失败: {error_str}", file=sys.stderr)
            
            # 检查是否是网络连接问题
            if any(keyword in error_str for keyword in [
                "Failed to connect", "ConnectionError", "TimeoutError", 
                "10060", "Max retries exceeded", "NewConnectionError", "ProxyError"
            ]):
                # 检查是否是代理问题
                if "ProxyError" in error_str or "Cannot connect to proxy" in error_str:
                    raise Exception(
                        "gTTS代理连接失败\n"
                        "原因：系统配置了代理但代理服务器不可用\n\n"
                        "建议解决方案：\n"
                        "1. 切换为 edge-tts（推荐，微软服务，国内可用）\n"
                        "2. 关闭系统代理设置后再试\n"
                        "3. 或切换为 pyttsx3（离线使用，需要FFmpeg）"
                    )
                else:
                    raise Exception(
                        "gTTS无法连接到 Google 服务\n"
                        "原因：在中国大陆无法直接访问 Google 服务\n\n"
                        "建议解决方案：\n"
                        "1. 切换为 edge-tts（推荐，微软服务，国内可用）\n"
                        "2. 切换为 pyttsx3（离线使用，需要FFmpeg）\n"
                        "3. 或启用VPN/代理后再使用 gTTS"
                    )
            raise
    
    @staticmethod
    def get_available_voices():
        """获取gTTS可用语言"""
        return list(GoogleTTSEngine.STATIC_VOICES)


class Pyttsx3Engine(TTSEngine):
    """离线TTS引擎 (pyttsx3)。注意：需要FFmpeg转换WAV为MP3"""

    # 未安装/未成功枚举前的占位列表（枚举在后台线程进行，避免卡住界面）
    STATIC_VOICES = [("default", "系统默认")]
    
    @staticmethod
    async def generate_audio(text, output_audio, voice, **kwargs):
        """pyttsx3生成音频"""
        try:
            import pyttsx3
        except ImportError as e:
            error_msg = "pyttsx3未安装，请运行: pip install pyttsx3"
            print(error_msg, file=sys.stderr)
            raise ImportError(error_msg) from e
        
        try:
            os.makedirs(os.path.dirname(output_audio), exist_ok=True)
            
            # 获取FFmpeg路径
            ffmpeg_path = kwargs.get('ffmpeg_path')
            if not ffmpeg_path:
                # 尝试检测系统中的FFmpeg
                is_installed, detected_path = check_ffmpeg_installed()
                if is_installed:
                    ffmpeg_path = detected_path
                else:
                    raise Exception(
                        "pyttsx3需要FFmpeg才能转换为MP3格式\n"
                        "请在[设置与自定义]标签页安装FFmpeg\n"
                        "或切换使用 edge-tts / gTTS 引擎"
                    )
            
            # pyttsx3是同步的
            def _generate():
                engine = pyttsx3.init()
                
                # 设置语音（如果指定）
                if voice and voice != "default":
                    voices = engine.getProperty('voices')
                    for v in voices:
                        if voice in v.id or voice in v.name:
                            engine.setProperty('voice', v.id)
                            break
                
                # 设置语速
                rate = kwargs.get('rate', 150)
                engine.setProperty('rate', rate)
                
                # 保存为WAV，然后转换为MP3
                temp_wav = output_audio.replace('.mp3', '.wav')
                engine.save_to_file(text, temp_wav)
                engine.runAndWait()
                
                # 转换WAV为MP3（需要ffmpeg）
                if os.path.exists(temp_wav):
                    import subprocess
                    try:
                        result = subprocess.run([
                            ffmpeg_path, '-i', temp_wav, '-codec:a', 'libmp3lame',
                            '-b:a', '192k', '-y', output_audio
                        ], check=True, capture_output=True, timeout=30)
                        os.remove(temp_wav)
                        return True
                    except subprocess.CalledProcessError as e:
                        # FFmpeg执行失败
                        error_output = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ''
                        raise Exception(f"FFmpeg转换失败: {error_output}")
                    except subprocess.TimeoutExpired:
                        raise Exception("FFmpeg转换超时")
                else:
                    raise Exception("pyttsx3生成WAV文件失败")
            
            result = await asyncio.get_event_loop().run_in_executor(None, _generate)
            return result, None  # pyttsx3不提供字幕
        except FileNotFoundError as e:
            # FFmpeg未找到
            error_msg = (
                "pyttsx3需要FFmpeg才能转换为MP3格式\n"
                "请在[设置与自定义]标签页安装FFmpeg\n"
                "或切换使用 edge-tts / gTTS 引擎"
            )
            print(f"pyttsx3错误: {error_msg}", file=sys.stderr)
            raise Exception(error_msg) from e
        except Exception as e:
            print(f"pyttsx3生成失败: {e}", file=sys.stderr)
            raise
    
    @staticmethod
    def get_available_voices():
        """获取pyttsx3可用语音"""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty('voices')
            return [("default", "系统默认")] + [(v.id, v.name) for v in voices]
        except:
            return [("default", "系统默认")]


# TTS引擎注册表
TTS_ENGINES = {
    "edge-tts": EdgeTTSEngine,
    "gtts": GoogleTTSEngine,
    "pyttsx3": Pyttsx3Engine,
}

# FFmpeg/FFprobe 可执行文件候选名（按平台）
if sys.platform == 'win32':
    FFMPEG_EXE_NAMES = ('ffmpeg.exe', 'ffmpeg.bat', 'ffmpeg.cmd', 'ffmpeg')
    FFPROBE_EXE_NAMES = ('ffprobe.exe', 'ffprobe.bat', 'ffprobe')
else:
    FFMPEG_EXE_NAMES = ('ffmpeg',)
    FFPROBE_EXE_NAMES = ('ffprobe',)


def get_tts_engine(engine_name):
    """获取TTS引擎实例"""
    return TTS_ENGINES.get(engine_name, EdgeTTSEngine)


def safe_extract_zip(zip_path, target_dir):
    """安全解压 ZIP：拒绝路径穿越（Zip Slip），返回是否成功

    Args:
        zip_path: ZIP 文件路径
        target_dir: 目标目录（会被创建）

    Returns:
        tuple: (success: bool, error_msg: str)
    """
    target_root = os.path.realpath(target_dir)
    os.makedirs(target_root, exist_ok=True)
    try:
        with zipfile_lib.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                member_path = os.path.realpath(
                    os.path.join(target_root, member.filename))
                if member_path != target_root and not member_path.startswith(
                        target_root + os.sep):
                    return False, f"压缩包包含非法路径，已拒绝解压: {member.filename}"
            zip_ref.extractall(target_root)
    except zipfile_lib.BadZipFile:
        return False, "无效的ZIP文件"
    except Exception as e:
        return False, f"解压失败: {e}"
    return True, None


def locate_ffmpeg_dir(extracted_root, root_dir=None):
    """在解压结果中定位包含 ffmpeg 可执行文件的目录（bin 目录）

    Returns:
        str: 目录路径；未找到返回 None
    """
    candidates = []
    if root_dir:
        candidates.append(os.path.join(extracted_root, root_dir, 'bin'))
    candidates.append(os.path.join(extracted_root, 'bin'))
    for candidate in candidates:
        for exe in FFMPEG_EXE_NAMES:
            if os.path.exists(os.path.join(candidate, exe)):
                return candidate
    return None


def verify_ffmpeg(bin_dir, timeout=10):
    """验证指定 bin 目录下的 ffmpeg 可执行文件可用

    Returns:
        str: 可执行文件路径；不可用返回 None
    """
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    for exe in FFMPEG_EXE_NAMES:
        candidate = os.path.join(bin_dir, exe)
        if not os.path.exists(candidate):
            continue
        try:
            result = subprocess.run([candidate, '-version'],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    timeout=timeout,
                                    creationflags=creation_flags)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


def install_bin_atomically(source_bin, target_bin, ffmpeg_exe_name='ffmpeg.exe'):
    """先验证新 bin 可用，再原子替换旧 bin（避免删掉旧版本却没装上新的）

    Returns:
        tuple: (success: bool, error_msg: str)
    """
    if not verify_ffmpeg(source_bin):
        return False, "解压出的 FFmpeg 无法运行，已保留原有安装"

    backup_bin = target_bin + '.old'
    if os.path.exists(backup_bin):
        shutil.rmtree(backup_bin, ignore_errors=True)
    moved_old = False
    if os.path.exists(target_bin):
        try:
            shutil.move(target_bin, backup_bin)
            moved_old = True
        except Exception as e:
            return False, f"无法替换现有 FFmpeg 目录（可能正在使用）: {e}"
    try:
        shutil.move(source_bin, target_bin)
    except Exception as e:
        if moved_old and not os.path.exists(target_bin):
            try:
                shutil.move(backup_bin, target_bin)  # 回滚
            except Exception:
                pass
        return False, f"安装 FFmpeg 失败: {e}"

    if not verify_ffmpeg(target_bin):
        return False, "安装后验证失败，已保留原有安装以便回滚"
    if moved_old:
        shutil.rmtree(backup_bin, ignore_errors=True)
    return True, None


def probe_chapter_count(m4b_path, ffmpeg_path=None):
    """用 ffprobe 统计 M4B 中的章节数，用于验证章节信息是否真的写入

    Returns:
        int: 章节数；无法探测时返回 -1（调用方不应据此判定失败）
    """
    candidates = []
    if ffmpeg_path and os.path.dirname(ffmpeg_path):
        base = os.path.dirname(ffmpeg_path)
        for probe_name in FFPROBE_EXE_NAMES:
            candidates.append(os.path.join(base, probe_name))
    candidates.extend(FFPROBE_EXE_NAMES)

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    for probe in candidates:
        try:
            result = subprocess.run(
                [probe, '-v', 'error', '-show_chapters', '-of', 'json', m4b_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, creationflags=creation_flags)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout.decode('utf-8', errors='ignore') or '{}')
        except ValueError:
            continue
        return len(payload.get('chapters') or [])
    return -1


async def process_text_to_mp3(text, output_audio, output_lrc, voice, chars_per_line=DEFAULT_CHARS_PER_LINE,
                              max_retries=MAX_RETRIES, tts_engine="edge-tts", **kwargs):
    """根据文本内容生成MP3和LRC文件"""
    # 获取TTS引擎
    engine = get_tts_engine(tts_engine)
    
    for attempt in range(max_retries):
        try:
            if not text or not text.strip():
                print(f"警告：文本为空，无法生成音频。", file=sys.stderr)
                return False

            # 使用指定的TTS引擎生成音频
            success, srt_content = await engine.generate_audio(text, output_audio, voice, **kwargs)
            
            if not success:
                if attempt < max_retries - 1:
                    print(f"生成音频失败 (第 {attempt + 1}/{max_retries} 次尝试)", file=sys.stderr)
                    print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                else:
                    print(f"生成音频失败，已达到最大重试次数 ({max_retries})", file=sys.stderr)
                    return False
            
            # 生成LRC文件
            if srt_content and srt_content.strip():
                # 有SRT字幕数据，转换为LRC
                lrc_content = convert_srt_to_lrc(srt_content, chars_per_line)
                
                # 再次检查转换后的LRC是否为空
                if not lrc_content or not lrc_content.strip():
                    # 使用降级方案
                    lrc_content = generate_simple_lrc(text, chars_per_line)
            else:
                # 没有SRT数据，使用降级方案
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
                _record_tts_failure(e)      # 重试都失败了，这时才记原因
        except IOError as e:
            if attempt < max_retries - 1:
                print(f"文件IO错误 (第 {attempt + 1}/{max_retries} 次尝试)：{e}", file=sys.stderr)
                print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"文件IO错误，已达到最大重试次数 ({max_retries})：{e}", file=sys.stderr)
                traceback.print_exc()
                _record_tts_failure(e)
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"生成音频过程中发生错误 (第 {attempt + 1}/{max_retries} 次尝试)：{e}", file=sys.stderr)
                print(f"等待 {RETRY_DELAY_SECONDS} 秒后自动重试...", file=sys.stderr)
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"生成音频过程中发生错误，已达到最大重试次数 ({max_retries})：{e}", file=sys.stderr)
                traceback.print_exc()
                _record_tts_failure(e)
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
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

    # 首先检查随包附带的 ffmpeg（打包后无需下载即可导出 M4B）
    bundled = AppConfig.bundled_ffmpeg_dir()
    if bundled:
        for name in ('ffmpeg.exe', 'ffmpeg'):
            candidate = os.path.join(bundled, name)
            if os.path.exists(candidate):
                try:
                    result = subprocess.run([candidate, '-version'],
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE,
                                            timeout=5,
                                            creationflags=creation_flags)
                    if result.returncode == 0:
                        return True, candidate
                except (subprocess.SubprocessError, OSError):
                    pass

    # 其次检查系统PATH中的ffmpeg
    try:
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
        
        # 解压文件（安全解压 + 先验证再原子替换）
        with zipfile_lib.ZipFile(zip_file_path, 'r') as zip_ref:
            # 获取压缩包内的根目录名
            namelist = zip_ref.namelist()
            root_dir = namelist[0].split('/')[0] if '/' in namelist[0] else namelist[0].split('\\')[0]

        temp_extract = os.path.join(task_temp_dir, 'ffmpeg_extract')
        if os.path.exists(temp_extract):
            shutil.rmtree(temp_extract)

        extract_ok, extract_error = safe_extract_zip(zip_file_path, temp_extract)
        if not extract_ok:
            return False, None, extract_error

        if progress_callback:
            progress_callback(60, 100, "正在安装FFmpeg...")

        source_bin = locate_ffmpeg_dir(temp_extract, root_dir)
        if not source_bin:
            return False, None, "ZIP文件中未找到可用的FFmpeg，请确保是完整包"

        target_bin = os.path.join(FFMPEG_DIR, 'bin')
        install_ok, install_error = install_bin_atomically(source_bin, target_bin)
        if not install_ok:
            return False, None, install_error

        if progress_callback:
            progress_callback(90, 100, "正在验证安装...")

        ffmpeg_exe = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe')

        # 验证安装
        if not os.path.exists(ffmpeg_exe):
            return False, None, "FFmpeg.exe未找到，安装失败"

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
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
            
            # 使用 requests 库下载（更可靠）
            if requests:
                # 设置请求头
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                
                # 设置代理（仅HTTP，避免 HTTPS 代理警告）
                proxies = None
                # 尝试从环境变量获取代理
                http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
                if http_proxy:
                    proxies = {'http': http_proxy, 'https': http_proxy}
                
                try:
                    # 开始下载（保持 TLS 证书校验：下载后会被安装并执行，不能降级）
                    response = requests.get(download_url, headers=headers, proxies=proxies, stream=True, timeout=DOWNLOAD_TIMEOUT)
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
            else:
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
                    
                    # 只对本次请求设置超时，不用 socket.setdefaulttimeout
                    # （后者是进程级全局副作用，会污染其他所有网络调用且不恢复）
                    opener = urllib.request.build_opener()
                    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')]
                    with opener.open(download_url, timeout=DOWNLOAD_TIMEOUT) as response, \
                            open(zip_path, 'wb') as out_file:
                        total_size = int(response.headers.get('content-length') or 0)
                        downloaded = 0
                        while True:
                            if cancel_check and cancel_check():
                                return False, None, "用户取消安装"
                            block = response.read(64 * 1024)
                            if not block:
                                break
                            out_file.write(block)
                            downloaded += len(block)
                            if progress_callback and total_size > 0:
                                percent = min(int(downloaded / total_size * 100), 100)
                                progress_callback(
                                    downloaded,
                                    total_size,
                                    f"下载中: {percent}% "
                                    f"({downloaded / 1048576:.1f}/"
                                    f"{total_size / 1048576:.1f} MB)"
                                )
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
        
        # 解压文件（安全解压 + 先验证再原子替换）
        with zipfile_lib.ZipFile(zip_path, 'r') as zip_ref:
            # 获取压缩包内的根目录名
            namelist = zip_ref.namelist()
            root_dir = namelist[0].split('/')[0] if '/' in namelist[0] else namelist[0].split('\\')[0]

        # 解压到临时目录
        temp_extract = os.path.join(task_temp_dir, 'ffmpeg_extract')
        extract_ok, extract_error = safe_extract_zip(zip_path, temp_extract)
        if not extract_ok:
            return False, None, extract_error

        # 定位并原子替换 bin 目录
        source_bin = locate_ffmpeg_dir(temp_extract, root_dir)
        if not source_bin:
            return False, None, "下载包中未找到可用的FFmpeg，请重试或手动安装"

        target_bin = os.path.join(FFMPEG_DIR, 'bin')
        install_ok, install_error = install_bin_atomically(source_bin, target_bin)
        if not install_ok:
            return False, None, install_error

        # 清理临时文件
        shutil.rmtree(temp_extract, ignore_errors=True)
        
        os.remove(zip_path)
        
        if progress_callback:
            progress_callback(100, 100, "安装完成")
        
        ffmpeg_exe = os.path.join(FFMPEG_DIR, 'bin', 'ffmpeg.exe')
        
        # 验证安装
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
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
    
    except URLError as e:
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


def merge_mp3_to_m4b(mp3_files, output_m4b, metadata, cover_data=None, ffmpeg_path=None,
                     chapter_titles=None, cancel_check=None):
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
            if cancel_check and cancel_check():
                return False, "用户取消合并"
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
            
            # 方案2: FFmpeg 失败时用 mutagen
            if duration is None or duration <= 0:
                try:
                    audio = MP3(mp3_file)
                    if audio.info and audio.info.length > 0:
                        duration = audio.info.length
                        print(f"Mutagen获取 {os.path.basename(mp3_file)} 时长: {duration:.2f}秒")
                except Exception as e:
                    print(f"Mutagen获取 {os.path.basename(mp3_file)} 时长失败: {e}")

            # 方案3: 都用不了时，用自带 MPEG 帧解析（不依赖任何第三方库）。
            # 直接兜底成 1 秒会让章节表整体错位，这里必须尽量拿到真实时长。
            if duration is None or duration <= 0:
                parsed_ms = mp3_duration_ms(mp3_file)
                if parsed_ms > 0:
                    duration = parsed_ms / 1000.0
                    print(f"MPEG解析获取 {os.path.basename(mp3_file)} 时长: {duration:.2f}秒")

            # 最终兜底: 仍然拿不到才用最小时长，并明确告警
            if duration is None or duration <= 0:
                duration = 1.0
                print(f"警告: 无法获取 {os.path.basename(mp3_file)} 的时长，"
                      f"章节表将使用最小时长1秒（可能导致章节定位不准）",
                      file=sys.stderr)

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
        if cancel_check and cancel_check():
            return False, "用户取消合并"
        print("正在合并音频文件并添加章节信息...")
        concat_cmd = [
            ffmpeg_path,
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-i", metadata_file,  # 添加章节元数据
            "-map", "0:a",
            "-map_metadata", "1",   # 使用metadata_file的全局元数据
            "-map_chapters", "1",   # 关键：显式要求写入章节表，否则 0:t 可能不存在
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
        
        result_err = result.stderr.decode('utf-8', errors='ignore')
        if result.returncode != 0:
            # 回退：部分 FFmpeg 构建对 -map_chapters 支持不佳，去掉它重试一次
            print("章节映射失败，去掉 -map_chapters 重试...", file=sys.stderr)
            fallback_cmd = [c for c in concat_cmd if c not in ("-map_chapters", "1")]
            result = subprocess.run(fallback_cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    creationflags=creation_flags)
            result_err = result.stderr.decode('utf-8', errors='ignore')
            if result.returncode != 0:
                error_msg = (f"FFmpeg合并音频失败\n错误代码: {result.returncode}\n"
                             f"详细信息: {result_err[-500:]}")
                print(f"FFmpeg合并失败: {result_err}", file=sys.stderr)
                return False, error_msg
        
        # 第二步：添加封面，转换为M4B
        print("正在添加封面...")
        
        metadata_cmd = [
            ffmpeg_path,
            "-i", temp_m4a,
        ]
        
        # 如果有封面，添加封面输入（扩展名按文件头判断，避免 PNG 被当作 JPEG）
        cover_file = None
        if cover_data:
            cover_ext = '.jpg'
            if cover_data.startswith(b'\x89PNG'):
                cover_ext = '.png'
            elif cover_data.startswith(b'GIF8'):
                cover_ext = '.gif'
            cover_file = os.path.join(task_temp_dir, "m4b_cover" + cover_ext)
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
                "-map", "0:t?",   # 映射章节信息（第二步必须显式保留）
                "-map", "1:v",
                "-disposition:v:0", "attached_pic",  # 设置封面为附加图片
            ])
        else:
            metadata_cmd.extend([
                "-map", "0:a",
                "-map", "0:t?",   # 映射章节信息
            ])
        
        # 添加元数据（保持原有章节信息）
        metadata_cmd.extend([
            "-map_chapters", "0",  # 关键：不要丢弃第一步写入的章节表
            "-metadata", "media_type=2",  # 2表示有声书
            "-f", "mp4",
            "-y",
            output_m4b
        ])
        
        if cancel_check and cancel_check():
            return False, "用户取消合并"
        result = subprocess.run(metadata_cmd,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              creationflags=creation_flags)
        
        if result.returncode != 0:
            error_output = result.stderr.decode('utf-8', errors='ignore')
            error_msg = f"FFmpeg添加封面/元数据失败\n错误代码: {result.returncode}\n详细信息: {error_output[-500:]}"
            print(f"FFmpeg添加元数据失败: {error_output}", file=sys.stderr)
            return False, error_msg
        
        # 第三步：验证章节是否真的写进去了（章节表丢失是静默失败）
        expected_chapters = len(mp3_files)
        actual_chapters = probe_chapter_count(output_m4b, ffmpeg_path)
        if actual_chapters >= 0 and actual_chapters != expected_chapters:
            warning = (f"章节信息写入异常：期望 {expected_chapters} 章，"
                       f"实际 {actual_chapters} 章")
            print(f"警告: {warning}", file=sys.stderr)
            if actual_chapters == 0:
                return False, f"{warning}。请更换 FFmpeg 构建后重试。"
        
        print(f"M4B文件生成成功: {output_m4b}（章节数 {actual_chapters}/"
              f"{expected_chapters}）")
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


# ===== 界面主题 =====
class UITheme:
    """集中管理界面配色与字体，避免样式散落在各处"""

    # 中性色
    BG = '#f4f6f9'          # 窗口底色
    SURFACE = '#ffffff'     # 卡片/输入框
    BORDER = '#d8dee7'
    HOVER = '#eef2f7'

    # 文本
    TEXT = '#1f2933'
    MUTED = '#6b7684'

    # 主题色
    ACCENT = '#2f6feb'
    ACCENT_DARK = '#2559c4'
    ACCENT_SOFT = '#e8f0fe'
    OK = '#137a4c'
    WARN = '#a15c00'
    ERROR = '#b42318'

    # 阅读区
    READ_BG = '#f7f9fc'

    FONT_FAMILY = 'Microsoft YaHei UI'
    BASE_FAMILY = 'Microsoft YaHei'


def apply_theme(root):
    """应用 ttk 主题与全局样式

    Windows 原生 'vista' 主题不支持自定义背景/边框，因此统一改用 'clam' 后手工调色。
    """
    theme = UITheme
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass

    style.configure('.',
                    background=theme.BG,
                    foreground=theme.TEXT,
                    font=(theme.FONT_FAMILY, 10),
                    focuscolor=theme.ACCENT)
    style.configure('TFrame', background=theme.BG)
    style.configure('Card.TFrame', background=theme.SURFACE,
                    relief='solid', borderwidth=1)
    style.configure('Toolbar.TFrame', background=theme.SURFACE,
                    relief='solid', borderwidth=1)
    style.configure('Header.TFrame', background=theme.SURFACE)
    style.configure('Status.TFrame', background=theme.SURFACE,
                    relief='solid', borderwidth=1)

    style.configure('TLabel', background=theme.BG, foreground=theme.TEXT)
    style.configure('Surface.TLabel', background=theme.SURFACE, foreground=theme.TEXT)
    style.configure('Muted.TLabel', background=theme.BG, foreground=theme.MUTED,
                    font=(theme.FONT_FAMILY, 9))
    style.configure('SurfaceMuted.TLabel', background=theme.SURFACE,
                    foreground=theme.MUTED, font=(theme.FONT_FAMILY, 9))
    style.configure('HeaderTitle.TLabel', background=theme.SURFACE,
                    foreground=theme.TEXT, font=(theme.FONT_FAMILY, 11, 'bold'))
    style.configure('Title.TLabel', background=theme.BG, foreground=theme.TEXT,
                    font=(theme.FONT_FAMILY, 12, 'bold'))
    style.configure('InfoKey.TLabel', background=theme.SURFACE,
                    foreground=theme.MUTED, font=(theme.FONT_FAMILY, 9))
    style.configure('InfoValue.TLabel', background=theme.SURFACE,
                    foreground=theme.TEXT, font=(theme.FONT_FAMILY, 10))
    style.configure('Status.TLabel', background=theme.SURFACE,
                    foreground=theme.MUTED, font=(theme.FONT_FAMILY, 9))

    # 卡片式容器
    style.configure('TLabelframe', background=theme.BG,
                    bordercolor=theme.BORDER, relief='solid', borderwidth=1)
    style.configure('TLabelframe.Label', background=theme.BG,
                    foreground=theme.TEXT, font=(theme.FONT_FAMILY, 10, 'bold'))
    style.configure('Card.TLabelframe', background=theme.SURFACE,
                    bordercolor=theme.BORDER, relief='solid', borderwidth=1)
    style.configure('Card.TLabelframe.Label', background=theme.SURFACE,
                    foreground=theme.MUTED, font=(theme.FONT_FAMILY, 9, 'bold'))

    # 按钮
    style.configure('TButton', background=theme.SURFACE, foreground=theme.TEXT,
                    bordercolor=theme.BORDER, focuscolor=theme.SURFACE,
                    relief='flat', padding=(12, 6), font=(theme.FONT_FAMILY, 10))
    style.map('TButton',
              background=[('pressed', theme.HOVER), ('active', theme.HOVER),
                          ('disabled', '#f2f4f7')],
              foreground=[('disabled', '#a3acb9')],
              bordercolor=[('active', theme.ACCENT)])

    style.configure('Accent.TButton', background=theme.ACCENT,
                    foreground='#ffffff', bordercolor=theme.ACCENT,
                    relief='flat', padding=(14, 7),
                    font=(theme.FONT_FAMILY, 10, 'bold'))
    style.map('Accent.TButton',
              background=[('pressed', theme.ACCENT_DARK), ('active', theme.ACCENT_DARK),
                          ('disabled', '#b9c8e8')],
              foreground=[('disabled', '#f0f4ff')],
              bordercolor=[('disabled', '#b9c8e8')])

    style.configure('Ghost.TButton', background=theme.BG, bordercolor=theme.BG,
                    relief='flat', padding=(8, 4),
                    font=(theme.FONT_FAMILY, 9))
    style.map('Ghost.TButton',
              background=[('active', theme.HOVER)],
              foreground=[('active', theme.ACCENT)])

    style.configure('TEntry', fieldbackground=theme.SURFACE,
                    bordercolor=theme.BORDER, lightcolor=theme.BORDER,
                    darkcolor=theme.BORDER, padding=5)
    style.configure('TCombobox', fieldbackground=theme.SURFACE,
                    background=theme.SURFACE, bordercolor=theme.BORDER,
                    arrowcolor=theme.MUTED, padding=4)
    style.map('TCombobox',
              fieldbackground=[('readonly', theme.SURFACE)],
              bordercolor=[('focus', theme.ACCENT), ('hover', theme.ACCENT)])
    style.configure('TSpinbox', fieldbackground=theme.SURFACE,
                    bordercolor=theme.BORDER, arrowcolor=theme.MUTED, padding=4)

    style.configure('TNotebook', background=theme.BG, bordercolor=theme.BG,
                    tabmargins=(0, 4, 0, 0))
    style.configure('TNotebook.Tab', background=theme.BG, foreground=theme.MUTED,
                    padding=(20, 9), font=(theme.FONT_FAMILY, 10))
    style.map('TNotebook.Tab',
              background=[('selected', theme.SURFACE)],
              foreground=[('selected', theme.ACCENT)],
              font=[('selected', (theme.FONT_FAMILY, 10, 'bold'))])

    style.configure('TRadiobutton', background=theme.BG, foreground=theme.TEXT)
    style.map('TRadiobutton', background=[('active', theme.BG)])
    style.configure('TCheckbutton', background=theme.BG, foreground=theme.TEXT)

    style.configure('TProgressbar', background=theme.ACCENT,
                    troughcolor='#e3e8f0', bordercolor=theme.SURFACE,
                    lightcolor=theme.ACCENT, darkcolor=theme.ACCENT,
                    thickness=8)
    style.configure('Vertical.TScrollbar', background=theme.HOVER,
                    troughcolor=theme.BG, bordercolor=theme.BG,
                    arrowcolor=theme.MUTED, relief='flat')
    style.configure('Horizontal.TScrollbar', background=theme.HOVER,
                    troughcolor=theme.BG, bordercolor=theme.BG,
                    arrowcolor=theme.MUTED, relief='flat')

    return style


class EpubToMp3App:
    def __init__(self, root):
        self.root = root
        self.root.title("epubReader · EPUB 有声书制作")
        self.MIN_WINDOW_HEIGHT = 700
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(1180, int(screen_w * 0.86))
        height = min(885, int(screen_h * 0.92))
        self.root.geometry('%dx%d' % (width, height))
        self.root.minsize(min(1000, width), min(self.MIN_WINDOW_HEIGHT, height))
        self.root.configure(background=UITheme.BG)

        # 应用主题（必须在创建控件之前）
        self.style = apply_theme(self.root)

        # 存储解析后的数据
        self.epub_data = None
        self.current_chapter_index = -1  # -1 表示没有章节被选中
        self.processing = False  # 标记是否有转换任务正在进行
        self.processing_lock = threading.Lock()  # 保护 processing 的检查与置位
        self.failed_chapters = []  # 记录失败的章节 [{"index": int, "title": str, "reason": str}]
        self.failed_chapters_lock = threading.Lock()  # 保护失败章节列表
        self.estimated_time_var = None  # 将在create_widgets中初始化
        self.estimated_time_var_epub_tab = None  # 将在create_widgets中初始化
        self.ffmpeg_path = None  # FFmpeg路径
        self.ffmpeg_installed = False  # FFmpeg安装状态

        # 实例级临时目录：不再使用所有实例共享的 TEMP_DIR，
        # 否则两个实例同时运行会互相删除对方的临时文件
        self.session_temp_dir = tempfile.mkdtemp(
            prefix='epubReader_%d_' % os.getpid())
        self._session_temp_counter = 0
        self._session_temp_lock = threading.Lock()
        # M4B 合并的中止信号（"中止合并"按钮会置位）
        self._m4b_cancel_event = threading.Event()
        atexit.register(self.cleanup_session_temp_dir)

        # 设置持久化（损坏/缺失时自动使用默认值）
        self.config_store = ConfigStore()
        self._stopping = False  # 关闭时停止消息泵续期

        # 日志：在应用初始化时就建立，保证无论从哪个入口启动都有日志可查
        self.logger = setup_logging()
        self.logger.info("界面初始化，临时目录: %s", self.session_temp_dir)
        self.write_debug_report()

        # Tk 回调里的异常默认只打到 stderr，窗口模式下等于丢失；
        # 注意 sys.excepthook 管不到这条路径，必须单独接管。
        self.root.report_callback_exception = self._report_callback_exception

        # 创建通信队列（仍然用于GUI更新）
        self.message_queue = queue.Queue()
        
        # *** 创建统一的任务调度器（并发上限 = 工作线程数） ***
        self.task_scheduler = TaskScheduler(max_workers=3)
        self.task_scheduler.start()

        # 字体（阅读区用 10.5pt，界面用 9pt）
        self.base_font = font.Font(family=UITheme.BASE_FAMILY, size=11)
        self.read_font = font.Font(family=UITheme.BASE_FAMILY, size=11)
        self.small_font = font.Font(family=UITheme.FONT_FAMILY, size=9)
        self.ui_font = font.Font(family=UITheme.FONT_FAMILY, size=10)

        # 创建主界面
        self.create_widgets()

        # 恢复上次的设置（缺失/损坏时静默使用默认值）
        self._load_persisted_settings()

        # 检查FFmpeg
        self.root.after(500, self.check_ffmpeg_on_startup)

        # 启动消息处理
        self.process_messages()

    def _report_callback_exception(self, exc_type, exc_value, exc_tb):
        """把 Tk 回调里的异常写进应用日志

        sys.excepthook 只在解释器顶层异常时触发，Tk 回调走的是
        root.report_callback_exception，两回事。窗口模式（打包后）没有 stderr，
        不接管这里的话界面回调出错时用户和日志都看不到任何线索。
        """
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(text, file=sys.stderr)
        try:
            self.logger.error("界面回调异常\n%s", text)
        except Exception:
            pass

    def write_debug_report(self):
        """当设置 EPUBREADER_DEBUG=1 时，写一份运行环境报告（便于排查打包后的问题）

        Windows 上是窗口程序，默认没有控制台，出问题时看不到任何输出；
        这个文件能回答"用的哪个 ffmpeg / 日志写到哪里 / 临时目录在哪"。
        """
        if os.environ.get('EPUBREADER_DEBUG') != '1':
            return
        try:
            lines = [
                'epubReader 调试报告',
                '时间: %s' % datetime.now().isoformat(),
                'frozen(是否打包): %s' % bool(getattr(sys, 'frozen', False)),
                'MEIPASS: %s' % getattr(sys, '_MEIPASS', None),
                '可执行文件: %s' % sys.executable,
                '工作目录: %s' % os.getcwd(),
                'APPDATA: %s' % os.environ.get('APPDATA'),
                '配置目录: %s' % AppConfig.CONFIG_DIR,
                '日志文件: %s' % AppConfig.LOG_PATH,
                '日志存在: %s' % os.path.exists(AppConfig.LOG_PATH),
                '临时目录: %s' % self.session_temp_dir,
                '随包 ffmpeg: %s' % AppConfig.bundled_ffmpeg_dir(),
                '检测到的 ffmpeg: %s' % (self.ffmpeg_path,),
            ]
            logger = self.logger
            if logger is not None:
                lines.append('logger handlers: %s' % [
                    type(h).__name__ for h in logger.handlers])
            target = os.path.join(os.path.expanduser('~'), 'epubReader_debug.txt')
            with io.open(target, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(lines) + '\n')
        except Exception as exc:
            print('写调试报告失败: %s' % exc, file=sys.stderr)

    def _collect_settings(self):
        """收集需要持久化的界面设置"""
        try:
            return {
                'tts_engine': self.tts_engine_var.get(),
                'voice': self.voice_var.get(),
                'chars_per_line': int(self.chars_per_line_var.get()),
                'output_format': self.output_format_var.get(),
                'concurrent_threads': int(self.concurrent_threads_var.get()),
                'output_dir': self.output_dir_var.get(),
                'album': self.album_var.get(),
                'artist': self.artist_var.get(),
            }
        except Exception as e:
            print(f"收集设置失败: {e}", file=sys.stderr)
            return {}

    def _load_persisted_settings(self):
        """把持久化的设置写回界面控件"""
        if self.config_store is None:
            return
        data = self.config_store.data
        try:
            if data.get('tts_engine'):
                self.tts_engine_var.set(data['tts_engine'])
                self.update_voice_list()
            if data.get('voice'):
                self.voice_var.set(data['voice'])
            if data.get('chars_per_line'):
                self.chars_per_line_var.set(int(data['chars_per_line']))
            if data.get('concurrent_threads'):
                self.concurrent_threads_var.set(int(data['concurrent_threads']))
            if data.get('output_format') in ('MP3', 'M4B'):
                self.output_format_var.set(data['output_format'])
            if data.get('output_dir') and os.path.isdir(data['output_dir']):
                self.output_dir_var.set(data['output_dir'])
            if data.get('album'):
                self.album_var.set(data['album'])
            if data.get('artist'):
                self.artist_var.set(data['artist'])
        except Exception as e:
            print(f"恢复设置失败（使用默认值）: {e}", file=sys.stderr)

    def save_settings(self):
        """保存界面设置（退出/开始任务时调用）

        采集控件值这一步要先于写盘、且单独兜住 `tk.TclError`：窗口已经销毁时
        （典型是"初始化还没走完就被关掉"）`StringVar.get()` 会抛 TclError，
        若直接冒出去就整条保存流程失败、配置一点都没落盘。此时退化成
        "把内存里已有的配置写下来"，比丢失全部设置好。
        """
        if self.config_store is None:
            return
        try:
            self.config_store.update(**self._collect_settings())
        except tk.TclError:
            self.logger.warning("读取控件设置失败（窗口可能已销毁），改为落盘已有配置")
        self.config_store.save()

    def try_begin_processing(self):
        """尝试进入“处理中”状态（线程安全，防止并发任务互相覆盖输出文件）

        Returns:
            bool: True 表示获取成功；False 表示已有任务在运行
        """
        with self.processing_lock:
            if self.processing:
                return False
            self.processing = True
            return True

    def end_processing(self):
        """退出“处理中”状态（线程安全）"""
        with self.processing_lock:
            self.processing = False

    def new_temp_path(self, suffix='', prefix='tmp'):
        """在实例级临时目录下分配一个唯一路径（替代已弃用的 mktemp）

        使用 mkstemp 保证原子性，避免 TOCTOU 竞争；文件已创建，调用方可直接使用。
        """
        os.makedirs(self.session_temp_dir, exist_ok=True)
        descriptor, path = tempfile.mkstemp(
            suffix=suffix, prefix=prefix + '_', dir=self.session_temp_dir)
        os.close(descriptor)
        return path

    def temp_subdir(self, prefix):
        """在实例级临时目录下创建子目录（用于多文件任务）"""
        with self._session_temp_lock:
            self._session_temp_counter += 1
            index = self._session_temp_counter
        path = os.path.join(self.session_temp_dir, '%s_%d' % (prefix, index))
        os.makedirs(path, exist_ok=True)
        return path

    def cleanup_session_temp_dir(self):
        """清理本实例的临时目录（容错：文件占用不抛异常）"""
        path = getattr(self, 'session_temp_dir', None)
        if not path or not os.path.isdir(path):
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            print(f"清理临时目录失败（可忽略）: {e}", file=sys.stderr)

    def submit_scheduler_task(self, task):
        """提交任务到调度器，失败时给出可读提示而不是抛栈

        Returns:
            bool: 是否成功提交
        """
        try:
            self.task_scheduler.submit_task(task)
            return True
        except RuntimeError as e:
            messagebox.showerror("无法提交任务", str(e))
            self.update_status(f"任务提交失败: {e}")
            return False

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
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))

        # 创建EPUB阅读与编辑页
        self.epub_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.epub_frame, text="  浏览与编辑  ")

        # 创建设置与自定义
        self.mp3_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.mp3_frame, text="  设置与自定义  ")

        # 底部状态栏
        self.create_status_bar()

        # 初始化EPUB阅读界面
        self.create_epub_reader_ui()

        # 初始化有声书转换界面
        self.create_mp3_converter_ui()

    def _update_chapter_count(self):
        """刷新章节计数（列表头右侧）"""
        label = getattr(self, 'chapter_count_label', None)
        if label is None:
            return
        total = len(self.epub_data['chapters']) if (
            self.epub_data and self.epub_data.get('chapters')) else 0
        label.config(text=('共 %d 章' % total) if total else '未载入')

    def create_status_bar(self):
        """创建底部状态栏"""
        theme = UITheme
        status_frame = ttk.Frame(self.root, style='Status.TFrame',
                                 padding=(14, 6))
        status_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        status_frame.columnconfigure(1, weight=1)
        status_frame.columnconfigure(2, weight=0)

        ttk.Label(status_frame, text="状态", style='SurfaceMuted.TLabel',
                  font=(theme.FONT_FAMILY, 9, 'bold')).grid(
            row=0, column=0, padx=(0, 10))

        self.status_label = ttk.Label(
            status_frame, text="等待操作…", anchor=tk.W,
            style='Status.TLabel')
        self.status_label.grid(row=0, column=1, sticky="ew")

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            status_frame, orient=tk.HORIZONTAL, length=240,
            mode='determinate', variable=self.progress_var)
        self.progress_bar.grid(row=0, column=2, padx=(12, 0))
        self.progress_bar["maximum"] = 100
        self.progress_bar["value"] = 0

    def create_epub_reader_ui(self):
        """创建EPUB阅读与编辑UI"""
        theme = UITheme

        # ---------- 顶部工具栏 ----------
        top_frame = ttk.Frame(self.epub_frame, style='Toolbar.TFrame',
                              padding=(12, 10))
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 6))
        top_frame.columnconfigure(2, weight=1)

        self.select_button = ttk.Button(
            top_frame, text="选择电子书", command=self.select_epub_file,
            style='Accent.TButton')
        self.select_button.grid(row=0, column=0, padx=(0, 10))

        ttk.Label(top_frame, text="文件", style='SurfaceMuted.TLabel').grid(
            row=0, column=1, padx=(0, 6))

        self.filepath_var = tk.StringVar()
        self.filepath_entry = ttk.Entry(
            top_frame, textvariable=self.filepath_var, state='readonly')
        self.filepath_entry.grid(row=0, column=2, sticky='ew', padx=(0, 10))

        self.estimated_time_var_epub_tab = tk.StringVar(value="预计耗时 00:00:00")
        self.estimated_time_label_epub_tab = ttk.Label(
            top_frame, textvariable=self.estimated_time_var_epub_tab,
            style='SurfaceMuted.TLabel')
        self.estimated_time_label_epub_tab.grid(row=0, column=3, padx=(0, 10))

        self.generate_ebook_button_epub_tab = ttk.Button(
            top_frame, text="生成有声书", command=self.start_generate_ebook,
            state="disabled", style='Accent.TButton')
        self.generate_ebook_button_epub_tab.grid(row=0, column=4, padx=(0, 8))

        self.export_all_button = ttk.Button(
            top_frame, text="批量导出 TXT", command=self.export_all_chapters,
            state="disabled")
        self.export_all_button.grid(row=0, column=5)

        # ---------- 主工作区 ----------
        main_pane = ttk.PanedWindow(self.epub_frame, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # ===== 左：封面 + 书籍信息 =====
        left_frame = ttk.Frame(main_pane, width=250)
        left_frame.pack_propagate(False)
        main_pane.add(left_frame, weight=0)

        self.cover_label = tk.Label(
            left_frame, text="图书封面", anchor=tk.CENTER,
            background=theme.ACCENT_SOFT, foreground=theme.MUTED,
            font=(theme.FONT_FAMILY, 10), padx=8, pady=8)
        self.cover_label.pack(fill=tk.X, padx=(0, 10), pady=(0, 8))

        info_card = ttk.Frame(left_frame, style='Card.TFrame', padding=(14, 12))
        info_card.pack(fill=tk.BOTH, expand=True, padx=(0, 10))
        info_card.columnconfigure(1, weight=1)

        ttk.Label(info_card, text="书籍信息", style='Card.TLabelframe.Label').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))

        info_rows = [
            ("书名", 'title_label'),
            ("作者", 'author_label'),
            ("出版社", 'publisher_label'),
            ("出版时间", 'publish_date_label'),
            ("全书字数", 'total_words_label'),
        ]
        for index, (text, attr) in enumerate(info_rows, start=1):
            ttk.Label(info_card, text=text, style='InfoKey.TLabel').grid(
                row=index, column=0, sticky='nw', pady=4, padx=(0, 10))
            widget = ttk.Label(info_card, text="", style='InfoValue.TLabel',
                               wraplength=140, justify=tk.LEFT)
            widget.grid(row=index, column=1, sticky='nw', pady=4)
            setattr(self, attr, widget)

        # ===== 中：章节列表 =====
        middle_frame = ttk.Frame(main_pane, width=280)
        main_pane.add(middle_frame, weight=1)

        list_header = ttk.Frame(middle_frame)
        list_header.pack(fill=tk.X, padx=(0, 10), pady=(0, 6))
        ttk.Label(list_header, text="章节列表",
                  style='Title.TLabel').pack(side=tk.LEFT)
        self.chapter_count_label = ttk.Label(list_header, text="共 0 章",
                                             style='Muted.TLabel')
        self.chapter_count_label.pack(side=tk.RIGHT)

        list_card = ttk.Frame(middle_frame, style='Card.TFrame', padding=1)
        list_card.pack(fill=tk.BOTH, expand=True, padx=(0, 10))

        self.chapter_listbox = tk.Listbox(
            list_card, exportselection=False, activestyle='none',
            font=self.ui_font, background=theme.SURFACE,
            foreground=theme.TEXT, selectbackground=theme.ACCENT,
            selectforeground='#ffffff', highlightthickness=0, bd=0,
            selectborderwidth=0, relief='flat')
        chapter_scrollbar = ttk.Scrollbar(list_card, orient=tk.VERTICAL,
                                          command=self.chapter_listbox.yview)
        self.chapter_listbox.configure(yscrollcommand=chapter_scrollbar.set)
        self.chapter_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                  padx=(6, 0), pady=6)
        chapter_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.chapter_listbox.bind('<ButtonRelease-1>', self.on_chapter_select)
        self.chapter_listbox.bind('<Button-3>', self.on_chapter_right_click)
        self.chapter_listbox.bind(
            '<Alt-Up>',
            lambda e: self.move_chapter_up(self.chapter_listbox.curselection()[0])
            if self.chapter_listbox.curselection() else None)
        self.chapter_listbox.bind(
            '<Alt-Down>',
            lambda e: self.move_chapter_down(self.chapter_listbox.curselection()[0])
            if self.chapter_listbox.curselection() else None)

        # ===== 右：章节内容 =====
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=2)

        content_header = ttk.Frame(right_frame)
        content_header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(content_header, text="章节内容",
                  style='Title.TLabel').pack(side=tk.LEFT)

        self.convert_chapter_button = ttk.Button(
            content_header, text="转有声书", command=self.convert_current_chapter,
            state="disabled", style='Accent.TButton')
        self.convert_chapter_button.pack(side=tk.RIGHT)

        self.chapter_save_button = ttk.Button(
            content_header, text="保存修改", command=self.save_chapter_content,
            state="disabled")
        self.chapter_save_button.pack(side=tk.RIGHT, padx=(0, 8))

        content_card = ttk.Frame(right_frame, style='Card.TFrame', padding=1)
        content_card.pack(fill=tk.BOTH, expand=True)

        self.chapter_text = scrolledtext.ScrolledText(
            content_card, wrap=tk.WORD, font=self.read_font,
            padx=14, pady=12, background=theme.READ_BG,
            foreground=theme.TEXT, relief='flat', bd=0,
            insertbackground=theme.ACCENT, spacing1=2, spacing3=6)
        self.chapter_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._update_chapter_count()
    def create_mp3_converter_ui(self):
        """创建有声书转换UI"""
        # 使用Grid布局管理器
        mp3_main_frame = ttk.Frame(self.mp3_frame, padding=(0, 4))
        mp3_main_frame.pack(fill=tk.BOTH, expand=True)
        mp3_main_frame.columnconfigure(0, weight=1)
        # 行 0-3：配置卡片；行 4：日志抽屉（展开时吃满剩余高度）；行 5：动作按钮栏
        mp3_main_frame.rowconfigure(4, weight=1)

        # ---------- 目录设置 ----------
        dir_frame = ttk.LabelFrame(mp3_main_frame, text=" 输出位置 ", padding=(12, 9))
        dir_frame.grid(row=0, column=0, sticky='ew', pady=(0, 9))
        dir_frame.columnconfigure(1, weight=1)

        ttk.Label(dir_frame, text="输出目录").grid(
            row=0, column=0, sticky='w', padx=(0, 10), pady=3)
        self.output_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "有声书目录"))
        output_entry = ttk.Entry(dir_frame, textvariable=self.output_dir_var)
        output_entry.grid(row=0, column=1, sticky='ew', pady=3)
        ttk.Button(dir_frame, text="浏览…",
                   command=lambda: self.browse_directory(self.output_dir_var)).grid(
            row=0, column=2, padx=(10, 0), pady=4)

        # ---------- TTS 配置 ----------
        tts_frame = ttk.LabelFrame(mp3_main_frame, text=" 语音合成 ", padding=(12, 9))
        tts_frame.grid(row=1, column=0, sticky='ew', pady=(0, 9))
        tts_frame.columnconfigure(1, weight=1)

        ttk.Label(tts_frame, text="TTS 引擎").grid(
            row=0, column=0, sticky='w', padx=(0, 10), pady=3)
        self.tts_engine_var = tk.StringVar(value=AppConfig.DEFAULT_TTS_ENGINE)
        tts_engine_combo = ttk.Combobox(
            tts_frame, textvariable=self.tts_engine_var, width=16, state="readonly")
        tts_engine_combo['values'] = ["edge-tts", "gtts", "pyttsx3"]
        tts_engine_combo.grid(row=0, column=1, sticky='w', pady=3)
        tts_engine_combo.bind('<<ComboboxSelected>>', self.on_tts_engine_changed)

        ttk.Label(tts_frame, text="语音").grid(
            row=1, column=0, sticky='w', padx=(0, 10), pady=3)
        self.voice_var = tk.StringVar(value=DEFAULT_VOICE)
        self.voice_combo = ttk.Combobox(
            tts_frame, textvariable=self.voice_var, state="readonly")
        self.voice_combo.grid(row=1, column=1, sticky='ew', pady=3)
        self.preview_button = ttk.Button(tts_frame, text="试听")
        self.preview_button.grid(row=1, column=2, padx=(10, 0), pady=4)

        ttk.Label(tts_frame, text="歌词每行字数", width=12, anchor='w').grid(
            row=2, column=0, sticky='w', padx=(0, 10), pady=3)
        self.chars_per_line_var = tk.IntVar(value=DEFAULT_CHARS_PER_LINE)
        spinbox = ttk.Spinbox(tts_frame, from_=5, to=50,
                              textvariable=self.chars_per_line_var, width=8)
        spinbox.grid(row=2, column=1, sticky='w', pady=3)
        ttk.Label(tts_frame, text="建议 10-20 字", style='Muted.TLabel').grid(
            row=2, column=2, sticky='w', padx=(10, 0), pady=4)

        # 初始化语音列表
        self.update_voice_list()

        # ====== 新增试听按钮（用pygame播放）======
        def tts_preview():
            if self.processing:
                messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
                return
            
            text = "腹有诗书气自华，读书万卷始通神"
            voice = self.get_selected_voice_code()  # 提取语音代码
            tts_engine = self.tts_engine_var.get()  # 获取TTS引擎
            self.update_status("正在生成试听音频...")
            self.append_log(f"试听TTS引擎: {tts_engine}")
            self.append_log(f"试听语音: {voice}")
            
            # 为试听创建独立的临时文件（mkstemp：原子创建，避免 mktemp 的竞争）
            preview_mp3 = self.new_temp_path(suffix='.mp3', prefix='preview')
            
            # *** 使用任务调度器执行 TTS 预览 ***
            async def generate_preview_audio():
                """生成预览音频（使用选中的TTS引擎）"""
                try:
                    engine = get_tts_engine(tts_engine)
                    success, _ = await engine.generate_audio(text, preview_mp3, voice)
                    if success:
                        return preview_mp3
                    else:
                        raise Exception(f"{tts_engine} 生成音频失败")
                except ImportError as e:
                    # 依赖未安装
                    raise ImportError(str(e))
                except Exception as e:
                    # 其他错误
                    raise Exception(f"{tts_engine} 错误: {str(e)}")
            
            def on_preview_success(mp3_path):
                """预览生成成功，播放音频"""
                if not os.path.exists(mp3_path):
                    messagebox.showerror("试听失败", "试听音频文件未生成")
                    return
                
                try:
                    import pygame
                except ImportError:
                    messagebox.showerror("缺少依赖", "需要安装 pygame 库才能试听音频。\n请运行：pip install pygame")
                    return
                
                def play_with_pygame():
                    try:
                        pygame.init()
                        pygame.mixer.init()
                        pygame.mixer.music.load(mp3_path)
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
                        # 清理临时文件
                        try:
                            if os.path.exists(mp3_path):
                                os.remove(mp3_path)
                        except:
                            pass
                
                # 在新线程中播放（避免阻塞GUI）
                threading.Thread(target=play_with_pygame, daemon=True).start()
            
            def on_preview_error(error):
                """预览生成失败"""
                error_str = str(error)
                self.update_status(f"试听失败: {error_str}")
                
                # 检查是否是依赖未安装
                if "未安装" in error_str or "pip install" in error_str:
                    messagebox.showerror(
                        "缺少依赖", 
                        f"{error_str}\n\n请在命令行中安装该库后重试。"
                    )
                # 检查是否是gTTS网络问题
                elif "gTTS" in error_str and ("无法连接" in error_str or "Google" in error_str):
                    messagebox.showerror(
                        "gTTS 无法使用",
                        f"gTTS 在中国大陆无法使用（Google 服务被屏蔽）\n\n"
                        f"强烈建议您立即切换为 edge-tts！\n\n"
                        f"操作步骤：\n"
                        f"1. 在上方 'TTS引擎' 下拉框选择 'edge-tts'\n"
                        f"2. 再次点击 '试听' 按钮\n\n"
                        f"edge-tts 优势：\n"
                        f"• 国内稳定可用（微软服务）\n"
                        f"• 高质量音频，多种中文语音\n"
                        f"• 支持精确的字幕时间轴\n\n"
                        f"错误详情：{error_str}"
                    )
                else:
                    messagebox.showerror("试听失败", f"试听音频生成失败:\n{error_str}")
            
            task = Task(
                task_id=str(uuid.uuid4()),
                task_type=TaskType.TTS,
                func=generate_preview_audio,
                callback=on_preview_success,
                error_callback=on_preview_error,
                timeout=120  # 试听短文本，超过 2 分钟视为异常
            )
            self.submit_scheduler_task(task)

        # ====== 试听按钮结束 ======
        self.preview_button.config(command=tts_preview)

        # ---------- 元数据与封面 ----------
        tag_frame = ttk.LabelFrame(mp3_main_frame, text=" 元数据与封面 ", padding=(12, 9))
        tag_frame.grid(row=2, column=0, sticky='ew', pady=(0, 9))
        tag_frame.columnconfigure(1, weight=1)
        tag_frame.columnconfigure(3, weight=1)

        ttk.Label(tag_frame, text="艺术家").grid(row=0, column=0, sticky='w', padx=(0, 10), pady=3)
        self.artist_var = tk.StringVar(value=DEFAULT_ARTIST)
        self.artist_entry = ttk.Entry(tag_frame, textvariable=self.artist_var, state='disabled')
        self.artist_entry.grid(row=0, column=1, sticky='ew', pady=3)

        ttk.Label(tag_frame, text="专辑").grid(row=0, column=2, sticky='w', padx=(16, 10), pady=4)
        self.album_var = tk.StringVar(value="有声书")
        self.album_entry = ttk.Entry(tag_frame, textvariable=self.album_var, state='disabled')
        self.album_entry.grid(row=0, column=3, sticky='ew', pady=3)

        ttk.Label(tag_frame, text="封面图片").grid(row=1, column=0, sticky='w', padx=(0, 10), pady=3)
        self.cover_path_var = tk.StringVar()
        self.cover_entry = ttk.Entry(tag_frame, textvariable=self.cover_path_var, state='disabled')
        self.cover_entry.grid(row=1, column=1, columnspan=2, sticky='ew', pady=3)
        self.cover_button = ttk.Button(tag_frame, text="选择…",
                                       command=self.browse_cover_image, state='disabled')
        self.cover_button.grid(row=1, column=3, sticky='w', pady=3)

        # 输出格式选择
        format_frame = ttk.LabelFrame(mp3_main_frame, text=" 输出格式 ",
                                      padding=(14, 12))
        format_frame.grid(row=3, column=0, sticky='ew', pady=(0, 9))
        format_frame.columnconfigure(1, weight=1)

        ttk.Label(format_frame, text="格式").grid(
            row=0, column=0, sticky='w', padx=(0, 10), pady=3)
        self.output_format_var = tk.StringVar(value="MP3")
        format_radio_frame = ttk.Frame(format_frame)
        format_radio_frame.grid(row=0, column=1, columnspan=2, sticky='w', pady=3)

        ttk.Radiobutton(format_radio_frame, text="MP3（按章节拆分）",
                        variable=self.output_format_var, value="MP3").pack(side=tk.LEFT, padx=(0, 18))
        ttk.Radiobutton(format_radio_frame, text="M4B（合并为单本有声书）",
                        variable=self.output_format_var, value="M4B").pack(side=tk.LEFT)

        ttk.Label(format_frame, text="并发章节数").grid(
            row=1, column=0, sticky='w', padx=(0, 10), pady=3)
        self.concurrent_threads_var = tk.IntVar(value=3)
        thread_spinbox = ttk.Spinbox(format_frame, from_=1, to=5,
                                     textvariable=self.concurrent_threads_var, width=8)
        thread_spinbox.grid(row=1, column=1, sticky='w', pady=3)
        ttk.Label(format_frame, text="1-5 章同时合成", style='Muted.TLabel').grid(
            row=1, column=2, sticky='w', padx=(10, 0), pady=4)

        # FFmpeg状态显示和安装按钮
        ffmpeg_status_frame = ttk.Frame(format_frame)
        ffmpeg_status_frame.grid(row=2, column=0, columnspan=3, sticky='w', pady=(8, 0))

        self.ffmpeg_status_label = ttk.Label(ffmpeg_status_frame,
                                             text="FFmpeg 状态：检测中...",
                                             font=self.ui_font)
        self.ffmpeg_status_label.pack(side=tk.LEFT, padx=(0, 10))

        self.ffmpeg_install_button = ttk.Button(ffmpeg_status_frame,
                                                text="安装 FFmpeg",
                                                command=self.install_ffmpeg_dialog,
                                                state="disabled")
        self.ffmpeg_install_button.pack(side=tk.LEFT, padx=(0, 6))
        
        self.ffmpeg_manual_install_button = ttk.Button(ffmpeg_status_frame,
                                                       text="从本地ZIP安装",
                                                       command=self.install_ffmpeg_from_local,
                                                       state="disabled")
        self.ffmpeg_manual_install_button.pack(side=tk.LEFT)

        # ---------- 动作按钮 ----------
        self.batch_button_frame = ttk.Frame(mp3_main_frame, style='Toolbar.TFrame',
                                            padding=(14, 8))
        self.batch_button_frame.grid(row=5, column=0, sticky='ew')

        self.generate_ebook_button = ttk.Button(
            self.batch_button_frame,
            text="生成有声书",
            command=self.start_generate_ebook,
            style="Accent.TButton"
        )
        self.generate_ebook_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # 合并MP3为M4B按钮
        self.merge_to_m4b_button = ttk.Button(
            self.batch_button_frame,
            text="合并 MP3 为 M4B",
            command=self.merge_mp3_to_m4b_dialog
        )
        self.merge_to_m4b_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # TXT批量转换按钮
        self.batch_txt_button = ttk.Button(
            self.batch_button_frame,
            text="TXT 批量转换",
            command=self.batch_convert_txt_dialog
        )
        self.batch_txt_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # 重新处理失败章节按钮（初始隐藏）
        self.retry_failed_button = ttk.Button(
            self.batch_button_frame,
            text="重新处理失败章节",
            command=self.retry_failed_chapters
        )
        # 初始状态不显示

        self.estimated_time_var = tk.StringVar(value="预计耗时 00:00:00")
        self.estimated_time_label = ttk.Label(
            self.batch_button_frame,
            textvariable=self.estimated_time_var,
            style='SurfaceMuted.TLabel'
        )
        self.estimated_time_label.pack(side=tk.RIGHT, padx=(10, 0))

        # ---------- 运行日志（可折叠，默认收起） ----------
        # 默认展开会把配置区挤出可视区，导致窗口较小时"日志不见了"，因此做成抽屉式。
        self.log_frame = ttk.LabelFrame(mp3_main_frame, text=" 运行日志 ",
                                        padding=(14, 8))

        self.log_text = scrolledtext.ScrolledText(
            self.log_frame,
            wrap=tk.WORD,
            font=(UITheme.FONT_FAMILY, 9),
            background=UITheme.READ_BG,
            foreground=UITheme.TEXT,
            relief='flat', bd=0, padx=8, pady=6, height=8
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)  # 只读

        self.log_toggle_button = ttk.Button(
            self.batch_button_frame, text="显示日志",
            command=self.toggle_log_panel, style='Ghost.TButton')
        self.log_toggle_button.pack(side=tk.RIGHT, padx=(10, 10))

        self.batch_hint_label = ttk.Label(
            self.batch_button_frame,
            text="提示：先选好格式与语音，再点「生成有声书」；详细记录可点右侧「显示日志」。",
            style='SurfaceMuted.TLabel')
        self.batch_hint_label.pack(side=tk.BOTTOM, anchor='w', pady=(10, 0))

    # 日志抽屉展开时需要的额外高度（配置区 + 日志本身）
    LOG_PANEL_EXTRA_HEIGHT = 185

    def toggle_log_panel(self):
        """展开/收起运行日志抽屉

        设置页的配置卡片本身就要 600+ px，如果不给窗口留出空间，
        展开日志只会挤出一条约 40px 的缝隙——和"日志不见了"没区别。
        因此展开时按需增高窗口，收起时缩回，并保证不超出屏幕。
        """
        root = self.root
        if self.log_frame.winfo_ismapped():
            self.log_frame.grid_forget()
            self.log_toggle_button.config(text="显示日志")
            self._resize_window_for_log(expand=False)
        else:
            self.log_frame.grid(row=4, column=0, sticky='nsew')
            self.log_toggle_button.config(text="收起日志")
            self._resize_window_for_log(expand=True)

    def _resize_window_for_log(self, expand):
        """日志抽屉开关时调整窗口高度（不超出屏幕可用区域）"""
        root = self.root
        try:
            root.update_idletasks()
            width = root.winfo_width()
            height = root.winfo_height()
            screen_h = root.winfo_screenheight()
            max_height = int(screen_h * 0.94)
            delta = self.LOG_PANEL_EXTRA_HEIGHT
            target = min(height + delta, max_height) if expand else max(
                height - delta, self.MIN_WINDOW_HEIGHT)
            if target != height:
                root.geometry('%dx%d+%d+%d' % (
                    width, target, root.winfo_x(), root.winfo_y()))
        except tk.TclError:
            pass

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
    
    def on_tts_engine_changed(self, event=None):
        """当TTS引擎变化时更新语音列表"""
        self.update_voice_list()
    
    def update_voice_list(self):
        """根据TTS引擎更新语音列表

        pyttsx3 的语音枚举要初始化 COM，可能阻塞数秒，因此放到后台线程，
        枚举完成后经 message_queue 回到主线程填充下拉框（原实现是同步调用，
        会在 GUI 初始化阶段卡住窗口）。
        """
        engine_name = self.tts_engine_var.get()
        engine = get_tts_engine(engine_name)

        # 内置引擎（edge-tts / gtts）列表是静态的，直接填
        static_voices = getattr(engine, 'STATIC_VOICES', None)
        if static_voices is None:
            try:
                voices = engine.get_available_voices()
            except Exception as e:
                print(f"获取语音列表失败: {e}", file=sys.stderr)
                voices = []
            self._apply_voice_list(voice_display=[f"{c} - {n}" for c, n in voices],
                                   default=voices[0] if voices else None)
            return

        # 需要运行时枚举：先填占位值，再后台枚举
        self._apply_voice_list(voice_display=[f"{c} - {n}" for c, n in static_voices],
                              default=static_voices[0] if static_voices else None)

        def worker():
            try:
                voices = engine.get_available_voices()
            except Exception as e:
                print(f"枚举语音失败: {e}", file=sys.stderr)
                return
            if not voices:
                return
            self.message_queue.put(
                ("voice_list", ([f"{c} - {n}" for c, n in voices], voices[0])))

        threading.Thread(target=worker, daemon=True, name="VoiceEnum").start()

    def _apply_voice_list(self, voice_display, default=None):
        """在主线程填充语音下拉框"""
        self.voice_combo['values'] = voice_display
        if default:
            self.voice_var.set(f"{default[0]} - {default[1]}")
        elif voice_display:
            self.voice_var.set(voice_display[0])
    
    def get_selected_voice_code(self):
        """从选中的语音显示中提取语音代码"""
        voice_str = self.voice_var.get()
        if " - " in voice_str:
            return voice_str.split(" - ")[0]
        return voice_str

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

        # *** 使用任务调度器解析EPUB ***
        task_id = str(uuid.uuid4())
        task = Task(
            task_id=task_id,
            task_type=TaskType.PARSE_EPUB,
            func=parse_epub_data,
            args=(file_path,),
            callback=lambda result: self._on_epub_parsed(result, file_path),
            error_callback=lambda error: self._on_epub_parse_error(error, file_path),
            timeout=600  # 超大 EPUB 也需要能在 10 分钟内给出结果
        )
        self.submit_scheduler_task(task)
    
    def _on_epub_parsed(self, epub_data, file_path):
        """解析成功回调"""
        self.epub_data = epub_data
        if self.epub_data:
            self.message_queue.put(("display_epub", None))
            self.message_queue.put(("status", f"已加载 {os.path.basename(file_path)}"))
        else:
            self.message_queue.put(("status", f"解析失败: {os.path.basename(file_path)}"))
            self.message_queue.put(("error", "无法解析选定的EPUB文件。"))
    
    def _on_epub_parse_error(self, error, file_path):
        """解析失败回调"""
        error_msg = str(error)
        self.message_queue.put(("status", f"解析出错: {error_msg}"))
        self.message_queue.put(("error", f"解析EPUB文件时发生错误: {error_msg}"))
        print(f"解析EPUB失败: {error_msg}", file=sys.stderr)

    def process_messages(self):
        """处理消息队列的消息，更新UI"""
        try:
            # 处理GUI消息队列
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
                elif message_type == "batch_progress":
                    done, total = data
                    self.set_batch_hint(f"正在合成：{done}/{total} 章")
                    self.update_status(f"处理中：{done}/{total}")
                elif message_type == "voice_list":
                    # 后台枚举语音完成（pyttsx3 等），回到主线程填下拉框
                    voice_display, default_voice = data
                    self._apply_voice_list(voice_display, default_voice)
                elif message_type == "batch_complete":
                    self.on_batch_complete()
                elif message_type == "failed_chapter":
                    # 记录失败的章节（加锁，避免与后台线程互改）
                    with self.failed_chapters_lock:
                        self.failed_chapters.append(data)
                elif message_type == "show_retry_button":
                    # 显示重试按钮
                    self.retry_failed_button.pack(side=tk.LEFT, padx=(0, 10))
                elif message_type == "hide_retry_button":
                    # 隐藏重试按钮
                    self.retry_failed_button.pack_forget()
                elif message_type == "clear_failed_chapters":
                    # 清空失败章节列表
                    with self.failed_chapters_lock:
                        self.failed_chapters.clear()
            
            # *** 处理任务调度器的回调 ***
            result = self.task_scheduler.get_result()
            while result:
                result_type, callback, args = result
                # callback 与 progress 都由主线程执行，progress 只在回调被覆盖时才需要
                if result_type in ("callback", "progress") and callback:
                    try:
                        callback(*args)
                    except Exception as e:
                        print(f"Callback error: {e}")
                        traceback.print_exc()
                result = self.task_scheduler.get_result()

        except queue.Empty:
            pass
        finally:
            # 继续检查队列（窗口关闭/退出时不再续期，避免测试收尾后仍有回调）
            if not getattr(self, '_stopping', False):
                try:
                    self.root.after(100, self.process_messages)
                except tk.TclError:
                    pass

    def update_status(self, message):
        """更新状态栏信息（左侧已有固定"状态"标签，这里只放内容）"""
        self.status_label.config(text=str(message))

    def set_batch_hint(self, text):
        """更新动作栏第二行的进度说明（不占用底部状态栏）"""
        hint = getattr(self, 'batch_hint_label', None)
        if hint is not None:
            try:
                hint.config(text=text)
            except tk.TclError:
                pass

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
            self.estimated_time_var.set("预计耗时 00:00:00")
        if hasattr(self, 'estimated_time_var_epub_tab') and self.estimated_time_var_epub_tab:
            self.estimated_time_var_epub_tab.set("预计耗时 00:00:00")

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
        # 注意：封面处理必须自成错误边界。原实现里 Image.open 失败会抛出到外层，
        # 导致后面的"章节列表/导出按钮/输出目录/预计耗时"全部被跳过，
        # 界面看起来像"没读到书"。损坏或格式异常的封面不该影响其余显示逻辑。
        if self.epub_data.get('cover_data'):
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
            except Exception as e:
                print(f"显示封面图片时出错（不影响其余信息）: {e}", file=sys.stderr)
                self.cover_label.config(image="", text="封面无法显示")
                self.cover_label.image = None

            # 无论缩略图是否成功，都把原始封面落到临时文件供 MP3/M4B 使用
            try:
                cover_path = self.new_temp_path(suffix='.jpg', prefix='cover')
                with open(cover_path, "wb") as f:
                    f.write(self.epub_data['cover_data'])
                self.cover_path_var.set(cover_path)
            except Exception as e:
                print(f"写入封面临时文件失败（不影响其余信息）: {e}", file=sys.stderr)
        else:
            self.cover_label.config(image="", text="无封面图片")
            self.cover_label.image = None

        # 显示章节列表
        self.chapter_listbox.delete(0, tk.END)
        self._update_chapter_count()
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
        self.estimated_time_var.set(f"预计耗时 {estimated_time}")
        self.estimated_time_var_epub_tab.set(f"预计耗时 {estimated_time}")

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
        menu.add_command(label="上移章节 (Alt+Up)", command=lambda: self.move_chapter_up(index))
        menu.add_command(label="下移章节 (Alt+Down)", command=lambda: self.move_chapter_down(index))
        menu.add_separator()
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
        self._update_chapter_count()

        # 更新预计耗时标签
        estimated_time = self.calculate_estimated_time()
        if hasattr(self, 'estimated_time_var') and self.estimated_time_var:
            self.estimated_time_var.set(f"预计耗时 {estimated_time}")
        if hasattr(self, 'estimated_time_var_epub_tab') and self.estimated_time_var_epub_tab:
            self.estimated_time_var_epub_tab.set(f"预计耗时 {estimated_time}")
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

    def move_chapter_up(self, index):
        """将章节上移"""
        if index <= 0 or not self.epub_data or 'chapters' not in self.epub_data:
            return
        
        # 交换数据
        chapters = self.epub_data['chapters']
        chapters[index], chapters[index-1] = chapters[index-1], chapters[index]
        
        # 更新界面
        self._refresh_chapter_listbox(select_index=index-1)
        self.update_status(f"已上移章节: {chapters[index-1]['title']}")

    def move_chapter_down(self, index):
        """将章节下移"""
        if not self.epub_data or 'chapters' not in self.epub_data:
            return
        
        chapters = self.epub_data['chapters']
        if index < 0 or index >= len(chapters) - 1:
            return
            
        # 交换数据
        chapters[index], chapters[index+1] = chapters[index+1], chapters[index]
        
        # 更新界面
        self._refresh_chapter_listbox(select_index=index+1)
        self.update_status(f"已下移章节: {chapters[index+1]['title']}")

    def _refresh_chapter_listbox(self, select_index=None):
        """重新加载章节列表并保持选择"""
        if not self.epub_data or 'chapters' not in self.epub_data:
            return
            
        # 保存当前滚动位置
        yview = self.chapter_listbox.yview()
        
        self.chapter_listbox.delete(0, tk.END)
        for i, chapter in enumerate(self.epub_data['chapters']):
            title = chapter.get('title', f"章节 {i + 1}")
            self.chapter_listbox.insert(tk.END, f" {title}")
        self._update_chapter_count()
            
        if select_index is not None:
            self.chapter_listbox.select_set(select_index)
            self.chapter_listbox.activate(select_index)
            self.current_chapter_index = select_index
            
        # 还原滚动位置
        self.chapter_listbox.yview_moveto(yview[0])

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

        # 计算安全文件名（不再创建临时目录/txt：
        # 原实现 mkdtemp 出的 epubReader_chapter_* 既没被使用也从没被清理，
        # 每次单章转换都会在系统临时目录里留下一个永久残留）
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title:
            safe_title = f"章节{index + 1}"

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
            "voice": self.get_selected_voice_code(),  # 提取语音代码
            "tts_engine": self.tts_engine_var.get(),  # 添加TTS引擎
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_path": self.cover_path_var.get()
        }

        # 开始转换：原子占用，避免与批量任务同时写输出目录
        if not self.try_begin_processing():
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
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

        # 禁用转换按钮（processing 已在上层占用）
        self.root.after(0, lambda: self.convert_chapter_button.config(state="disabled"))

        try:
            # 读取封面数据
            cover_data = None
            if params['cover_path'] and os.path.exists(params['cover_path']):
                with open(params['cover_path'], 'rb') as f:
                    cover_data = f.read()
            elif self.epub_data and 'cover_data' in self.epub_data and self.epub_data['cover_data']:
                cover_data = self.epub_data['cover_data']

            # 在后台线程内自建自销事件循环（不再跨线程复用调度器的 loop）
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            self.message_queue.put(("log", "正在生成MP3..."))

            try:
                # 执行TTS转换（使用指定的TTS引擎）
                result = loop.run_until_complete(
                    process_text_to_mp3(
                        params['content'],
                        params['output_mp3'],
                        params['output_lrc'],
                        params['voice'],
                        params['chars_per_line'],
                        tts_engine=params.get('tts_engine', 'edge-tts')
                    )
                )
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

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
                # 弹窗里直接给出原因，别再把用户打发去翻日志
                reason = ' '.join((LAST_TTS_FAILURE or '').split())
                self.message_queue.put(("log", "MP3生成失败"))
                if reason:
                    self.message_queue.put(("log", "  原因: " + reason[:300]))
                self.message_queue.put(("status", "转换失败"))
                self.root.after(0, lambda: messagebox.showerror(
                    "转换失败",
                    "生成MP3失败。\n\n" + (reason[:600] if reason
                                          else "详情见日志: " + AppConfig.LOG_PATH)))

        except Exception as e:
            self.message_queue.put(("log", f"转换失败: {str(e)}"))
            self.message_queue.put(("status", f"错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("转换错误", str(e)))

        finally:
            # 启用转换按钮
            self.end_processing()
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
        
        # *** 使用任务调度器下载FFmpeg ***
        def download_task_wrapper():
            """包装下载任务（同步函数）"""
            return download_ffmpeg(update_progress, check_cancel)
        
        def on_download_success(result):
            """下载成功回调"""
            success, ffmpeg_path, error_msg = result
            
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
        
        def on_download_error(error):
            """下载失败回调"""
            try:
                progress_dialog.destroy()
            except:
                pass
            
            if not cancel_flag['cancelled']:
                messagebox.showerror("安装错误", f"FFmpeg安装过程中出错: {str(error)}")
        
        task = Task(
            task_id=str(uuid.uuid4()),
            task_type=TaskType.DOWNLOAD,
            func=download_task_wrapper,
            callback=on_download_success,
            error_callback=on_download_error,
            cancellable=True,
            timeout=1800  # 5 个镜像源 × 最长下载时间
        )
        self.submit_scheduler_task(task)
    
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
        
        # *** 使用任务调度器安装FFmpeg ***
        def install_task_wrapper():
            """包装安装任务（同步函数）"""
            return install_ffmpeg_from_zip(zip_file, update_progress)
        
        def on_install_success(result):
            """安装成功回调"""
            success, ffmpeg_path, error_msg = result
            
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
        
        def on_install_error(error):
            """安装失败回调"""
            try:
                progress_dialog.destroy()
            except:
                pass
            
            messagebox.showerror("安装错误", f"FFmpeg安装过程中出错: {str(error)}")
        
        task = Task(
            task_id=str(uuid.uuid4()),
            task_type=TaskType.GENERAL,
            func=install_task_wrapper,
            callback=on_install_success,
            error_callback=on_install_error,
            timeout=600  # 从本地 ZIP 解压安装
        )
        self.submit_scheduler_task(task)
    
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
        
        file_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=self.ui_font)
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
            hint_label = ttk.Label(meta_frame, text=hint_text, foreground="green", font=self.ui_font)
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
            # 原子占用，防止与批量生成同时操作同一批文件
            if not self.try_begin_processing():
                messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
                return
            self._m4b_cancel_event.clear()  # 复位上一次的中止信号
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
                "chapter_titles": chapter_titles,
                "cancel_event": self._m4b_cancel_event
            }
            
            threading.Thread(target=self._merge_mp3_thread, args=(params,), daemon=True).start()
        
        ttk.Button(button_frame, text="开始合并", command=start_merge).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="关闭", command=merge_dialog.destroy).pack(side=tk.LEFT, padx=5)
        # 真正的取消：合并过程中可随时中止（原来只能关闭对话框，后台继续跑完）
        cancel_merge_button = ttk.Button(
            button_frame, text="中止合并",
            command=lambda: (self._m4b_cancel_event.set(),
                             cancel_merge_button.config(state="disabled"),
                             self.update_status("已请求中止合并...")))
        cancel_merge_button.pack(side=tk.LEFT, padx=5)
    
    def _merge_mp3_thread(self, params):
        """后台线程：合并MP3为M4B"""
        import time
        start_time = time.time()
        
        mp3_files = params["mp3_files"]
        output_m4b = params["output_m4b"]
        metadata = params["metadata"]
        cover_data = params.get("cover_data")
        chapter_titles = params.get("chapter_titles")
        cancel_event = params.get("cancel_event")
        
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
                chapter_titles,
                cancel_check=(cancel_event.is_set if cancel_event is not None else None)
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
                aborted = "取消" in (error_msg or "")
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put(("log", "已中止M4B合并" if aborted else "M4B合并失败"))
                if error_msg:
                    self.message_queue.put(("log", f"原因: {error_msg}"))
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put(("status", "已中止M4B合并" if aborted else "M4B合并失败"))

                if aborted:
                    self.root.after(0, lambda: messagebox.showinfo(
                        "已中止",
                        "已按你的请求中止合并。\n中间文件已清理，原有 MP3 文件未受影响。"))
                else:
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
            self.end_processing()
            self.root.after(0, lambda: self.generate_ebook_button.config(state="normal"))
            self.root.after(0, lambda: self.merge_to_m4b_button.config(state="normal"))
    
    def start_generate_ebook(self):
        """从内存章节数据批量生成有声书（MP3+LRC）"""
        # 原子占用：避免连点导致两个批次同时写同一批输出文件
        if not self.try_begin_processing():
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
        try:
            self._start_generate_ebook_locked()
        except Exception as e:
            self.end_processing()
            raise

    def _start_generate_ebook_locked(self):
        """start_generate_ebook 的实际实现（调用前需已占用 processing）"""
        # 清空之前的失败记录
        self.message_queue.put(("clear_failed_chapters", None))
        self.message_queue.put(("hide_retry_button", None))
        
        # 检查epub_data
        if not self.epub_data or not self.epub_data.get('chapters'):
            self.end_processing()
            messagebox.showerror("错误", "没有可用的电子书章节。请先加载EPUB文件。")
            return
        
        # 检查M4B格式是否需要FFmpeg
        if self.output_format_var.get() == "M4B" and not self.ffmpeg_installed:
            self.end_processing()
            msg = "M4B格式需要安装FFmpeg。\n\n是否现在安装？"
            if messagebox.askyesno("需要FFmpeg", msg):
                self.install_ffmpeg_dialog()
            return
        output_dir = self.output_dir_var.get()
        if not output_dir:
            output_dir = filedialog.askdirectory(title="选择输出目录")
            if not output_dir:
                self.end_processing()
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
            self.end_processing()
            return
        # 禁用按钮（processing 已在上层占用）
        self.generate_ebook_button.config(state="disabled")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        # 参数
        params = {
            "output_dir": output_dir,
            "chapters": self.epub_data['chapters'],
            "voice": self.get_selected_voice_code(),  # 传语音代码，而不是显示文本
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data,
            "output_format": self.output_format_var.get(),
            "book_title": self.epub_data['title'],
            "ffmpeg_path": self.ffmpeg_path,
            "concurrent_threads": self.concurrent_threads_var.get(),  # 并发数
            "tts_engine": self.tts_engine_var.get()  # 关键修复：批量流程必须带上引擎
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
        """后台线程：从内存章节批量生成有声书（修复版：单一事件循环 + 结果按索引回填）

        关键修复：
        1. 显式传递用户选择的 TTS 引擎（原实现会静默回退到 edge-tts）
        2. 用自己的 asyncio.run 承载并发，不再跨线程复用事件循环
        3. 结果按章节索引回填，合并 M4B 前重建列表，保证顺序与标题严格对应
        """
        import time

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
        concurrent_threads = max(1, int(params.get("concurrent_threads") or 3))
        tts_engine = params.get("tts_engine") or AppConfig.DEFAULT_TTS_ENGINE
        # 重试任务只传失败章节的子集，这里显式保留它们在原书中的真实序号，
        # 保证输出文件名与首次生成完全一致（否则重试会写出错误的序号文件）
        chapter_indices = params.get("chapter_indices")
        if not chapter_indices or len(chapter_indices) != len(chapters):
            chapter_indices = list(range(len(chapters)))
        total_chapters = len(chapters) or 1

        self.message_queue.put(("log", f"开始生成有声电子书，共 {len(chapters)} 个章节"))
        self.message_queue.put(("log", f"输出格式: {output_format}"))
        self.message_queue.put(("log", f"TTS引擎: {tts_engine}"))
        self.message_queue.put(("log", f"并发数: {concurrent_threads}"))
        self.message_queue.put(("status", f"处理中: 0/{len(chapters)}"))

        # -------- 纯逻辑：章节处理（不触碰 Tk 状态） --------
        state = {"done": 0}
        results = {}        # 章节索引 -> 成功生成的 MP3 路径
        local_failed = {}   # 章节索引 -> 失败详情
        state_lock = threading.Lock()

        def resolve_index(position):
            """子集任务的位置 -> 原书章节序号"""
            if 0 <= position < len(chapter_indices):
                return chapter_indices[position]
            return position

        def chapter_paths(index, title):
            safe_title = sanitize_filename(title) or f"章节{index + 1}"
            base = f"{index + 1:03d}-{safe_title}"
            return (os.path.join(output_dir, base + ".mp3"),
                    os.path.join(output_dir, base + ".lrc"))

        def record_failure(index, title, reason, mp3_path=None, lrc_path=None):
            detail = {"index": index, "title": title, "reason": reason}
            if mp3_path:
                detail["mp3_path"] = mp3_path
            if lrc_path:
                detail["lrc_path"] = lrc_path
            with state_lock:
                local_failed[index] = detail
            self.message_queue.put(("failed_chapter", dict(detail)))

        def record_success(index, mp3_path):
            with state_lock:
                results[index] = mp3_path
                local_failed.pop(index, None)

        def bump_progress():
            with state_lock:
                state["done"] += 1
                done = state["done"]
            self.message_queue.put(("progress", int(done / total_chapters * 100)))
            self.message_queue.put((
                "batch_progress", (done, len(chapters))))

        async def run_one(position, chapter, semaphore):
            index = resolve_index(position)
            title = chapter.get("title", f"章节{index + 1}")
            content = chapter.get("content", "")
            output_mp3, output_lrc = chapter_paths(index, title)

            if not content.strip():
                self.message_queue.put(("log", f"跳过空章节: {title}"))
                bump_progress()
                return

            async with semaphore:
                try:
                    self.message_queue.put((
                        "log", f"处理: {title}（{len(content)} 字，"
                               f"{len(chunk_text(content))} 个分块）"))
                    success = await process_text_to_mp3(
                        content, output_mp3, output_lrc, voice, chars_per_line,
                        tts_engine=tts_engine,
                    )
                    if success:
                        if update_mp3_tag(output_mp3, output_lrc, cover_data,
                                          title, artist, album):
                            self.message_queue.put(("log", f"完成: {title}"))
                        else:
                            self.message_queue.put(
                                ("log", f"生成成功但标签失败: {title}"))
                            record_failure(index, title, "ID3标签添加失败",
                                           output_mp3, output_lrc)
                        record_success(index, output_mp3)
                    else:
                        # 带上原因：以前只写章节名，弹窗却让用户"查看日志了解详情"，
                        # 而窗口模式下原因根本没被记下来过
                        reason = ' '.join((LAST_TTS_FAILURE or '').split())
                        self.message_queue.put(("log", f"生成失败: {title}"))
                        if reason:
                            self.message_queue.put(("log", "  原因: " + reason[:300]))
                        record_failure(
                            index, title,
                            "MP3生成失败" + ("（" + reason[:200] + "）" if reason else ""))
                except Exception as e:
                    self.message_queue.put(("log", f"处理出错: {title} - {e}"))
                    traceback.print_exc()
                    record_failure(index, title, f"异常: {e}")
                finally:
                    bump_progress()

        async def run_batch(indices):
            """按索引并发处理，事件循环生命周期由 asyncio.run 托管"""
            # 分块级并发交给 asyncio，避免线程池与事件循环混用
            semaphore = asyncio.Semaphore(concurrent_threads)
            await asyncio.gather(
                *(run_one(pos, chapters[pos], semaphore) for pos in indices))

        try:
            # -------- 第一轮 --------
            pending = [pos for pos, ch in enumerate(chapters) if ch.get("content", "").strip()]
            asyncio.run(run_batch(pending))

            # -------- 自动重试一轮（按索引回填，绝不 append） --------
            if local_failed and not params.get("is_retry"):
                retry_indices = sorted(local_failed)
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put((
                    "log", f"检测到 {len(retry_indices)} 个章节处理失败，自动重试一轮..."))
                self.message_queue.put(("log", "=" * 50))

                async def retry_one(index):
                    chapter = chapters[index]
                    title = chapter.get("title", f"章节{index + 1}")
                    output_mp3, output_lrc = chapter_paths(index, title)
                    reason = local_failed.get(index, {}).get("reason", "")
                    try:
                        if reason == "ID3标签添加失败" and os.path.exists(output_mp3):
                            if update_mp3_tag(output_mp3, output_lrc, cover_data,
                                              title, artist, album):
                                self.message_queue.put(
                                    ("log", f"[自动重试成功] 标签已补: {title}"))
                                record_success(index, output_mp3)
                            else:
                                self.message_queue.put(
                                    ("log", f"[自动重试失败] 标签仍失败: {title}"))
                        else:
                            self.message_queue.put(("log", f"[自动重试] {title}"))
                            success = await process_text_to_mp3(
                                chapter.get("content", ""), output_mp3, output_lrc,
                                voice, chars_per_line, tts_engine=tts_engine,
                            )
                            if success and update_mp3_tag(output_mp3, output_lrc,
                                                          cover_data, title, artist, album):
                                self.message_queue.put(
                                    ("log", f"[自动重试成功] {title}"))
                                record_success(index, output_mp3)
                            else:
                                self.message_queue.put(
                                    ("log", f"[自动重试失败] {title}"))
                    except Exception as e:
                        self.message_queue.put(("log", f"[自动重试异常] {title} - {e}"))

                asyncio.run(run_coros_with_limit(
                    (retry_one(index) for index in retry_indices), concurrent_threads))

            # -------- 结果整理：严格按章节顺序重建，保证 MP3 与标题一一对应 --------
            ordered_indices = sorted(results)
            mp3_files = [results[i] for i in ordered_indices]
            valid_chapter_titles = [
                chapters[i].get("title", f"章节{i + 1}") for i in ordered_indices]

            if output_format == "M4B" and mp3_files:
                self.message_queue.put(("log", "=" * 50))
                self.message_queue.put(("log", f"开始合并 {len(mp3_files)} 个章节为M4B格式..."))
                self.message_queue.put(("status", "正在合并为M4B..."))

                safe_title = sanitize_filename(book_title) or "有声书"
                m4b_output = os.path.join(output_dir, f"{safe_title}.m4b")

                metadata = {
                    'title': book_title,
                    'author': artist,
                    'album': album
                }

                success, error_msg = merge_mp3_to_m4b(
                    mp3_files, m4b_output, metadata, cover_data, ffmpeg_path,
                    valid_chapter_titles)

                if success:
                    self.message_queue.put(("log", f"M4B文件生成成功: {os.path.basename(m4b_output)}"))
                    self.message_queue.put(("log", "正在清理临时MP3文件..."))

                    for mp3_file in mp3_files:
                        try:
                            if os.path.exists(mp3_file):
                                os.remove(mp3_file)
                            lrc_file = os.path.splitext(mp3_file)[0] + ".lrc"
                            if os.path.exists(lrc_file):
                                os.remove(lrc_file)
                        except Exception as e:
                            self.message_queue.put(("log", f"清理文件失败: {e}"))

                    self.message_queue.put(("log", "临时文件清理完成"))
                else:
                    self.message_queue.put(("log", "=" * 50))
                    self.message_queue.put(("log", "M4B合并失败，保留原始MP3文件"))
                    if error_msg:
                        self.message_queue.put(("log", f"失败原因: {error_msg}"))
                    self.message_queue.put(("log", "=" * 50))

            elapsed = time.time() - start_time
            elapsed_str = f"{elapsed:.2f} 秒"
            self.message_queue.put(("progress", 100))

            # 统计失败和成功的章节数（以本章任务的索引结果为准，不用全局列表）
            success_count = len(mp3_files)
            failed_count = len(local_failed)
            with self.failed_chapters_lock:
                self.failed_chapters[:] = [local_failed[i] for i in sorted(local_failed)]

            if output_format == "M4B":
                self.message_queue.put(("status", f"M4B生成完成（耗时 {elapsed_str}）"))
                self.message_queue.put(("log", f"M4B有声书生成完成!"))
            else:
                self.message_queue.put((
                    "status",
                    f"批量生成完成: {success_count}/{len(chapters)}（耗时 {elapsed_str}）"))
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
            # 事件循环已由 asyncio.run 负责创建与关闭，此处只需恢复 UI 状态
            self.end_processing()
            self.root.after(0, lambda: self.generate_ebook_button.config(state="normal"))
            self.root.after(0, lambda: self.merge_to_m4b_button.config(state="normal"))
            self.root.after(0, lambda: self.batch_txt_button.config(state="normal"))

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
            "voice": self.get_selected_voice_code(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data,
            "tts_engine": self.tts_engine_var.get()  # 同样必须带上引擎
        }
        
        # 启动后台线程：原子占用，避免与其他批次互相覆盖输出
        if not self.try_begin_processing():
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
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
        tts_engine = params.get("tts_engine") or AppConfig.DEFAULT_TTS_ENGINE

        total_files = len(txt_files) or 1
        processed_count = 0
        failed_files = []

        self.message_queue.put(("log", f"开始批量转换 {len(txt_files)} 个文件"))
        self.message_queue.put(("log", f"TTS引擎: {tts_engine}"))
        self.message_queue.put(("status", f"批处理中: 0/{len(txt_files)}"))

        # 创建异步任务循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def bump_progress(status_text=None):
            nonlocal processed_count
            processed_count += 1
            self.message_queue.put(("progress", int(processed_count / total_files * 100)))
            self.message_queue.put((
                "status", status_text or f"批处理中: {processed_count}/{len(txt_files)}"))

        try:
            # 遍历处理每个文件
            for txt_file in txt_files:
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

                    bump_progress()
                    continue

                # 读取文本内容
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    if not content.strip():
                        self.message_queue.put(("log", f"跳过空文件: {txt_file}"))
                        bump_progress()
                        continue

                    # 生成MP3和LRC
                    self.message_queue.put(("log", f"处理: {txt_file}"))

                    # 异步执行TTS转换（带上用户选择的引擎）
                    result = loop.run_until_complete(
                        process_text_to_mp3(
                            content,
                            output_mp3,
                            output_lrc,
                            voice,
                            chars_per_line,
                            tts_engine=tts_engine
                        )
                    )

                    if result:
                        # 添加ID3标签
                        if update_mp3_tag(output_mp3, output_lrc, cover_data, base_name, artist, album):
                            self.message_queue.put(("log", f"完成: {txt_file}"))
                        else:
                            self.message_queue.put(("log", f"生成成功但标签失败: {txt_file}"))
                            failed_files.append(txt_file)
                    else:
                        self.message_queue.put(("log", f"生成失败: {txt_file}"))
                        failed_files.append(txt_file)

                except Exception as e:
                    self.message_queue.put(("log", f"处理文件 '{txt_file}' 时出错: {str(e)}"))
                    traceback.print_exc()
                    failed_files.append(txt_file)

                bump_progress()

            # 完成所有处理
            self.message_queue.put(("progress", 100))
            self.message_queue.put((
                "status", f"批处理完成: {len(txt_files) - len(failed_files)}/{len(txt_files)}"))
            self.message_queue.put(("log", "批量转换完成!"))
            if failed_files:
                self.message_queue.put((
                    "log", f"失败 {len(failed_files)} 个: {', '.join(failed_files[:10])}"))
            
            # 弹窗提示
            def show_complete_msg():
                message = (f"完成 {len(txt_files) - len(failed_files)}/{len(txt_files)} "
                           f"个TXT文件批量转换\n输出目录: {output_dir}")
                if failed_files:
                    message += f"\n\n失败 {len(failed_files)} 个，详见运行日志"
                messagebox.showinfo("批量转换完成", message)
            self.root.after(0, show_complete_msg)

        except Exception as e:
            self.message_queue.put(("log", f"批处理任务出错: {str(e)}"))
            self.message_queue.put(("status", f"批处理错误: {str(e)}"))
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("批处理错误", str(e)))

        finally:
            try:
                loop.close()
            except Exception:
                pass
            # 恢复UI状态
            self.end_processing()
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
        
        # 显示失败章节列表（复制一份，避免与后台线程同时读写）
        with self.failed_chapters_lock:
            snapshot = list(self.failed_chapters)
        failed_list = "\n".join([f"  - {fc['title']} ({fc.get('reason', '未知错误')})" 
                                 for fc in snapshot])
        msg = (
            f"共有 {len(snapshot)} 个章节处理失败：\n\n"
            f"{failed_list}\n\n"
            f"是否重新处理这些失败的章节？"
        )
        
        if not messagebox.askyesno("重新处理失败章节", msg):
            return
        
        # 准备重试的章节（同时保留它们在原书中的真实序号）
        retry_chapters_data = []
        retry_chapter_indices = []
        for fc in snapshot:
            chapter_index = fc.get('index')
            if chapter_index is not None and 0 <= chapter_index < len(self.epub_data['chapters']):
                retry_chapters_data.append(self.epub_data['chapters'][chapter_index])
                retry_chapter_indices.append(chapter_index)
        
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
            "chapter_indices": retry_chapter_indices,  # 保留原书序号，文件名与首次一致
            "voice": self.get_selected_voice_code(),
            "chars_per_line": self.chars_per_line_var.get(),
            "artist": self.artist_var.get(),
            "album": self.album_var.get(),
            "cover_data": cover_data,
            "output_format": "MP3",  # 重试时只生成MP3，不合并
            "book_title": self.epub_data['title'],
            "ffmpeg_path": self.ffmpeg_path,
            "tts_engine": self.tts_engine_var.get(),
            "concurrent_threads": self.concurrent_threads_var.get(),
            "is_retry": True  # 标记这是重试任务
        }
        
        # 禁用按钮（原子占用，避免与其他批次冲突）
        if not self.try_begin_processing():
            messagebox.showwarning("处理中", "有转换任务正在进行，请等待完成。")
            return
        self.generate_ebook_button.config(state="disabled")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "\n" + "="*50 + "\n")
        self.log_text.insert(tk.END, f"[重试] 开始重新处理 {len(retry_chapters_data)} 个失败章节...\n")
        self.log_text.insert(tk.END, "="*50 + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        
        # 启动后台线程重试
        threading.Thread(target=self._generate_ebook_thread, args=(params,), daemon=True).start()

    def _teardown(self):
        """停止后台、保存设置、清理本实例临时目录（**不碰 Tk**）

        两个要点：

        1. `_stopping` 必须**最先**立起来，之后消息泵不再续期，界面也不再被
           后台消息改写；
        2. 每一步各自兜住异常，任何一步失败都不能中断剩下的收尾。

        第 2 点是被真实故障逼出来的：打包成窗口模式后，逃到顶层的异常会被
        PyInstaller 弹成 "Failed to execute script ... unhandled exception"
        错误框（实测出现过 `can't invoke "wm" command: application has been
        destroyed`），用户看到的是吓人的报错，而设置根本没来得及保存。

        刻意不碰 Tk，所以窗口已经销毁时（main() 启动序列里就被关掉）也能调，
        main() 的兜底分支正是复用它。
        """
        # 先立停止标志：此后消息泵不再续期
        self._stopping = True

        # *** 停止任务调度器 / 保存设置 / 清理本实例临时目录 ***
        # 只清理本实例的目录：共享 TEMP_DIR 可能正被其他实例使用，
        # 直接 rmtree 会删掉别人正在写入的文件
        for label, action in (
            ("停止任务调度器", self.task_scheduler.stop),
            ("保存设置", self.save_settings),
            ("清理临时目录", self.cleanup_session_temp_dir),
        ):
            try:
                action()
            except Exception:
                self.logger.warning("退出时%s失败（已跳过）", label, exc_info=True)

    def on_closing(self):
        """处理窗口关闭事件，始终弹窗确认"""
        if self.processing:
            msg = "有转换任务正在进行。\n确定要退出吗？"
        else:
            msg = "确定要退出吗？"
        if not messagebox.askyesno("确认退出", msg):
            return

        self._teardown()

        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def calculate_estimated_time(self):
        """计算预计耗时，基于字数（使用与 LRC 估算同一个速度常量）"""
        if not self.epub_data or 'total_words' not in self.epub_data:
            return "00:00:00"

        total_words = self.epub_data['total_words']
        # 与 AppConfig.CHARS_PER_SECOND 保持一致，避免两个互相矛盾的估算
        total_seconds = int(total_words / AppConfig.CHARS_PER_SECOND + 0.5)

        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _install_exception_hooks(log_path):
    """安装全局异常钩子：GUI 回调里的异常此前只会打印到 stderr，容易丢"""
    def report(exc_type, exc_value, exc_tb):
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(text, file=sys.stderr)
        try:
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write(f"\n[{datetime.now().isoformat()}] 未捕获异常\n{text}")
        except Exception:
            pass

    sys.excepthook = report
    if hasattr(threading, 'excepthook'):
        threading.excepthook = lambda args: report(
            args.exc_type, args.exc_value, args.exc_traceback)


def main():
    """主函数"""
    # 统一日志：文件轮转 + stderr，便于事后排查（原来只有 print/traceback）
    logger = setup_logging()
    logger.info("epubReader 启动，日志文件: %s", AppConfig.LOG_PATH)
    _install_exception_hooks(AppConfig.LOG_PATH)

    # 创建并启动应用
    root = tk.Tk()
    # 先让窗口完成映射再取消置顶，否则可能从未置顶过
    root.attributes("-topmost", True)
    root.update_idletasks()
    app = EpubToMp3App(root)

    # 居中窗口
    #
    # 从这里开始窗口已经可见，必须防住"初始化期间窗口就被关掉"：慢启动时用户
    # （或自动化脚本）完全可能在 main() 走完之前点关闭，Tk 的默认行为会直接
    # 销毁窗口，此后**任何** Tk 调用都会抛
    #   TclError: can't invoke "wm" command: application has been destroyed
    # 而 main() 里这几步没人兜，异常逃到顶层；打包成窗口模式后会被 PyInstaller
    # 弹成 "Failed to execute script 'epubReader' due to unhandled exception"
    # 报错框——用户看到的是崩溃，实际上设置本该正常保存。实测 traceback：
    #   File "epubReader.py", line 6018, in main
    #   _tkinter.TclError: can't invoke "wm" command: application has been destroyed
    # 注意 `root.update()` 本身就会处理排队中的 WM_CLOSE，销毁发生在那一步之内，
    # 所以 center_window/update 这段也要一起兜。
    try:
        app.center_window()
        root.update()
    except tk.TclError:
        app._teardown()
        return

    # 取消置顶 + 装上关闭处理器（仅在首次显示时置顶）
    try:
        root.attributes("-topmost", False)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)
    except tk.TclError:
        app._teardown()
        return

    # 启动主循环
    root.mainloop()


if __name__ == "__main__":
    if sys.platform == "win32" and sys.executable.endswith("pythonw.exe"):
        # 行缓冲 + 统一编码，避免崩溃时日志停在缓冲区里
        log_dir = os.getenv("TEMP") or tempfile.gettempdir()
        sys.stdout = open(os.path.join(log_dir, "epubReader_stdout.log"),
                          "w", encoding="utf-8", buffering=1)
        sys.stderr = open(os.path.join(log_dir, "epubReader_stderr.log"),
                          "w", encoding="utf-8", buffering=1)
    main()