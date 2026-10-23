# -*- coding: utf-8 -*-
"""离线端到端验证：用本地 FFmpeg 生成真实音频，替代联网 TTS。

背景：Microsoft 的 edge-tts WebSocket 端点当前对全部请求返回 403
（/voices/list 仍 200），属于上游服务变化（见 rany2/edge-tts#458）。
为了在不依赖该服务的前提下验证「分块 → 拼接 → 字幕 → ID3 → M4B」整条链路，
这里注册一个本地引擎：用 ffmpeg 的 sine 合成真实 MP3，并生成真实 SRT。

用法：
    .venv\\Scripts\\python.exe run_offline_e2e.py
"""
import io
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'epub reader.py')
if not os.path.exists(SRC):
    SRC = r'D:\SynologyDrive\图片\epub reader.py'

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''), flush=True)
    if not cond:
        failures.append(label)


def find_ffmpeg():
    for root in (os.path.join(HERE, 'ffmpeg_local'), HERE):
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if 'ffmpeg.exe' in files:
                return os.path.join(base, 'ffmpeg.exe')
    return shutil.which('ffmpeg')


def main():
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print('未找到 ffmpeg，跳过离线端到端验证')
        return 0
    print('使用 ffmpeg: %s' % ffmpeg, flush=True)

    spec = importlib.util.spec_from_file_location('epub_reader', SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['epub_reader'] = mod
    spec.loader.exec_module(mod)

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

    class LocalFfmpegEngine(mod.TTSEngine):
        """本地引擎：用 ffmpeg 合成可听辨的真实 MP3 + 真实 SRT 时间轴"""

        STATIC_VOICES = [('local-sine', '本地合成（离线测试）')]

        @staticmethod
        async def generate_audio(text, output_audio, voice, **kwargs):
            return LocalFfmpegEngine._generate(text, output_audio, kwargs)

        @staticmethod
        def _generate(text, output_audio, kwargs=None):
            kwargs = kwargs or {}
            chunks = mod.chunk_text(text) or [text]
            os.makedirs(os.path.dirname(output_audio) or '.', exist_ok=True)
            work = tempfile.mkdtemp(prefix='local_tts_')
            parts, srt_blocks = [], []
            index_base, offset_ms = 0, 0
            try:
                for position, chunk in enumerate(chunks):
                    seconds = max(0.6, len(chunk) / 4.5)
                    part = os.path.join(work, '%04d.mp3' % position)
                    subprocess.run(
                        [ffmpeg, '-f', 'lavfi', '-i',
                         'sine=frequency=%d:duration=%.2f' % (420 + position * 40, seconds),
                         '-codec:a', 'libmp3lame', '-b:a', '64k', '-y', part],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=flags, check=True)
                    duration_ms = mod.mp3_duration_ms(part)
                    # 造一条与音频等长的字幕，跨块时验证时间轴平移
                    srt = '1\n00:00:00,000 --> %s\n%s\n' % (
                        mod._format_srt_time(max(duration_ms - 50, 100)),
                        chunk[:12])
                    shifted, index_base = mod.shift_srt(srt, offset_ms, index_base)
                    if shifted:
                        srt_blocks.append(shifted)
                    offset_ms += duration_ms
                    parts.append(part)
                    cb = kwargs.get('progress_callback')
                    if cb:
                        cb(position + 1, len(chunks))
                with open(output_audio, 'wb') as out:
                    for part in parts:
                        with open(part, 'rb') as fh:
                            shutil.copyfileobj(fh, out)
                return True, '\n'.join(srt_blocks)
            except Exception:
                traceback.print_exc()
                return False, None
            finally:
                shutil.rmtree(work, ignore_errors=True)

    mod.TTS_ENGINES['local-ffmpeg'] = LocalFfmpegEngine

    import tkinter as tk
    from tkinter import messagebox
    dialog_log = {'error': [], 'warning': []}
    messagebox.askyesno = lambda *a, **k: True
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showerror = lambda t, m, **k: dialog_log['error'].append((t, m))
    messagebox.showwarning = lambda t, m, **k: dialog_log['warning'].append((t, m))
    mod.messagebox.askyesno = messagebox.askyesno
    mod.messagebox.showinfo = messagebox.showinfo
    mod.messagebox.showerror = messagebox.showerror
    mod.messagebox.showwarning = messagebox.showwarning
    mod.os.startfile = lambda *a, **k: None

    work = tempfile.mkdtemp(prefix='offline_e2e_')
    mod.AppConfig.CONFIG_PATH = os.path.join(work, 'config.json')
    mod.AppConfig.LOG_PATH = os.path.join(work, 'app.log')
    mod.AppConfig.CONFIG_DIR = work

    sys.path.insert(0, HERE)
    import make_test_epub
    epub_path = os.path.join(work, 'sample.epub')
    make_test_epub.build(epub_path)

    root = tk.Tk()
    root.update_idletasks()
    app = mod.EpubToMp3App(root)
    root.update()

    def settle(seconds=0.5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                root.update()
            except tk.TclError:
                return
            time.sleep(0.01)

    def wait_until(pred, timeout=180.0, label=''):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                root.update()
            except tk.TclError:
                return bool(pred())
            if pred():
                return True
            time.sleep(0.02)
        print('    超时: %s' % (label or pred), flush=True)
        return False

    def run_steps():
        print('\n[1] 载入 EPUB', flush=True)
        mod.filedialog.askopenfilename = lambda **k: epub_path
        app.select_epub_file()
        check('EPUB 解析完成', wait_until(lambda: app.epub_data is not None, 90))
        settle(1.2)
        check('章节列表已填充', app.chapter_listbox.size() == 3,
              app.chapter_listbox.size())

        print('\n[2] 批量生成（本地 ffmpeg 引擎，不联网）', flush=True)
        out_dir = os.path.join(work, 'audio')
        os.makedirs(out_dir, exist_ok=True)
        app.output_dir_var.set(out_dir)
        app.tts_engine_var.set('local-ffmpeg')
        app.voice_combo['values'] = ['local-sine - 本地合成（离线测试）']
        app.voice_var.set('local-sine - 本地合成（离线测试）')
        app.output_format_var.set('MP3')
        app.concurrent_threads_var.set(2)
        app.start_generate_ebook()
        check('批量任务启动', wait_until(lambda: app.processing, 15))
        check('批量生成完成', wait_until(lambda: not app.processing, 300))

        mp3s = sorted(f for f in os.listdir(out_dir) if f.endswith('.mp3'))
        lrcs = sorted(f for f in os.listdir(out_dir) if f.endswith('.lrc'))
        if not mp3s:
            print('    --- 运行日志 ---', flush=True)
            try:
                content = app.log_text.get('1.0', 'end').strip()
            except Exception as exc:
                content = '<读取日志失败: %s>' % exc
            for line in content.splitlines()[-25:]:
                print('    | %s' % line, flush=True)
            if dialog_log['error']:
                print('    错误弹窗: %s' % (dialog_log['error'][:2],), flush=True)
        check('产出 3 个 MP3', len(mp3s) == 3, mp3s)
        check('产出 3 个 LRC', len(lrcs) == 3, lrcs)

        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, USLT
        for name in mp3s:
            path = os.path.join(out_dir, name)
            info = MP3(path)
            tags = ID3(path)
            check('%s 时长 > 0' % name, info.info.length > 0,
                  '%.2fs' % info.info.length)
            check('%s ID3 完整' % name,
                  all(k in tags for k in ('TIT2', 'TPE1', 'TALB')), list(tags.keys()))
            check('%s 有封面' % name,
                  any(isinstance(t, APIC) for t in tags.values()))
            check('%s 有歌词' % name,
                  any(isinstance(t, USLT) for t in tags.values()))

        lrc = open(os.path.join(out_dir, lrcs[0]), encoding='utf-8').read()
        check('LRC 为合法时间轴', lrc.lstrip().startswith('['))
        times = [int(m.group(1)) * 60 + int(m.group(2))
                 for m in re.finditer(r'\[(\d{2}):(\d{2})\.\d{2}\]', lrc)]
        check('LRC 时间戳单调不减', times == sorted(times), times[:6])

        print('\n[3] M4B 合并', flush=True)
        m4b = os.path.join(out_dir, 'book.m4b')
        ok, err = mod.merge_mp3_to_m4b(
            [os.path.join(out_dir, f) for f in mp3s], m4b,
            {'title': '离线验证用书', 'author': '测试作者', 'album': '离线验证用书'},
            app.epub_data.get('cover_data'), ffmpeg,
            [re.sub(r'^\d{3}-', '', os.path.splitext(f)[0]) for f in mp3s])
        check('M4B 合并成功', ok is True, err)
        if os.path.exists(m4b):
            chapters = mod.probe_chapter_count(m4b, ffmpeg)
            check('M4B 章节数正确', chapters == len(mp3s),
                  '期望 %d 实际 %s' % (len(mp3s), chapters))
        else:
            check('M4B 文件存在', False)

        print('\n[4] 关闭', flush=True)
        app.on_closing()
        check('配置已保存', os.path.exists(mod.AppConfig.CONFIG_PATH))
        check('无错误弹窗', not dialog_log['error'], dialog_log['error'][:2])
        shutil.rmtree(work, ignore_errors=True)

    def bootstrap():
        try:
            run_steps()
        except Exception:
            traceback.print_exc()
            failures.append('主体异常')
        finally:
            try:
                root.quit()
            except tk.TclError:
                pass

    root.after(80, bootstrap)
    try:
        root.mainloop()
    except tk.TclError:
        pass

    print('\n' + '=' * 62, flush=True)
    print('%d checks, %d failures' % (checks[0], len(failures)), flush=True)
    for name in failures:
        print('  FAILED: %s' % name, flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
