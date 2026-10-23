# -*- coding: utf-8 -*-
"""加入 EPUBREADER_DEBUG 环境变量支持：把关键路径与启动信息写到固定文件。

打包后的程序没有控制台，出问题时无法查看 print/traceback，
这个开关让用户（或支持人员）能拿到一份"运行时体检报告"。
"""
import io

F = r'D:\SynologyDrive\图片\epub reader.py'
src = io.open(F, encoding='utf-8').read()

old = '''        self.logger = setup_logging()
        self.logger.info("界面初始化，临时目录: %s", self.session_temp_dir)'''
new = '''        self.logger = setup_logging()
        self.logger.info("界面初始化，临时目录: %s", self.session_temp_dir)
        self.write_debug_report()'''
assert src.count(old) == 1
src = src.replace(old, new, 1)

anchor = '''    def _collect_settings(self):'''
helper = '''    def write_debug_report(self):
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
                fh.write('\\n'.join(lines) + '\\n')
        except Exception as exc:
            print('写调试报告失败: %s' % exc, file=sys.stderr)

    def _collect_settings(self):'''
assert src.count(anchor) == 1
src = src.replace(anchor, helper, 1)

io.open(F, 'w', encoding='utf-8', newline='').write(src)
compile(src, 'x', 'exec')
print('已加入 EPUBREADER_DEBUG 支持，语法 OK')
