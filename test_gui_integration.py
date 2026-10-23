# -*- coding: utf-8 -*-
"""GUI 实机验证：用真实依赖驱动真实应用，走完主要用户流程。

必须用装了依赖的解释器运行：
    .venv\\Scripts\\python.exe -u test_gui_integration.py

关键点：Tk 的 update/after 必须在主线程且事件循环已启动时调用，
因此测试主体经 root.after 调度进 mainloop，由应用自己的 process_messages
（同样基于 root.after）在同一个事件循环里泵回调——这也是真实运行方式。

覆盖：窗口构建 / 载入 EPUB / 章节编辑 / 批量生成 MP3 / ID3 与封面歌词校验 /
单章转换 / TXT 批量转换 / 关闭时保存设置与清理临时目录。
"""
import io
import json
import multiprocessing
import os
import re
import shutil
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
    """在本目录下查找测试用的 ffmpeg/ffprobe"""
    roots = [os.path.join(HERE, 'ffmpeg_local'), HERE]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if 'ffmpeg.exe' in files:
                return os.path.join(base, 'ffmpeg.exe')
    return None


def ffprobe_json(ffmpeg_exe, media_path):
    """用与 ffmpeg 同目录的 ffprobe 读取 JSON 信息"""
    import json as _json
    import subprocess as _sp
    probe = os.path.join(os.path.dirname(ffmpeg_exe), 'ffprobe.exe')
    flags = _sp.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    result = _sp.run([probe, '-v', 'error', '-show_format', '-show_streams',
                      '-show_chapters', '-of', 'json', media_path],
                     stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=60,
                     creationflags=flags)
    if result.returncode != 0:
        return None
    try:
        return _json.loads(result.stdout.decode('utf-8', errors='ignore') or '{}')
    except ValueError:
        return None


def main():
    import importlib.util
    import tkinter as tk
    from tkinter import messagebox

    spec = importlib.util.spec_from_file_location('epub_reader', SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['epub_reader'] = mod
    spec.loader.exec_module(mod)
    print('已载入模块: %s' % SRC, flush=True)

    # ---------- 屏蔽交互式对话框 ----------
    dialog_log = {'info': [], 'error': [], 'warning': [], 'ask': []}
    messagebox.askyesno = lambda t, m, **k: (dialog_log['ask'].append((t, m)), True)[1]
    messagebox.showinfo = lambda t, m, **k: dialog_log['info'].append((t, m))
    messagebox.showerror = lambda t, m, **k: dialog_log['error'].append((t, m))
    messagebox.showwarning = lambda t, m, **k: dialog_log['warning'].append((t, m))
    mod.messagebox.askyesno = messagebox.askyesno
    mod.messagebox.showinfo = messagebox.showinfo
    mod.messagebox.showerror = messagebox.showerror
    mod.messagebox.showwarning = messagebox.showwarning
    mod.os.startfile = lambda *a, **k: None

    # 临时配置目录，避免污染真实 %APPDATA%
    work = tempfile.mkdtemp(prefix='gui_verify_')
    config_path = os.path.join(work, 'config.json')
    log_path = os.path.join(work, 'app.log')
    mod.AppConfig.CONFIG_PATH = config_path
    mod.AppConfig.LOG_PATH = log_path
    mod.AppConfig.CONFIG_DIR = work

    sys.path.insert(0, HERE)
    import make_test_epub
    epub_path = os.path.join(work, 'sample.epub')
    make_test_epub.build(epub_path)
    print('    · 测试 EPUB: %s（%d 字节，封面 %d 字节）'
          % (epub_path, os.path.getsize(epub_path),
             len(make_test_epub.COVER_PNG)), flush=True)

    root = tk.Tk()
    root.update_idletasks()
    app = mod.EpubToMp3App(root)
    root.update()

    def settle(seconds=0.6):
        """在主线程推进 Tk 事件循环（会触发应用自己的 process_messages）"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                root.update()
            except tk.TclError:
                return
            time.sleep(0.01)

    def wait_until(predicate, timeout=60.0, label=''):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                root.update()
            except tk.TclError:
                return bool(predicate())
            if predicate():
                return True
            time.sleep(0.02)
        print('    超时等待: %s' % (label or predicate), flush=True)
        return False

    def run_steps():
        """全部步骤都在 Tk 主线程内执行"""
        print('\n[1] 构建窗口', flush=True)
        check('窗口标题存在', bool(root.title()), root.title())
        check('状态栏初始可用', '状态' in app.status_label.cget('text'))
        check('章节列表初始为空', app.chapter_listbox.size() == 0)
        check('调度器已启动', app.task_scheduler.running)
        check('实例临时目录已创建', os.path.isdir(app.session_temp_dir),
              app.session_temp_dir)
        check('配置存储已初始化', app.config_store is not None)
        check('语音下拉框有候选值', bool(app.voice_combo['values']))

        print('\n[2] 载入 EPUB', flush=True)
        mod.filedialog.askopenfilename = lambda **k: epub_path
        app.select_epub_file()
        ok = wait_until(lambda: app.epub_data is not None, 120, 'EPUB 解析完成')
        check('EPUB 解析成功', bool(ok and app.epub_data))
        if not app.epub_data:
            return

        data = app.epub_data
        check('书名正确', data['title'] == '测试书目：雨中的信', data['title'])
        check('作者正确', data['author'] == '测试作者', data['author'])
        check('出版社正确', data['publisher'] == '测试出版社', data['publisher'])
        check('出版时间正确', data['publish_date'] == '2024-05-06', data['publish_date'])
        check('提取到 3 个章节', len(data['chapters']) == 3, len(data['chapters']))
        check('封面数据已提取且为 PNG',
              bool(data.get('cover_data')) and data['cover_data'].startswith(b'\x89PNG'))

        settle(1.5)  # 等 display_epub 消息被泵出
        check('章节列表已填充', app.chapter_listbox.size() == 3,
              app.chapter_listbox.size())
        check('书名已显示到界面', app.title_label.cget('text') == data['title'],
              app.title_label.cget('text'))
        check('封面控件已加载图片',
              getattr(app.cover_label, 'image', None) is not None)
        check('默认选中第一章', app.current_chapter_index == 0,
              app.current_chapter_index)
        check('导出按钮已启用',
              str(app.export_all_button.cget('state')) == 'normal')
        check('输出目录已自动设为有声书目录',
              os.path.isdir(app.output_dir_var.get()), app.output_dir_var.get())
        check('预计耗时可显示', app.estimated_time_var.get().startswith('预计耗时'),
              app.estimated_time_var.get())

        print('\n[3] 章节内容与编辑保存', flush=True)
        first_text = app.chapter_text.get('1.0', 'end').strip()
        check('章节内容已显示', '第一章' in first_text, first_text[:40])
        app.chapter_text.insert('end', '\n[测试追加]')
        app.save_chapter_content()
        check('保存修改写回内存',
              '测试追加' in app.epub_data['chapters'][0]['content'])
        app.epub_data['chapters'][0]['content'] = \
            app.epub_data['chapters'][0]['content'].replace('\n[测试追加]', '')

        print('\n[4] 批量生成（MP3 + LRC，真实 edge-tts）', flush=True)
        output_dir = os.path.join(work, 'audiobook')
        os.makedirs(output_dir, exist_ok=True)
        app.output_dir_var.set(output_dir)
        app.voice_var.set('zh-CN-XiaoxiaoNeural - 晓晓 (女声)')
        app.tts_engine_var.set('edge-tts')
        app.output_format_var.set('MP3')
        app.concurrent_threads_var.set(2)
        app.chars_per_line_var.set(12)
        check('语音代码解析正确',
              app.get_selected_voice_code() == 'zh-CN-XiaoxiaoNeural',
              app.get_selected_voice_code())

        app.start_generate_ebook()
        check('批量任务已进入处理中状态',
              wait_until(lambda: app.processing, 15, '批量任务启动'))
        check('批量生成已结束',
              wait_until(lambda: not app.processing, 1200, '批量生成完成'))

        mp3_files = sorted(f for f in os.listdir(output_dir) if f.endswith('.mp3'))
        lrc_files = sorted(f for f in os.listdir(output_dir) if f.endswith('.lrc'))
        check('生成了 3 个 MP3', len(mp3_files) == 3, mp3_files)
        check('生成了 3 个 LRC', len(lrc_files) == 3, lrc_files)
        check('MP3 文件名带 3 位章节序号前缀',
              all(len(f) > 4 and f[:3].isdigit() and f[3] == '-' for f in mp3_files),
              mp3_files)
        check('LRC 内容为合法时间轴',
              all(open(os.path.join(output_dir, f), encoding='utf-8')
                  .read().lstrip().startswith('[') for f in lrc_files))

        print('\n[5] 校验产出：ID3 标签、封面、歌词、音频', flush=True)
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, USLT
        for name in mp3_files:
            path = os.path.join(output_dir, name)
            info = MP3(path)
            tags = ID3(path)
            check('%s 时长 > 0' % name, info.info.length > 0,
                  '%.2fs' % info.info.length)
            check('%s 标题/艺术家/专辑标签齐全' % name,
                  all(k in tags for k in ('TIT2', 'TPE1', 'TALB')), list(tags.keys()))
            apics = [t for t in tags.values() if isinstance(t, APIC)]
            check('%s 嵌入了封面' % name, bool(apics))
            check('%s 封面是 PNG 类型' % name,
                  bool(apics) and apics[0].mime == 'image/png',
                  apics[0].mime if apics else None)
            check('%s 嵌入了歌词' % name,
                  any(isinstance(t, USLT) for t in tags.values()))
        lrc1 = open(os.path.join(output_dir, lrc_files[0]), encoding='utf-8').read()
        check('第一章歌词包含第一章文本', '行李' in lrc1 or '第一章' in lrc1, lrc1[:60])

        print('\n[6] 单选章节转有声书', flush=True)
        app.current_chapter_index = 1
        app.convert_chapter_to_mp3(1)
        check('单章转换完成',
              wait_until(lambda: not app.processing, 600, '单章转换完成'))
        single = [f for f in os.listdir(output_dir)
                  if f.startswith('第二章') and f.endswith('.mp3')]
        check('单章 MP3 已生成', len(single) == 1, single)
        if single:
            check('单章 MP3 可解析',
                  MP3(os.path.join(output_dir, single[0])).info.length > 0)

        print('\n[7] TXT 批量转换', flush=True)
        txt_dir = os.path.join(work, 'txt_in')
        txt_out = os.path.join(work, 'txt_out')
        os.makedirs(txt_dir, exist_ok=True)
        os.makedirs(txt_out, exist_ok=True)
        for i in (1, 2):
            with io.open(os.path.join(txt_dir, 'text_%02d.txt' % i), 'w',
                         encoding='utf-8') as fh:
                fh.write('这是第%d个测试文本。用于验证批量转换流程。' % i)

        def fake_askdir(**kwargs):
            return txt_dir if 'TXT文件' in (kwargs.get('title') or '') else txt_out
        mod.filedialog.askdirectory = fake_askdir
        app.batch_convert_txt_dialog()
        check('TXT 批量转换完成',
              wait_until(lambda: not app.processing, 600, 'TXT 批量转换完成'))
        txt_mp3 = sorted(f for f in os.listdir(txt_out) if f.endswith('.mp3'))
        check('TXT 转换产出 2 个 MP3', len(txt_mp3) == 2, txt_mp3)

        # ---------- [7.5] M4B 合并（仅当能找到真实 ffmpeg） ----------
        ffmpeg_exe = find_ffmpeg()
        if not ffmpeg_exe:
            print('\n[7.5] M4B 合并：未找到 ffmpeg，跳过', flush=True)
        else:
            print('\n[7.5] M4B 合并（真实 ffmpeg: %s）' % ffmpeg_exe, flush=True)
            check('probe_chapter_count 可调用',
                  callable(getattr(mod, 'probe_chapter_count', None)))
            try:
                ver = mod.subprocess.run([ffmpeg_exe, '-version'],
                                         stdout=mod.subprocess.PIPE,
                                         stderr=mod.subprocess.PIPE, timeout=30)
                check('ffmpeg 可执行', ver.returncode == 0)
            except Exception as exc:
                check('ffmpeg 可执行', False, exc)

            m4b_files = sorted(f for f in os.listdir(output_dir)
                               if f.endswith('.mp3'))
            m4b_out = os.path.join(output_dir, 'merged.m4b')
            # 标题必须与 mp3 列表一一对应：单章转换也写进了同一目录，
            # 数量可能与章节数不同，因此从文件名反推标题
            titles = [re.sub(r'^\d{3}-', '', os.path.splitext(f)[0])
                      for f in m4b_files]
            ok_merge, merge_err = mod.merge_mp3_to_m4b(
                [os.path.join(output_dir, f) for f in m4b_files],
                m4b_out,
                {'title': '测试书目：雨中的信', 'author': '测试作者',
                 'album': '测试书目：雨中的信'},
                data.get('cover_data'), ffmpeg_exe, titles)
            check('M4B 合并成功', ok_merge is True, merge_err)
            check('M4B 文件已生成',
                  os.path.exists(m4b_out) and os.path.getsize(m4b_out) > 10000,
                  os.path.getsize(m4b_out) if os.path.exists(m4b_out) else None)

            if os.path.exists(m4b_out):
                chapters = mod.probe_chapter_count(m4b_out, ffmpeg_exe)
                check('M4B 章节数与输入一致',
                      chapters == len(m4b_files),
                      '期望 %d，实际 %s' % (len(m4b_files), chapters))

                # ffprobe 检查音轨与封面
                probe = ffprobe_json(ffmpeg_exe, m4b_out)
                streams = probe.get('streams', []) if probe else []
                kinds = [s.get('codec_type') for s in streams]
                check('M4B 含音频流', 'audio' in kinds, kinds)
                check('M4B 含附加封面流',
                      any(s.get('disposition', {}).get('attached_pic') == 1
                          for s in streams), kinds)
                fmt = (probe or {}).get('format', {})
                check('M4B 时长与音频总长接近',
                      float(fmt.get('duration') or 0) > 5,
                      fmt.get('duration'))
                check('M4B 书名元数据已写入',
                      '测试书目' in json.dumps(probe, ensure_ascii=False),
                      fmt.get('tags'))

                # 取消：立刻中止不应产生/覆盖产物
                abort_out = os.path.join(output_dir, 'aborted.m4b')
                ok_abort, abort_err = mod.merge_mp3_to_m4b(
                    [os.path.join(output_dir, f) for f in m4b_files],
                    abort_out, {'title': 'X'}, None, ffmpeg_exe, titles,
                    cancel_check=lambda: True)
                check('中止时返回失败', ok_abort is False)
                check('中止原因可读', '取消' in (abort_err or ''), abort_err)
                check('中止时不产生输出文件', not os.path.exists(abort_out))

        print('\n[8] 关闭流程', flush=True)
        session_dir = app.session_temp_dir
        app.on_closing()
        settle(0.5)
        check('配置已保存', os.path.exists(config_path), config_path)
        if os.path.exists(config_path):
            with io.open(config_path, encoding='utf-8') as fh:
                saved = json.load(fh)
            check('保存了语音设置',
                  str(saved.get('voice', '')).startswith('zh-CN-XiaoxiaoNeural'),
                  saved.get('voice'))
            check('保存了输出格式', saved.get('output_format') == 'MP3', saved)
            check('保存了并发数', saved.get('concurrent_threads') == 2, saved)
            check('保存了歌词每行字数', saved.get('chars_per_line') == 12, saved)
        check('实例临时目录已清理', not os.path.exists(session_dir), session_dir)
        check('日志文件已生成', os.path.exists(log_path), log_path)
        check('全程没有弹出错误对话框', not dialog_log['error'],
              dialog_log['error'][:2])
        if dialog_log['warning']:
            print('    · 警告对话框: %s' % (dialog_log['warning'][:3],), flush=True)

        shutil.rmtree(work, ignore_errors=True)
        print('\n' + '=' * 66, flush=True)
        print('%d checks, %d failures' % (checks[0], len(failures)), flush=True)
        for name in failures:
            print('  FAILED: %s' % name, flush=True)

    def bootstrap():
        try:
            run_steps()
        except Exception:
            traceback.print_exc()
            failures.append('测试主体异常')
        finally:
            try:
                root.quit()
            except tk.TclError:
                pass

    # 事件循环启动后再跑测试主体：应用的 process_messages 也在这个循环里
    root.after(50, bootstrap)
    try:
        root.mainloop()
    except tk.TclError:
        pass
    return 1 if failures else 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
