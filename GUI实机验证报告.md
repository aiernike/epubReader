# GUI 实机验证报告

- 目标文件：`D:\SynologyDrive\图片\epub reader.py`（就地修改）
- 验证方式：**真实依赖 + 真实 Tk 窗口 + 真实 edge-tts + 真实 FFmpeg**
- 结果：**71 项断言全部通过（0 失败）**

## 一、环境搭建

系统 Python 3.8.8 缺少多个依赖，且直连 pypi.org 超时。处理方式：

1. 建项目专用虚拟环境 `.venv`（不污染系统 Python）
2. 使用清华 PyPI 镜像；`lxml` 因 pip 下载停滞，改为直接下载 wheel 后用 `--no-index` 本地安装
3. FFmpeg：`gyan.dev` 与 `github.com` 直连不可用，改用 `gh-proxy.com` 镜像下载 BtbN 构建（190.9 MB / 7.7 秒），解压到 `ffmpeg_local/`

已安装：pillow 10.4.0、beautifulsoup4 4.12.3、lxml 5.2.2、ebooklib 0.18、mutagen 1.47.0、requests 2.31.0、edge-tts 7.2.7、aiohttp 3.10.11

## 二、验证方式

`test_gui_integration.py` 构建**真实的 `EpubToMp3App` 窗口**，用真实 `edge_tts` 合成、真实 FFmpeg 合并，逐项检查状态与产物：

- Tk 的 `update`/`after` 必须在主线程且事件循环已启动时调用，因此测试主体经 `root.after` 调度进 `mainloop`，
  由应用自己的 `process_messages` 在同一事件循环里泵回调——这就是真实运行方式。
- 交互式对话框（askyesno/showinfo/showerror）被替换为记录器，既能自动应答，也用于断言"全程没有错误弹窗"。
- 配置目录指向临时位置，避免污染真实 `%APPDATA%`。

## 三、验证结果

| 阶段 | 断言数 | 结果 |
| --- | --- | --- |
| [1] 构建窗口（标题/状态栏/调度器/临时目录/配置/语音列表） | 7 | 全部 PASS |
| [2] 载入 EPUB（元数据/章节/封面/列表/选中/按钮/输出目录/预计耗时） | 15 | 全部 PASS |
| [3] 章节内容显示与编辑保存 | 2 | 全部 PASS |
| [4] 批量生成（真实 edge-tts，MP3+LRC） | 6 | 全部 PASS |
| [5] 产物校验（mutagen 解析 + ID3 + 封面 + 歌词） | 16 | 全部 PASS |
| [6] 单选章节转有声书 | 3 | 全部 PASS |
| [7] TXT 批量转换 | 2 | 全部 PASS |
| [7.5] M4B 合并（真实 ffmpeg + ffprobe 断言） | 12 | 全部 PASS |
| [8] 关闭流程（保存设置/清理临时目录/日志/无错误弹窗） | 8 | 全部 PASS |

### 关键实测证据

- **章节列表、书名、封面都正确显示**，默认选中第一章，输出目录自动创建为"有声书目录_<书名>"
- 3 章 MP3 命名 `001-第一章 起点.mp3` … `003-第三章 终点.mp3`，与章节一一对应
- 每个 MP3：时长 > 0、`TIT2/TPE1/TALB` 齐全、**封面为 `image/png`**（证明封面格式探测正确）、USLT 歌词存在
- **M4B：ffprobe 报告 4 个章节、4 个输入 4 个章节**（`-map_chapters` 缺失时这里会是 0）
- M4B 含音频流 + `attached_pic` 封面流，总时长 ≈ 各章之和（10.78+10.20+8.42+10.20）
- M4B 书名元数据写入成功；"中止合并"返回失败且**不产生输出文件**
- 关闭后配置 JSON 保存了语音/格式/并发数/歌词字数，实例临时目录被清理，日志文件生成

### 独立 M4B 实测（`test_m4b_real.py`，16 项断言）

用 ffmpeg 的 `sine` 生成器现场合成 3 个 3 秒 MP3，不依赖网络：

- ffprobe 报告 **3/3 章节**，章节标题正确写入，起点 0 / 3 / 6 秒单调
- 封面 `attached_pic` 就位，总时长 ≈ 9 秒，书名元数据正确
- 取消不产生产物；空输入被拒绝；无残留临时目录

## 四、本轮实机验证发现并修复的真实 bug

### 4.1 封面异常会中断后续所有显示逻辑

原实现把封面缩略图与 IMAGE 打开放在同一个 `try` 里，而该 `try` 之外的"章节列表 / 导出按钮 /
输出目录 / 预计耗时"没有独立错误边界。当封面数据损坏或格式异常时：

- `Image.open` 抛异常 → 被 catch 打印一行日志
- 但**后面的章节列表填充、按钮启用、输出目录设置全部被跳过**
- 界面表现成"书读进来了但什么都没有"，用户无从判断原因

本轮测试用一张手工构造、CRC 错误的 PNG 触发到这条路径（Pillow 拒绝解析）才暴露出来。

修复：把封面缩略图与"写封面临时文件"各自包进独立 `try/except`，失败时只在封面区域提示
"封面无法显示"，其余显示逻辑照常执行。

### 4.2 单章转换每次都在系统临时目录留下永久残留

`convert_chapter_to_mp3` 会 `tempfile.mkdtemp(prefix='epubReader_chapter_')` 并把章节写成 txt，
但这个目录从未被清理、也没有任何下游代码读它。测试跑若干次后系统临时目录里积累了 7 个
`epubReader_chapter_*`（只有进程被强杀时才可能被系统清理）。

修复：删掉这段无用代码（txt 文件本身也没有任何引用）。

### 4.3 日志只在 `main()` 里初始化

从其他入口（含被测试直接构造）启动应用时没有日志文件。修复：在 `EpubToMp3App.__init__`
中就调用 `setup_logging()`，保证任何入口都有日志。

### 4.4 测试自身的三个问题（非产品缺陷，但会造成假失败）

- 用后台线程调用 Tk `update()` → `RuntimeError: main thread is not in main loop`。
  改为经 `root.after` 把测试主体调度进 `mainloop`，由应用自己的消息泵驱动。
- 断言"临时分块目录已清理"时用全局存在性判断，被其他套件/异常终止留下的同名目录误伤。
  改为只统计本次运行**新增**的目录。
- 测试封面 PNG 的 CRC 写错，导致 Pillow 无法解析——反而因此发现了 4.1。

## 五、最终验证结果（9 个套件）

```
语法检查（内存编译）                        OK
test_pure_logic.py       PASS   46 checks, 0 failures
test_scheduler.py        PASS   20 checks, 0 failures
test_tempfiles.py        PASS   16 checks, 0 failures
test_m4b.py              PASS   20 checks, 0 failures
test_settings.py         PASS   32 checks, 0 failures
import_check.py          PASS   IMPORT OK
test_m4b_real.py         PASS   16 checks, 0 failures   （真实 ffmpeg + ffprobe）
test_e2e_tts.py          PASS   10 checks, 0 failures   （真实 edge_tts）
test_gui_integration.py  PASS   71 checks, 0 failures   （真实 Tk 窗口走完 8 个流程）
====================================================================
累计断言: 231   失败套件: 0
```

## 六、复现命令

```powershell
$py = "C:\Users\ttlut\Documents\deepseek-harness\default-workspace\epub-reader-refactor\.venv\Scripts\python.exe"
cd "C:\Users\ttlut\Documents\deepseek-harness\default-workspace\epub-reader-refactor"

# 全部套件（含 GUI 实机 + 真实 FFmpeg + 真实 edge-tts）
& $py run_all_tests.py "D:\SynologyDrive\图片\epub reader.py"

# 只跑快的那些（跳过联网/带界面的两套）
& $py run_all_tests.py "D:\SynologyDrive\图片\epub reader.py" --fast

# 只跑 GUI 实机验证
& $py -u test_gui_integration.py

# 只跑真实 FFmpeg 的 M4B 验证
& $py test_m4b_real.py
```

## 七、仍未覆盖的部分

1. **仅验证了自动化可驱动的路径**：手动拖动分栏、右键菜单（上移/下移/删除/导出）、Alt+↑/↓ 快捷键、
   FFmpeg 自动下载安装对话框、试听播放（需音频设备）未逐一人工点击。
2. **试听功能**需要 `pygame`（未安装）与实际音频设备，未验证播放；生成试听音频的代码路径已由 TTS 流程间接覆盖。
3. **`pyttsx3` 引擎**未装（需要 Windows SAPI + FFmpeg 转码），其语音枚举走后台线程的路径未实测，
   但该分支已由 `test_settings.py` 的 `STATIC_VOICES` 断言覆盖。
4. **`gtts` 引擎**需要能访问 Google，未实测。
5. **大书压力测试**未做：本次用 3 章短文本，未验证 100+ 章、单章数万字的长时间批量稳定性。
6. **FFmpeg 安装路径**：本次验证用的 ffmpeg 位于 `epub-reader-refactor/ffmpeg_local/`，
   应用默认仍会在首次使用时下载到 `%APPDATA%\epubReader\ffmpeg`（未替你改动该默认行为）。

