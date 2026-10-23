# -*- coding: utf-8 -*-
"""针对 TaskScheduler 的行为测试（通过桩模块完整导入真实文件）。

阶段 2-A 的验收测试：并发上限、取消、超时、关闭、进度转发、异常隔离。
运行前会用桩补齐 PIL/bs4/ebooklib/mutagen/requests，因此不需要联网或 GUI。
"""
import importlib.util
import io
import os
import sys
import threading
import time
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else r'D:\SynologyDrive\图片\epub reader.py'


def install_stubs():
    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class _Any(object):
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return _Any()

        def __getattr__(self, item):
            return _Any()

    stub('PIL')
    stub('PIL.Image', open=lambda *a, **k: _Any(),
         Resampling=types.SimpleNamespace(LANCZOS=1))
    stub('PIL.ImageTk', PhotoImage=_Any)
    stub('bs4', BeautifulSoup=_Any)
    stub('ebooklib', ITEM_COVER='cover', ITEM_DOCUMENT='doc')
    stub('ebooklib.epub', read_epub=lambda *a, **k: _Any())
    stub('mutagen')
    stub('mutagen.mp3', MP3=_Any)
    stub('mutagen.id3', ID3=_Any, APIC=_Any, TIT2=_Any, TPE1=_Any,
         TALB=_Any, USLT=_Any)
    stub('requests', Session=types.SimpleNamespace(merge_environment_settings=_Any),
         get=_Any,
         exceptions=types.SimpleNamespace(Timeout=Exception, RequestException=Exception))


def load_module():
    install_stubs()
    spec = importlib.util.spec_from_file_location('epub_reader', SRC)
    module = importlib.util.module_from_spec(spec)
    sys.modules['epub_reader'] = module
    spec.loader.exec_module(module)
    return module


mod = load_module()
Task = mod.Task
TaskType = mod.TaskType

failures = []
checks = [0]


def check(label, cond, detail=''):
    checks[0] += 1
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + ((' -> %s' % (detail,)) if not cond else ''))
    if not cond:
        failures.append(label)


def drain(scheduler, timeout=10.0):
    """模拟 GUI 的 process_messages：把结果队列里的回调取出来执行。

    调度器只负责投递 (kind, callback, args)，真正的执行在主线程。
    """
    deadline = time.time() + timeout
    handled = 0
    while time.time() < deadline:
        result = scheduler.get_result()
        if result:
            kind, callback, args = result
            if callback:
                try:
                    callback(*args)
                except Exception as exc:
                    print('callback raised: %r' % (exc,))
                    raise
            handled += 1
        else:
            time.sleep(0.02)
    return handled


def wait_for(predicate, timeout=5.0, scheduler=None):
    """轮询等待条件成立，同时把回调泵出去"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if scheduler is not None:
            result = scheduler.get_result()
            while result:
                kind, callback, args = result
                if callback:
                    callback(*args)
                result = scheduler.get_result()
        if predicate():
            return True
        time.sleep(0.02)
    return False


print('\n[1] 构造与启动')
scheduler = mod.TaskScheduler(max_workers=2)
scheduler.start()
check('start() 后 worker 线程存活',
      any(t.name.startswith('TaskScheduler') for t in threading.enumerate()),
      [t.name for t in threading.enumerate()])

print('\n[2] 并发上限')
active = [0]
peak = [0]
lock = threading.Lock()
finished = [0]


def slow_task():
    with lock:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
    time.sleep(0.4)
    with lock:
        active[0] -= 1
        finished[0] += 1
    return 'ok'


for _ in range(4):
    scheduler.submit_task(Task(
        task_id=os.urandom(8).hex(), task_type=TaskType.GENERAL,
        func=slow_task, callback=lambda r: None))

wait_for(lambda: finished[0] == 4, timeout=8, scheduler=scheduler)
check('4 个任务全部完成', finished[0] == 4, finished[0])
check('任务并发执行（峰值 > 1，说明没有串行）', peak[0] >= 2, 'peak=%d' % peak[0])
check('并发数不超过 max_workers=2', peak[0] <= 2, 'peak=%d' % peak[0])

print('\n[3] 超时')
timed_out = []
scheduler.submit_task(Task(
    task_id='timeout-case', task_type=TaskType.GENERAL,
    func=lambda: time.sleep(5), timeout=0.5,
    error_callback=lambda e: timed_out.append(e)))
wait_for(lambda: len(timed_out) == 1, timeout=4, scheduler=scheduler)
check('超时任务触发 error_callback', len(timed_out) == 1, timed_out)
check('超时异常可读且为 TimeoutError',
      bool(timed_out) and isinstance(timed_out[0], TimeoutError) and '超时' in str(timed_out[0]),
      timed_out)

print('\n[4] 任务异常不会杀死 worker')
errors = []
scheduler.submit_task(Task(
    task_id='boom', task_type=TaskType.GENERAL,
    func=lambda: (_ for _ in ()).throw(ValueError('boom')),
    error_callback=lambda e: errors.append(e)))
wait_for(lambda: len(errors) == 1, timeout=4, scheduler=scheduler)
alive_after_error = any(t.name.startswith('TaskScheduler') for t in threading.enumerate())
check('异常任务触发 error_callback', len(errors) == 1, errors)
check('异常后 worker 线程仍在运行', alive_after_error)
after_ok = []
scheduler.submit_task(Task(
    task_id='after-boom', task_type=TaskType.GENERAL,
    func=lambda: 'still works', callback=lambda r: after_ok.append(r)))
wait_for(lambda: after_ok == ['still works'], timeout=4, scheduler=scheduler)
check('异常之后仍能继续处理新任务', after_ok == ['still works'], after_ok)

print('\n[5] 进度回调转发')
progress = []
scheduler.submit_task(Task(
    task_id='progress-case', task_type=TaskType.GENERAL,
    func=lambda progress_callback=None: [progress_callback(i, 3) for i in range(1, 4)],
    progress_callback=lambda *a: progress.append(a)))
wait_for(lambda: len(progress) == 3, timeout=4, scheduler=scheduler)
check('progress_callback 被注入并由主线程执行', progress == [(1, 3), (2, 3), (3, 3)], progress)

print('\n[6] 取消')
cancel_started = threading.Event()
cancel_hits = []
cancel_responded = threading.Event()


def long_running(cancel_event=None):
    """声明 cancel_event 参数，验证调度器会自动注入并支持协作式取消"""
    cancel_started.set()
    for _ in range(200):
        if cancel_event is not None and cancel_event.is_set():
            cancel_hits.append('cancelled')
            cancel_responded.set()
            raise RuntimeError('cancelled by user')
        time.sleep(0.05)
    return 'finished'


cancel_id = 'cancel-me'
scheduler.submit_task(Task(
    task_id=cancel_id, task_type=TaskType.GENERAL,
    func=long_running, cancellable=True))
cancel_started.wait(2.0)
check('任务已开始执行', cancel_started.is_set())
cancel_ok = scheduler.cancel_task(cancel_id)
check('cancel_task 返回 True', cancel_ok is True)
check('is_task_cancelled 反映状态', scheduler.is_task_cancelled(cancel_id) is True)
check('cancel_event 被注入且已置位',
      scheduler.get_cancel_event(cancel_id) is not None
      and scheduler.get_cancel_event(cancel_id).is_set())
check('长任务响应取消并抛出 TaskCancelled', cancel_responded.wait(2.0), cancel_hits)
time.sleep(0.4)

print('\n[7] 关闭')
stop_start = time.time()
scheduler.stop()
elapsed = time.time() - stop_start
check('stop() 不被阻塞（< 3s）', elapsed < 3.0, '%.2fs' % elapsed)
# 前面所有任务都是短任务；最多再等一个 5 秒的超时 case 收尾
deadline = time.time() + 6.0
while time.time() < deadline:
    if not [t for t in threading.enumerate()
            if t.name.startswith('TaskScheduler') and t.is_alive()]:
        break
    time.sleep(0.1)
leftovers = [t.name for t in threading.enumerate()
             if t.name.startswith('TaskScheduler') and t.is_alive()]
check('关闭后调度器线程全部退出（含线程池）', not leftovers, leftovers)

print('\n[8] 关闭语义：新提交被拒绝 + 不再有非守护线程')
scheduler2 = mod.TaskScheduler(max_workers=3)
scheduler2.start()
done2 = []


def quick():
    time.sleep(0.05)
    return 'q'


for _ in range(3):
    scheduler2.submit_task(Task(
        task_id=os.urandom(6).hex(), task_type=TaskType.GENERAL,
        func=quick, callback=lambda r: done2.append(r)))
# 等任务真正跑完再关闭（不制造"正在执行的长任务"这种特例）
wait_for(lambda: len(done2) == 3, timeout=5, scheduler=scheduler2)
before = [t.name for t in threading.enumerate() if not t.daemon]
scheduler2.stop()
deadline = time.time() + 5.0
while time.time() < deadline:
    if not [t for t in threading.enumerate() if t.name.startswith('TaskScheduler')]:
        break
    time.sleep(0.1)
after = [t.name for t in threading.enumerate() if not t.daemon]
check('stop() 后不再新增非守护线程', len(after) <= len(before),
      'before=%s after=%s' % (before, after))
left2 = [t.name for t in threading.enumerate() if t.name.startswith('TaskScheduler')]
check('stop() 后调度器线程全部退出', not left2, left2)
rejected = False
try:
    scheduler2.submit_task(Task(task_id='late', task_type=TaskType.GENERAL,
                                func=lambda: None))
except RuntimeError:
    rejected = True
check('关闭后拒绝新任务（不再静默挂起）', rejected)

print('\n' + '=' * 60)
print('%d checks, %d failures' % (checks[0], len(failures)))
for name in failures:
    print('  FAILED: %s' % name)
sys.exit(1 if failures else 0)
