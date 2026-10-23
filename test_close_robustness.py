# -*- coding: utf-8 -*-
"""退出流程与界面回调异常的健壮性测试。

覆盖两个真实故障（都是打包成窗口模式后才暴露的）：

1. 退出收尾里任何一步抛异常，都会让异常逃到顶层，被 PyInstaller 弹成
   "Failed to execute script ... unhandled exception"，而设置根本没保存。
   实测出现过 `can't invoke "wm" command: application has been destroyed`。
   → 收尾必须逐步兜异常，且"保存设置"必须执行到。
2. Tk 回调里的异常默认只打到 stderr。窗口程序没有 stderr，等于什么线索都没有；
   sys.excepthook 也管不到这条路径（只有顶层异常才走它）。
   → 必须由 root.report_callback_exception 接管并写进应用日志。

用法（需要完整依赖，走 PYTHONPATH 指向 .venv 的 site-packages）：
    python test_close_robustness.py "D:\\SynologyDrive\\图片\\epub reader.py"
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import traceback

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'
if not os.path.exists(SRC):
    print('找不到目标文件: %s' % SRC)
    sys.exit(1)

checks = 0
failures = []


def check(label, condition, detail=''):
    global checks
    checks += 1
    if condition:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s%s' % (label, (' -> %s' % (detail,)) if detail else ''))
        failures.append(label)


# 隔离运行时目录，避免污染真实 %APPDATA%
ROOT = tempfile.mkdtemp(prefix='close_robust_')
for sub in ('Roaming', 'Local', 'temp'):
    os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
os.environ['APPDATA'] = os.path.join(ROOT, 'Roaming')
os.environ['LOCALAPPDATA'] = os.path.join(ROOT, 'Local')
os.environ['TEMP'] = os.path.join(ROOT, 'temp')
os.environ['TMP'] = os.path.join(ROOT, 'temp')
os.environ['USERPROFILE'] = ROOT

spec = importlib.util.spec_from_file_location('epub_under_close_test', SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules['epub_under_close_test'] = mod
try:
    spec.loader.exec_module(mod)
except ModuleNotFoundError as exc:
    # 需要 PIL / bs4 / ebooklib / mutagen 等运行期依赖；缺了就跳过，不算失败
    print('缺少依赖（%s），跳过退出健壮性测试（这不是失败）' % exc.name)
    sys.exit(0)
except Exception:
    print('导入目标模块失败:')
    traceback.print_exc()
    print('0 checks, 1 failures')
    sys.exit(1)

# messagebox 全部替换成记录器：自动应答，同时能断言"没有错误弹窗"
dialogs = []


def stub(name):
    def fn(*args, **kwargs):
        dialogs.append((name, [str(a)[:60] for a in args]))
        return True
    fn.__name__ = name
    return fn


for name in ('askyesno', 'showinfo', 'showerror', 'showwarning',
             'askokcancel', 'askquestion'):
    if hasattr(mod.messagebox, name):
        setattr(mod.messagebox, name, stub(name))

root = mod.tk.Tk()
root.withdraw()                     # 测试不需要真的显示窗口
app = mod.EpubToMp3App(root)

print('\n[1] Tk 回调异常必须进日志（sys.excepthook 管不到这条路径）')
cfg = os.path.join(ROOT, 'Roaming', 'epubReader', 'config.json')
log = os.path.join(ROOT, 'Roaming', 'epubReader', 'epubReader.log')
check('应用日志已创建', os.path.exists(log), log)

check('已接管 root.report_callback_exception',
      root.report_callback_exception.__func__ is app._report_callback_exception.__func__
      if hasattr(root.report_callback_exception, '__func__') else
      root.report_callback_exception == app._report_callback_exception)

before = io.open(log, encoding='utf-8', errors='ignore').read() if os.path.exists(log) else ''
try:
    raise RuntimeError('模拟界面回调异常')
except RuntimeError:
    app._report_callback_exception(*sys.exc_info())
after = io.open(log, encoding='utf-8', errors='ignore').read() if os.path.exists(log) else ''
check('回调异常被写进日志', '模拟界面回调异常' in after and len(after) > len(before))

print('\n[2] 退出收尾：中间步骤失败也不能中断保存设置')
calls = []


def boom():
    calls.append('stop')
    raise RuntimeError('模拟调度器停止失败')


app.task_scheduler.stop = boom
app.on_closing()

check('确认对话框弹出过', any(n == 'askyesno' for n, _a in dialogs), dialogs)
check('调度器停止确实被调用（并失败了）', calls == ['stop'], calls)
check('关闭标志已立起', app._stopping is True)
check('即使 stop() 抛异常，配置文件仍写出', os.path.exists(cfg), cfg)
if os.path.exists(cfg):
    content = io.open(cfg, encoding='utf-8', errors='ignore').read()
    check('配置文件含设置项', content.strip().startswith('{') and 'tts_engine' in content,
          content[:120])
check('退出时的失败被记进日志',
      '退出时停止任务调度器失败' in io.open(log, encoding='utf-8', errors='ignore').read())
check('没有任何错误弹窗', not any(n in ('showerror', 'showwarning') for n, _a in dialogs),
      dialogs)

print('\n[3] 用户点"否"时不能退出')
dialogs[:] = []


def deny(*args, **kwargs):
    dialogs.append(('askyesno', ['deny']))
    return False


mod.messagebox.askyesno = deny
app2_root = mod.tk.Tk()
app2_root.withdraw()
app2 = mod.EpubToMp3App(app2_root)
app2.on_closing()
check('点否后窗口仍存在（未 destroy）', bool(app2_root.winfo_exists()))
check('点否后没有保存设置（不该有副作用）', app2._stopping is False)
try:
    app2_root.destroy()
except mod.tk.TclError:
    pass

# 前两个 Tk 根用完就销毁：[4][5] 各自新建根，避免多解释器互相干扰
try:
    app._teardown()
    root.destroy()
except mod.tk.TclError:
    pass

print('\n[4] 窗口已销毁时仍要能收尾（"初始化期间被关掉"的竞态路径）')
app3_root = mod.tk.Tk()
app3_root.withdraw()
app3 = mod.EpubToMp3App(app3_root)
app3_root.destroy()                     # 模拟：main() 还没走完，窗口就没了

if os.path.exists(cfg):
    os.remove(cfg)

try:
    app3.save_settings()
    check('窗口销毁后 save_settings 不抛异常', True)
except Exception as exc:
    check('窗口销毁后 save_settings 不抛异常', False, repr(exc))
check('窗口销毁后配置仍落盘（退化成落盘已有配置）', os.path.exists(cfg), cfg)

app3._stopping = False
try:
    app3._teardown()
    check('窗口销毁后 _teardown 不抛异常（收尾不碰 Tk）', True)
except Exception as exc:
    check('窗口销毁后 _teardown 不抛异常（收尾不碰 Tk）', False, repr(exc))
check('_teardown 会立起关闭标志', app3._stopping is True)

print('\n[5] main() 启动序列：窗口在初始化期间被销毁必须安静退出')
# 复现用户报的那条 traceback：
#   File "epubReader.py", line 6018, in main
#   _tkinter.TclError: can't invoke "wm" command: application has been destroyed
# 手法：把 center_window 换成"显示完立刻销毁窗口"，让 main() 后续的
# root.update()/attributes() 都撞上已销毁的窗口。
killed = []
orig_center = mod.EpubToMp3App.center_window


def center_and_kill(self):
    self.root.update_idletasks()
    self.root.destroy()
    killed.append(True)


mod.EpubToMp3App.center_window = center_and_kill
mod.messagebox.askyesno = stub('askyesno')
try:
    mod.main()
    check('main() 在窗口被提前销毁时不抛异常', True)
except Exception as exc:
    check('main() 在窗口被提前销毁时不抛异常', False, repr(exc))
finally:
    mod.EpubToMp3App.center_window = orig_center
check('确实走到了"窗口被销毁"这一步', killed == [True])

print('\n[6] TTS 失败必须给出可行动的诊断，并落进应用日志')
# 真实事故：edge-tts 7.2.8 发 Sec-MS-GEC-Version=1-... 被服务端一律 403。
# 当时界面上只有"生成失败: 章节名"，原因被 print 到 stderr —— 而打包成窗口程序后
# stderr 是空的（只有 pythonw.exe 才重定向），于是"请查看日志了解详情"是一句空话。
mod.LAST_TTS_FAILURE = None
fake_403 = Exception("WSServerHandshakeError: 403, message='Invalid response status'")
detail = mod._record_tts_failure(fake_403)
check('403 被翻译成诊断文本', '403' in detail and 'edge-tts' in detail, detail[:80])
check('诊断里给出可执行的解决办法', '升级' in detail and '7.3.1' in detail)
check('失败原因存进 LAST_TTS_FAILURE 供调用方展示',
      mod.LAST_TTS_FAILURE == detail)
log_now = io.open(log, encoding='utf-8', errors='ignore').read()
check('失败原因写进应用日志（窗口模式 stderr 收不到）',
      'TTS 失败' in log_now and '403' in log_now,
      log_now[-200:])
check('非 403 的错误保留原始信息',
      mod._record_tts_failure(RuntimeError('随便一个错')).strip() == '随便一个错')

shutil.rmtree(ROOT, ignore_errors=True)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks, len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
