# epubReader
# epubReader

> EPUB电子书阅读器和有声书生成工具

[![Version](https://img.shields.io/badge/version-0.01-blue.svg)](https://github.com/yourusername/epubReader)
[![Python](https://img.shields.io/badge/python-3.8+-brightgreen.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 功能特性

### 📚 EPUB阅读
- ✅ 打开并阅读EPUB格式电子书
- ✅ 显示书籍封面、元数据信息
- ✅ 章节列表浏览
- ✅ 章节内容查看和编辑
- ✅ 章节删除管理

### 🎧 有声书生成
- ✅ 单章节转换为MP3音频
- ✅ 批量转换所有章节
- ✅ 自动生成LRC同步歌词
- ✅ 嵌入完整ID3标签（封面、标题、艺术家、专辑、歌词）
- ✅ 支持M4B有声书格式（包含章节标记）

### 🔊 TTS语音
- ✅ 使用Microsoft Edge TTS引擎
- ✅ 支持多种中英文语音
- ✅ 语音试听功能
- ✅ 自定义语音参数

### ⚙️ 智能安装
- ✅ 自动检测FFmpeg
- ✅ 一键自动下载安装
- ✅ 支持本地ZIP文件安装
- ✅ 多镜像源自动切换

## 截图预览

### 主界面
![主界面](docs/screenshot-main.png)

### 章节阅读
![章节阅读](docs/screenshot-reader.png)

### 设置界面
![设置界面](docs/screenshot-settings.png)

## 安装说明

### 环境要求
- Python 3.8+
- Windows 10/11（推荐）

### 依赖安装

```bash
pip install -r requirements.txt
```

### 运行程序

```bash
python "epub reader.py"
```

## 使用指南

### 1. 打开EPUB文件
1. 点击"选择电子书"按钮
2. 选择EPUB文件
3. 程序自动解析并显示书籍信息

### 2. 生成有声书

#### MP3格式（分章节）
1. 在"设置与自定义"选择输出格式为"MP3"
2. 选择语音、配置参数
3. 点击"生成电子书"
4. 等待转换完成

#### M4B格式（单文件）
1. 首次使用需安装FFmpeg（程序会自动提示）
2. 选择输出格式为"M4B"
3. 点击"生成电子书"
4. 程序自动合并章节并嵌入章节信息

### 3. 编辑章节
1. 在章节列表选择章节
2. 在右侧文本框编辑内容
3. 点击"保存修改"

### 4. 删除章节
1. 在章节列表右键点击章节
2. 选择"删除章节"
3. 确认删除

## 技术栈

- **GUI框架**: Tkinter
- **EPUB解析**: ebooklib, BeautifulSoup4
- **图像处理**: Pillow
- **TTS引擎**: edge-tts
- **音频处理**: FFmpeg, mutagen
- **异步处理**: asyncio
- **多线程**: threading

## 配置说明

### 默认配置

```python
DEFAULT_VOICE = "zh-CN-YunjianNeural"  # 默认语音
DEFAULT_CHARS_PER_LINE = 15  # LRC每行字数
MAX_RETRIES = 3  # TTS最大重试次数
DOWNLOAD_TIMEOUT = 30  # 下载超时（秒）
```

### 输出目录
有声书默认输出到EPUB文件同目录的`有声书目录_书名`文件夹

### FFmpeg安装位置
- Windows: `%APPDATA%\epubReader\ffmpeg`

## 常见问题

### Q: 下载FFmpeg很慢或失败？
**A**: 程序支持多个镜像源自动切换，如果都失败可以：
1. 点击"从本地ZIP安装"
2. 手动下载FFmpeg ZIP包
3. 选择下载的文件安装

### Q: 支持哪些语音？
**A**: 支持Microsoft Edge TTS的所有语音，包括：
- zh-CN-YunjianNeural（男声）
- zh-CN-XiaoxiaoNeural（女声）
- zh-CN-YunxiNeural（男童声）
- 更多英文和其他语言语音

### Q: M4B文件无法播放？
**A**: 确保：
1. 使用支持M4B的播放器（苹果图书、iTunes、Smart AudioBook Player等）
2. FFmpeg已正确安装
3. 转换过程没有错误

### Q: 如何调整音质？
**A**: 当前使用AAC 64kbps编码（适合语音），如需调整可修改代码中的参数

## 开发计划

- [ ] 支持更多电子书格式（AZW3、MOBI）
- [ ] 添加更多TTS引擎
- [ ] 支持GPU加速
- [ ] 批量处理多本书
- [ ] 云端TTS服务集成
- [ ] 音频增强功能

## 贡献指南

欢迎提交Issue和Pull Request！

1. Fork本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 提交Pull Request

## 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

## 致谢

- [ebooklib](https://github.com/aerkalov/ebooklib) - EPUB解析库
- [edge-tts](https://github.com/rany2/edge-tts) - Microsoft Edge TTS
- [FFmpeg](https://ffmpeg.org/) - 音视频处理
- [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) - HTML解析

## 联系方式

- 提交Issue: [GitHub Issues](https://github.com/yourusername/epubReader/issues)
- 邮箱: your.email@example.com

## 更新日志

### v0.01 (2026-01-02)
- 🎉 首次发布
- ✨ EPUB阅读功能
- ✨ MP3有声书生成
- ✨ M4B有声书格式支持
- ✨ 章节信息嵌入
- ✨ FFmpeg自动安装
- ✨ 多镜像源下载

---

**如果觉得这个项目对你有帮助，请给个 ⭐ Star！**
#
