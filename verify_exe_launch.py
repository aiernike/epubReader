# -*- coding: utf-8 -*-
"""打包产物启动冒烟测试：把 dist 真正跑起来，验证"能开、能关、配置落盘"。

跑两轮：

- **第 1 轮（正常路径）**：等 `main()` 走完再关闭 —— 断言窗口出现、标题正确、
  启动即写日志、确认框弹出、进程退出、配置落盘且是合法 JSON。
- **第 2 轮（竞态路径）**：窗口一出现就发 WM_CLOSE —— 复现并回归
  `TclError: can't invoke "wm" command: application has been destroyed`
  （见 `epubReader.py` 的 `main()` 兜底）。断言点是日志里没有"未捕获异常"，
  比抓弹窗稳。

设计要点（都是踩过坑之后加的）：

1. **运行目录可切换**：默认把 `dist\\epubReader`（不含 ffmpeg）复制到脚本同级的
   `_smoke_run/` 再启动。有些受限执行环境不允许"镜像不在本目录树内"的进程读取
   自身文件，此时 PyInstaller bootloader 会报
   `Cannot open PyInstaller archive ...`——那是环境限制，不是产物缺陷。
   用 `--in-place` 可直接跑原始 dist 目录。
2. **区分 产品失败 / 环境受限**：识别出环境受限时退出码为 2，便于跑在 CI 或
   受限沙箱里时不被误判成回归。
3. **必须处理"确认退出"对话框**：应用的 on_closing 恒定弹 askyesno，
   只发 WM_CLOSE 是关不掉的，必须再确认一次。
4. **不能盲点第一个按钮**：Tk 对话框的按钮枚举顺序与默认按钮不一致，
   点成"否"会让应用继续运行，看起来像"关不掉"。

用法：
    python verify_exe_launch.py [--in-place] [--keep] [--close-delay 8]
退出码：0 通过 / 1 失败 / 2 环境受限
"""
import argparse
import ctypes
import ctypes.wintypes
import io
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIST = os.path.join(HERE, 'dist', 'epubReader')
EXE_NAME = 'epubReader.exe'

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
VK_RETURN = 0x0D
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

# 已知的"环境受限"特征（镜像不在允许范围内时 bootloader/加载器给出的报错）
ENV_SIGNATURES = (
    'Cannot open PyInstaller archive',
    'Permission denied',
    'Access is denied',
)

checks = []
failures = []
env_blocked = []


def check(label, cond, detail=''):
    checks.append(label)
    if cond:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s%s' % (label, (' -> %s' % detail) if detail else ''))
        failures.append(label)
    return cond


def is_env_error(text):
    return any(sig.lower() in (text or '').lower() for sig in ENV_SIGNATURES)


def visible_windows(pid):
    """返回该进程所有可见顶层窗口：(hwnd, title, class)"""
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum(hwnd, _l):
        owner = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(128)
            u.GetClassNameW(hwnd, cls, 128)
            found.append((hwnd, buf.value, cls.value))
        return True

    u.EnumWindows(enum, 0)
    return found


def child_widgets(hwnd):
    """返回对话框内部所有控件：(hwnd, 类名, 文本)。用于把 traceback 打出来。"""
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum(c, _l):
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(c, cls, 128)
        n = u.GetWindowTextLengthW(c)
        text = ''
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(c, b, n + 1)
            text = b.value
        out.append((c, cls.value, text))
        return True

    u.EnumChildWindows(hwnd, enum, 0)
    return out


def child_texts(hwnd):
    return [t for _h, _c, t in child_widgets(hwnd) if t]


def confirm_close_dialog(proc, timeout=15):
    """走完"确认退出"对话框。返回 'confirmed' / 'error' / 'gone' / 'timeout'。

    两个踩过的坑：
    - 不能盲点"第一个按钮"：Tk 对话框的按钮枚举顺序与默认按钮不一致，点成
      "否"会让应用继续运行，测试就会卡在"进程没退出"这个假象上。
    - 关闭时若抛未捕获异常，PyInstaller 会弹 "Unhandled exception in script"。
      必须把它跟正常确认框区分开，并把 traceback 打出来，否则会被误判成
      "关闭流程正常"。
    """
    u = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, title, cls in visible_windows(proc.pid):
            if cls != '#32770':      # 只看 Windows 标准对话框
                continue
            widgets = child_widgets(hwnd)
            buttons = [(c, t.replace('&', '')) for c, klass, t in widgets if klass == 'Button']
            print('      对话框: 标题=%r 按钮=%s' % (title, [b for _h, b in buttons]))

            if 'exception' in title.lower() or any('Traceback' in t for _h, _c, t in widgets):
                print('      --- 关闭流程抛出了未捕获异常 ---')
                for _h, _c, t in widgets:
                    if t.strip():
                        print('      %s' % t.strip())
                return 'error'

            target = None
            for h, label in buttons:
                if label.startswith(('是', 'Y', 'y')) or label.lower() in ('yes', 'ok'):
                    target = h
                    break
            if target is None and buttons:
                target = buttons[0][0]
            if target is not None:
                u.SendMessageW(target, BM_CLICK, 0, 0)
                return 'confirmed'
        if proc.poll() is not None:
            return 'gone'
        time.sleep(0.5)
    return 'timeout'


def wait_for_windows(proc, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = visible_windows(proc.pid)
        if wins:
            return wins
        if proc.poll() is not None:
            return []
        time.sleep(0.5)
    return []


def prepare_run_dir(copy, skip_ffmpeg=False):
    """准备运行环境，返回 (scratch_root, exe_path)。

    `scratch_root` **只用来放隔离的 profile/temp，并在结束时被整目录删掉**。
    早期版本在 `--in-place` 时把 scratch_root 直接指向 `dist`，收尾的 rmtree 就会
    顺手把整个打包产物删掉 —— 一个"验证脚本毁掉被验证对象"的坑，已改为恒定用
    `_smoke_run/`，`--in-place` 只影响 exe 取自哪里。
    """
    scratch = os.path.join(HERE, '_smoke_run')
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    if not copy:
        return scratch, os.path.join(SRC_DIST, EXE_NAME)

    app_dir = os.path.join(scratch, 'app')
    os.makedirs(app_dir)
    for name in os.listdir(SRC_DIST):
        if name == 'ffmpeg' and skip_ffmpeg:
            continue
        src = os.path.join(SRC_DIST, name)
        dst = os.path.join(app_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return scratch, os.path.join(app_dir, EXE_NAME)


def launch_app(exe, env, attempts):
    """启动产物并等主窗口出现。

    返回 (proc, wins, blocked_detail, fatal_detail)：

    - `wins` 非空  —— 正常起来，`wins[0]` 是主窗口；
    - `blocked_detail` 非空 —— 被执行环境限制阻断（不是产物缺陷，退出码 2）；
    - `fatal_detail` 非空 —— bootloader 报了真实错误（产物缺陷，退出码 1）。

    启动重试是必要的：某些安全软件会短暂占用刚写出的 exe，导致 bootloader
    读不到自身归档，属于环境噪声，重试几次再下结论。
    """
    proc = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print('      第 %d 次启动尝试…' % attempt)
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env)
        wins = wait_for_windows(proc, 60)
        if wins:
            hwnd, _title, cls = wins[0]
            if cls == '#32770':          # Windows 标准对话框 = 出错弹窗
                detail = ' / '.join(child_texts(hwnd))
                if is_env_error(detail) and attempt < attempts:
                    print('      启动被环境限制阻断，稍后重试: %s' % detail.strip()[:120])
                    if proc.poll() is None:
                        proc.terminate()
                    time.sleep(3)
                    continue
                if is_env_error(detail):
                    print('  ENV   启动被环境限制阻断（非产物缺陷）: %s' % detail.strip())
                    if proc.poll() is None:
                        proc.terminate()
                    return proc, [], detail.strip(), ''
                if proc.poll() is None:
                    proc.terminate()
                return proc, [], '', detail
            return proc, wins, '', ''
        if proc.poll() is not None and attempt < attempts:
            time.sleep(2)
            continue
        break
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return proc, [], '', ''


def send_close(pid):
    """给该进程的所有可见顶层窗口发 WM_CLOSE。"""
    sent = 0
    for h, _t, _c in visible_windows(pid):
        ctypes.windll.user32.PostMessageW(h, WM_CLOSE, 0, 0)
        sent += 1
    return sent


def wait_exit(proc, timeout):
    """等进程退出，返回是否真的退出（超时会 terminate）。"""
    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
    if proc.poll() is None:
        proc.terminate()
        time.sleep(1)
        return False
    return True


def finish(run_root, keep, code, exe=None):
    _finish(keep, run_root, exe)
    print('-' * 60)
    print('%d checks, %d failures' % (len(checks), len(failures)))
    for name in failures:
        print('  FAILED: %s' % name)
    return code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-place', action='store_true', help='直接在 dist 目录里运行，不复制')
    ap.add_argument('--keep', action='store_true', help='保留临时运行目录')
    ap.add_argument('--launch-attempts', type=int, default=3,
                    help='启动重试次数（默认 3：安全软件占用刚写出的 exe 时会短暂失败）')
    ap.add_argument('--close-delay', type=float, default=8.0,
                    help='出现窗口后等多少秒再发 WM_CLOSE（默认 8：窗口先于 main() 显示，'
                         '立刻关会命中"初始化期间被关闭"的竞态路径，那不该是日常用例）')
    ap.add_argument('--no-ffmpeg', action='store_true',
                    help='复制时跳过 190MB 的 ffmpeg（更快，但应用会走"缺少 FFmpeg"分支）')
    args = ap.parse_args()

    if not os.path.exists(os.path.join(SRC_DIST, EXE_NAME)):
        print('未找到打包产物：%s' % os.path.join(SRC_DIST, EXE_NAME))
        print('请先运行 build_exe.py 打包。')
        return 2

    run_root, exe = prepare_run_dir(copy=not args.in_place, skip_ffmpeg=args.no_ffmpeg)
    profile = os.path.join(run_root, 'profile')
    temp = os.path.join(run_root, 'temp')
    for p in (os.path.join(profile, 'Roaming'), os.path.join(profile, 'Local'), temp):
        os.makedirs(p, exist_ok=True)

    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env.update({
        'APPDATA': os.path.join(profile, 'Roaming'),
        'LOCALAPPDATA': os.path.join(profile, 'Local'),
        'TEMP': temp,
        'TMP': temp,
        'USERPROFILE': profile,
    })

    print('可执行文件: %s' % exe)
    print('运行目录  : %s' % run_root)
    print('-' * 60)

    log_path = os.path.join(profile, 'Roaming', 'epubReader', 'epubReader.log')
    cfg_path = os.path.join(profile, 'Roaming', 'epubReader', 'config.json')

    def read_log():
        if not os.path.exists(log_path):
            return ''
        with io.open(log_path, encoding='utf-8', errors='ignore') as fh:
            return fh.read()

    # ================= 第 1 轮：正常启动 + 正常关闭 =================
    print('### 第 1 轮：正常启动、正常关闭（等 %.1fs 让 main() 走完）' % args.close_delay)
    proc, wins, blocked, fatal = launch_app(exe, env, args.launch_attempts)
    if fatal:
        check('bootloader 未报致命错误', False, fatal)
        return finish(run_root, args.keep, 1, exe)
    if blocked:
        env_blocked.append(blocked)
        return finish(run_root, args.keep, 2, exe)
    if not wins:
        check('进程启动并显示窗口', False, '退出码=%s' % (proc.poll() if proc else 'N/A'))
        return finish(run_root, args.keep, 1, exe)

    _hwnd, title, _cls = wins[0]
    check('bootloader 未报致命错误', True)
    check('主窗口出现', bool(title), title)
    check('窗口标题是应用本体', 'epubReader' in title, title)

    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(log_path):
        time.sleep(0.5)
    check('启动即写出日志文件', os.path.exists(log_path), log_path)

    # 关闭流程：WM_CLOSE -> "确认退出" 对话框 -> 点"是"
    time.sleep(args.close_delay)
    send_close(proc.pid)
    time.sleep(1.5)
    confirmed = confirm_close_dialog(proc, 15)
    check('关闭流程能真正退出进程', wait_exit(proc, 30),
          '退出码=%s' % proc.poll())
    check('关闭流程没有抛未捕获异常', confirmed != 'error')
    check('退出流程弹出了确认对话框', confirmed == 'confirmed', confirmed)

    if check('正常关闭后写出配置文件', os.path.exists(cfg_path), cfg_path):
        try:
            with io.open(cfg_path, encoding='utf-8') as fh:
                cfg = json.load(fh)
            check('配置文件是合法 JSON', isinstance(cfg, dict))
            check('配置包含设置项 tts_engine', 'tts_engine' in cfg, str(list(cfg))[:120])
        except Exception as exc:
            check('配置文件可解析', False, repr(exc))

    log_after_round1 = read_log()

    # ================= 第 2 轮：窗口刚出现就关（竞态路径） =================
    #
    # 这一轮**故意**不等 main() 走完就发 WM_CLOSE，复现用户报过来的那条 traceback：
    #   File "epubReader.py", line 6018, in main
    #   tkinter\__init__.py, line 1976, in wm_attributes
    #   _tkinter.TclError: can't invoke "wm" command: application has been destroyed
    # 成因：Tk 窗口在 main() 走完之前就已经显示，此时点关闭，Tk 默认行为直接销毁窗口，
    # 其后的 root.update()/attributes() 全部抛 TclError，逃到顶层后 PyInstaller 弹
    # "Failed to execute script ... unhandled exception"。
    #
    # 断言点在日志而不是弹窗：顶层异常一定会经 sys.excepthook 落盘并带上
    # "未捕获异常" 字样，比抓弹窗更稳（弹窗会被关掉、被其他窗口盖住）。
    print('### 第 2 轮：窗口一出现立刻关闭（复现 main() 启动竞态）')
    proc2, wins2, blocked2, fatal2 = launch_app(exe, env, args.launch_attempts)
    if fatal2 or blocked2 or not wins2:
        # 第 1 轮已经证明能起来，这里失败就是真实回归
        check('竞态关闭：第二轮仍能启动', False,
              fatal2 or blocked2 or ('退出码=%s' % (proc2.poll() if proc2 else 'N/A')))
    else:
        sent = send_close(proc2.pid)
        race = confirm_close_dialog(proc2, 20)
        # race = 'gone' 表示 WM_CLOSE 在 root.protocol(WM_DELETE_WINDOW) 装上之前就到了，
        # Tk 默认行为直接销毁窗口 —— 这才是真正的竞态路径，也是用户报的那条 traceback 的现场。
        # 'confirmed' 说明启动太快、没赶上竞态，这一轮的覆盖就变弱了，必须显式提示。
        print('      竞态关闭结果: %s（向 %d 个窗口发了 WM_CLOSE；%s）' % (
            race, sent, {'gone': '命中竞态：窗口先于关闭处理器被销毁',
                   'confirmed': '未赶上竞态：启动太快，关闭处理器已就绪',
                   'timeout': '异常：既无对话框也没退出',
                   'error': '复现了未捕获异常'}.get(race, '')))
        check('竞态关闭：没有未捕获异常弹窗', race != 'error', race)
        check('竞态关闭：进程没有被挂住', race != 'timeout', race)
        check('竞态关闭：进程能正常退出', wait_exit(proc2, 25),
              '退出码=%s' % proc2.poll())
        tail = read_log()[len(log_after_round1):]
        check('竞态关闭：日志里没有未捕获异常', '未捕获异常' not in tail,
              tail.strip()[-300:])

    return finish(run_root, args.keep, 1 if failures else 0, exe)


def _finish(keep, run_root, exe=None):
    if keep:
        print('保留运行目录: %s' % run_root)
        return
    # 保险：verify 脚本绝不允许删掉打包产物所在的目录
    if exe and os.path.abspath(run_root) == os.path.dirname(os.path.abspath(exe)):
        print('跳过清理（运行目录就是产物目录）: %s' % run_root)
        return
    shutil.rmtree(run_root, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
