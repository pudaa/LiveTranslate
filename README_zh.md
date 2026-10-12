# LiveTranslate

[English](README.md) | **中文**

Windows 实时语音字幕工具。捕获系统音频（WASAPI loopback）和可选的麦克风输入，**在 CPU 上**完成语音识别，结果显示在透明悬浮字幕窗口上。视频本身不需要有字幕，机器不需要独显；只要你不想翻译，就完全不需要联网。

适用于看无字幕视频、直播、语音对话等场景——无需修改播放器，全局音频捕获即开即用。

> **本项目是 [TheDeathDragon/LiveTranslate](https://github.com/TheDeathDragon/LiveTranslate) 的 fork。**
> 上游做的是「把外语音频实时翻译成你的语言」；本 fork 改成了另一件事：**静音看没有字幕的视频**——字幕本身就是目的，翻译只是一个可选的叠加层。这个用途上的转变带来了重写的字幕渲染、按应用捕获音频、纯 CPU 的推理链路，以及小得多的分发包。详见 [本 fork 的改动](#本-fork-的改动)。

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)
![Windows](https://img.shields.io/badge/Platform-Windows-0078d4)
![License](https://img.shields.io/badge/License-MIT-green)

## 截图

![LiveTranslate](screenshot/zh.png)

## 安装视频

[![安装演示](https://img.shields.io/badge/Bilibili-安装演示-00A1D6?logo=bilibili)](https://www.bilibili.com/video/BV1K2Awz6Euw) 适用于看外语视频、直播、ASMR等场景，也可以语音输入实时并行翻译多种语音

## 功能特性

- **实时字幕管线**：系统音频 → VAD（ONNX）→ ASR（ONNX，跑在 CPU）→ 字幕显示；翻译可选
- **纯 CPU 可用**：默认引擎 sherpa-onnx + SenseVoice，不需要独立显卡
- **字幕优先**：`captions` 模式零 API 依赖、完全离线；`translate` 模式保留上游的翻译能力
- **按应用捕获**：只录指定进程的声音，其他窗口的声音不会被录进去
- **多 ASR 引擎**：sherpa-onnx（默认，CPU）、faster-whisper、SenseVoice、FunASR Nano、Anime-Whisper
- **远程 ASR**：通过 HTTP 把语音识别放到 GPU 机器上跑 —— 见 [REMOTE_ASR.md](REMOTE_ASR.md)
- **兼容任意 OpenAI 格式 API**：DeepSeek、Grok、Qwen、GPT、Ollama、vLLM 等
- **流式字幕**：说话的同时逐字出现，partial 结果原地替换
- **影院字幕预设**：8 个预设 + 纯净模式，另有 SRT 导出
- **麦克风混音**：可选将麦克风输入混合到系统音频一起识别
- **低延迟 VAD**：32ms 音频块 + Silero VAD（ONNX），自适应静音检测
- **透明悬浮窗**：始终置顶、鼠标穿透、可拖拽，14 种配色主题
- **模型自动管理**：首次启动向导，支持 ModelScope / HuggingFace 双源
- **内置基准测试**：对比翻译模型速度和质量

## 本 fork 的改动

本 fork 与上游共用同一条管线，但用途不同，日常使用体验差别很大。在上游 `190ef02` 之上有 32 个提交。

### 1. 字幕是目的，翻译是可选项

`pipeline.mode` 把这两件事拆开：

| 模式 | 你会看到 | 需要 API Key？ |
|---|---|---|
| `captions` | 识别出的文字**就是**字幕 | 不需要，完全离线 |
| `translate` | 上游行为：原文 + 译文 | 需要 |

`captions` 模式下没有任何数据离开本机。这正是本 fork 的目标场景，也是「纯 CPU 构建」能成立的前提。

### 2. 字幕渲染被重写

上游的悬浮窗用来读**一整行翻译结果**没问题，但要展示**正在说的话**是另一回事：

- **影院预设** —— 4 个悬浮窗 + 4 个独立字幕窗预设，外加隐藏全部边框的「纯净模式」。这是当初 fork 的直接原因。
- **流式字幕** —— 说话的同时逐字出现。partial 结果**原地替换**而不是追加，所以同一句话不会出现两次。
- **排队释放** —— ASR 一次送来一整段，读者无法分辨「新增从哪里开始」。文字先缓冲，再按**阅读单元**逐个放出（中文一字、英文一词）。
- **传送带滚动** —— 整行的位置是**自由推进的量**，不再由文字宽度推导，因此**插入文字不会推动已经在屏幕上的文字**。（更直觉的做法——量出宽度再补偿——试过并回退了三遍：补偿机制无法区分「可见内容变了」和「不可见内容变了」。）
- **矫正标记** —— 识别器改写某个词时，给它加**细分虚线底划线**（标记「哪里改过」），并从 45% 亮度**弱渐入**（标记「刚刚改的」）。矫正刻意不表现成「新文字出现」。
- **陈旧渐隐** —— 4 秒没有新内容，整行在 0.9 秒内淡出，避免长时间没人说话时屏幕上留着一句过期的话。
- **SRT 导出** —— 时间轴取自**捕获音频的时钟**而非墙钟，所以丢帧或暂停捕获都不会让字幕相对视频漂移。

### 3. 只捕获一个应用，而不是整个系统

上游录整个输出设备。如果你只想给某一个视频配字幕、同时另一个窗口在放音乐，这就不够用了。本 fork 使用 **WASAPI 进程回环**（`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`）只捕获指定进程，设置页会告诉你隔离是否真的生效。需要 Windows 10 2004+。

### 4. 跑在 CPU 上

上游构建默认需要 NVIDIA 显卡。本 fork 的默认引擎是 **sherpa-onnx**（ONNX Runtime），完全不依赖 torch：

| | PyTorch + CUDA（上游） | sherpa-onnx int8（本 fork） |
|---|---|---|
| 8 秒片段 | 约 95 ms | 约 215 ms |
| 预算 | 600 ms | 600 ms |
| 独立显卡 | **P0，约 21 W，占用 39%** | **休眠，P8，约 2.7 W** |
| 运行时体积 | 约 2.5 GB（torch + CUDA） | 约 19 MB |

两者跑的是**同一份 SenseVoice-Small 权重**，识别质量不变——数据来自真实课堂录音（见 `doc/02-端侧化与省电调研-2026-10-11.md`）。两点说明：

- **VAD 也换成了 ONNX**，实测分段结果与 torch 版**完全一致**。让整条采集链路都不需要 torch，是这个改动的目的。
- **sherpa-onnx 的 SenseVoice 解码不输出标点**（词表里有标点 token，但贪心 + int8 解码会丢掉）。用一个 CT-Transformer 标点模型补回，每段约 15 ms；可以在设置里关掉。

### 5. 性能与打包

- **FunASR 导入：43 秒 → 18 秒**。上游的 `import funasr` 会急切遍历它全部 432 个子模块，连带拖进 transformers 和 scipy——而 SenseVoice 根本不用它们。
- **纯 CPU 打包**。`build_exe.py` 产出一个单文件 exe（约 200 MB），不需要 torch 也不需要 CUDA；ONNX 模型（约 310 MB）放在 exe 旁边。旧的 PyTorch 版 exe 是 2.8 GB。
- **靠脚本验证，不靠肉眼**。`tools/verify_ui.py` 离屏驱动真实控件并**逐帧推进**字幕动画；`tools/verify_runtime.py` 真的拉起 ASR 子进程；`tools/bench_asr_backends.py` 和 `tools/compare_asr_quality.py` 可以用你自己的音频测延迟与质量。

### 没有改动的部分

管线结构、翻译客户端、设置界面都来自上游。翻译功能照旧可用——随时可以把 `translate` 模式打开。

## 更新日志

查看 [中文更新日志](i18n/CHANGELOG_zh.md) | [English Changelog](i18n/CHANGELOG_en.md)

## 系统要求

- **操作系统**：Windows 10/11（「按应用捕获」需要 Windows 10 2004 及以上）
- **显卡**：**不需要**。默认引擎跑在 CPU 上
- **Python**：3.10–3.12（仅从源码运行时需要）
- **网络**：`captions` 模式完全离线；只有 `translate` 模式需要访问翻译 API

## 快速开始

### 直接运行（推荐）

从 Releases 下载发布包，解压后双击 **`LiveTranslate.exe`**。模型随包提供，首次启动**不需要下载、不需要联网**，也不需要装 Python 或 CUDA。

> 解压时请保持目录结构：`LiveTranslate.exe` 旁边的 `models/` 目录必须一起解压，程序靠它找模型。

### 从源码安装

```bash
git clone https://github.com/TheDeathDragon/LiveTranslate.git
cd LiveTranslate
```

双击 **`install.bat`** 一键安装——脚本会自动：
1. 检测 Python 3.10–3.12（未安装则通过 winget 自动安装）
2. 创建虚拟环境
3. 检测 NVIDIA 显卡，选择 CUDA / CPU 版 PyTorch
4. 安装全部依赖

安装完成后双击 **`start.bat`** 启动。

更新时双击 **`update.bat`**——自动拉取最新代码并更新依赖（未安装 Git 会通过 winget 自动安装）。

<details>
<summary>手动安装</summary>

```bash
python -m venv .venv
.venv\Scripts\activate

# PyTorch（三选一）
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126  # CUDA
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128  # CUDA（RTX 50 系列）
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu    # 仅 CPU

# 依赖
pip install -r requirements.txt

# 启动
.venv\Scripts\python.exe main.py
```

</details>

## 首次使用

1. 弹出设置向导——选择下载源（ModelScope 适合国内，HuggingFace 适合海外）和缓存路径
2. 自动下载 Silero VAD + SenseVoice 模型（约 1GB）
3. 下载完成后进入主界面

## 配置翻译 API

设置 → 翻译标签页：

| 参数 | 示例 |
|------|------|
| API Base | `https://api.deepseek.com/v1` |
| API Key | 你的密钥 |
| Model | `deepseek-chat` |
| 代理 | `none` / `system` / 自定义地址 |

## 架构

```
Audio (WASAPI 32ms) → VAD (Silero) → ASR → LLM Translation → Overlay
         ↑ 可选麦克风混音
```

```
main.py                 主入口，管线编排
livetranslate/          所有代码都在这个包内
├── paths.py            统一路径锚点（项目根 / models / logs ...）
├── audio/
│   ├── capture.py      WASAPI loopback + 麦克风混音
│   └── vad.py          Silero VAD
├── asr/
│   ├── client.py       主进程 worker 管理（spawn + Pipe IPC）
│   ├── worker.py       子进程入口
│   ├── server.py       独立 HTTP ASR 服务（远程 Whisper 用）
│   └── backends/
│       ├── whisper.py      faster-whisper 后端
│       ├── funasr.py       统一 FunASR 模型选择后端
│       ├── sensevoice.py   SenseVoice 后端
│       ├── funasr_nano.py  FunASR Nano 后端
│       ├── anime_whisper.py Anime-Whisper 后端 (日语动画/Galgame)
│       ├── remote.py       远程 Whisper 客户端（见 REMOTE_ASR.md）
│       └── nano_src/       内置的 FunASR-Nano 代码
├── translate/
│   └── translator.py   OpenAI 兼容翻译客户端 (流式/JSON/上下文)
├── output/
│   └── transcript.py   转录落盘
├── ui/
│   ├── overlay.py          PyQt6 透明悬浮窗
│   ├── subtitle_window.py  独立字幕窗（OBS 用，描边文字）
│   ├── subtitle_settings.py 字幕窗设置 UI
│   ├── control_panel.py    设置面板 UI (7 个标签页)
│   ├── dialogs.py          设置向导、下载、模型配置对话框
│   ├── log_window.py       实时日志窗
│   └── i18n.py             界面文案
├── models/
│   └── manager.py      模型下载与缓存管理
└── benchmark.py        翻译基准测试
```

## 致谢

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 基于 CTranslate2 的 Whisper 推理
- [FunASR](https://github.com/modelscope/FunASR) — SenseVoice / Fun-ASR-Nano
- [Anime-Whisper](https://huggingface.co/litagin/anime-whisper) — 日语动画/Galgame 专用 ASR
- [Silero VAD](https://github.com/snakers4/silero-vad) — 语音活动检测

## Star History

<a href="https://www.star-history.com/?repos=TheDeathDragon%2FLiveTranslate&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=TheDeathDragon/LiveTranslate&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=TheDeathDragon/LiveTranslate&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/image?repos=TheDeathDragon/LiveTranslate&type=date&legend=top-left" />
 </picture>
</a>

## 许可证

[MIT License](LICENSE)
