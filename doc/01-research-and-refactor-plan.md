# LiveTranslate 调研报告与改造方案

> 基线：`TheDeathDragon/LiveTranslate` @ `190ef027f38e577b0d2ecd06058ff6cbd236496b`（2026-08-17）
> Fork：`pudaa/LiveTranslate` ｜ 本地：`D:/PersonApps/LiveTranslate`
> 许可：MIT ｜ 平台：Windows 10/11 ｜ Python 3.10–3.12

---

## 0. 结论先行

三句话：

1. **ASR 与翻译本来就是分离的模块**，但**管线编排把它们焊死了**——`main.py` 里两处 `if source_lang == target_lang: ... else: 提交翻译`，没有开关。**"翻译可关闭"是本项目最核心的解耦点**，也是老大场景的直接命中项。
2. **SenseVoice 已经是默认引擎**（`config.yaml` 里 `asr_engine: funasr` + `funasr_model: sensevoice-small`）。老大提的第 5 条在功能上**基本已满足**，要补的是"注册表化 + 语言码补齐"。
3. **目录确实全平铺**（33 个 `.py` 在根目录），但**代码质量不差**——`CLAUDE.md` 写得比大多数商业项目都细，ASR worker 已有 RSS 回收、设置原子写、设备热插拔重连。重构风险主要在 `main.py`(94KB) 和 `control_panel.py`(65KB) 这两个巨型文件。

---

## 1. 项目基线

### 1.1 目录现状（重构前）

```
LiveTranslate/                     33 个 .py 全部平铺在根目录，9.6MB
├── main.py                  94 KB  ← 入口 + 管线 + 应用编排（2340 行）
├── control_panel.py         65 KB  ← 设置面板（7 个页签）
├── subtitle_overlay.py      47 KB  ← 主悬浮窗（聊天列表形态）
├── subtitle_window.py       34 KB  ← OBS 用的独立字幕窗（描边渲染）
├── dialogs.py               30 KB  ← 向导 / 模型下载 / 模型编辑
├── subtitle_settings.py     26 KB  ← 字幕窗设置
├── model_manager.py         27 KB  ← 模型探测 / 下载 / 缓存
├── translator.py            20 KB  ← OpenAI 兼容翻译客户端
├── audio_capture.py         18 KB  ← WASAPI loopback + 麦克风混音
├── vad_processor.py         16 KB  ← Silero VAD
├── install.ps1 / start.bat / update.bat / build_release.ps1
├── asr_engine.py             faster-whisper 后端
├── asr_funasr.py             FunASR 统一分发后端
├── asr_sensevoice.py         SenseVoice 后端
├── asr_funasr_nano.py        FunASR Nano 后端
├── asr_anime_whisper.py      Anime-Whisper 后端
├── asr_remote.py             远程 ASR 客户端
├── asr_client.py             子进程管理（主进程侧）
├── asr_worker.py             子进程入口（引擎装配）
├── asr_server.py             独立 HTTP ASR 服务（给远程用）
├── transcript_writer.py      转录落盘（.txt，墙钟时间戳）
├── benchmark.py              翻译基准测试
├── log_window.py / i18n.py
├── funasr_nano/              vendored 包（ctc.py / model.py / tools/）
├── i18n/                     en.yaml / zh.yaml / CHANGELOG_*.md
└── tests/                    4 个 pytest
```

### 1.2 管线与并发模型

```
系统音频(WASAPI loopback, 32ms) ──┐
                                  ├─→ VAD(Silero) ─→ ASR(子进程) ─→ 翻译(线程池) ─→ 悬浮窗
麦克风(可选混音) ─────────────────┘
```

线程/进程分工（`CLAUDE.md` + 源码核对一致）：

| 载体 | 职责 |
|---|---|
| 主线程 | Qt 事件循环（所有 UI） |
| capture 线程 | `LiveTranslateApp._capture_loop` — 读音频 + 跑 VAD |
| ASR 队列线程 | `_asr_loop` — 排空 VAD 段，调 `ASRClient.transcribe()` |
| **ASR 子进程** | `asr_worker.py`，持有具体引擎/模型，走 `multiprocessing.Pipe` |
| 翻译线程池 | `_tl_executor`（`max_workers=max(8, n+1)`），`max_tokens` 受控 |

两点值得称赞的设计（改造时要保留）：
- **ASR 跑在独立子进程**，所以主进程不会被 ONNX/torch 的原生内存泄漏拖死；`_maybe_recycle_asr_worker()` 还会在 worker RSS 超基线时优雅回收（`main.py:977-1042`）。
- **thinking 关闭策略**按 provider 分 6 种风格（`translator.py:141-177`），解决了"思考模型把 max_tokens 烧光返回空"的坑。

---

## 2. ASR 现状（调研项 ①）

### 2.1 引擎清单

分发点在 **`asr_worker.py:54-102` 的 `_load_engine()`**——一个 `if/elif` 链：

| 引擎 key | 实现 | 类 | 模型 / 规格 | 缓存体积 |
|---|---|---|---|---|
| `funasr` **（默认）** | `asr_funasr.py` → 再分发 | `FunASREngine` | 由 `funasr_model` 决定 | — |
| ├ `sensevoice-small` **（实际默认）** | `asr_sensevoice.py` | `SenseVoiceEngine` | `iic/SenseVoiceSmall` | 940 MB |
| ├ `funasr-nano-2512` | `asr_funasr_nano.py` | `FunASRNanoEngine` | `Fun-ASR-Nano-2512` + Qwen3-0.6B | 3.5 GB |
| └ `funasr-mlt-nano-2512` | 同上 | 同上 | `Fun-ASR-MLT-Nano-2512`（多语言） | 3.5 GB |
| `whisper` | `asr_engine.py` | `ASREngine` | faster-whisper：tiny→large-v3 | 78MB–3.1GB |
| `anime-whisper` | `asr_anime_whisper.py` | `AnimeWhisperEngine` | `litagin/anime-whisper`（日漫） | 3.1 GB |
| `remote-whisper` | `asr_remote.py` + `asr_server.py` | `RemoteASREngine` | HTTP 甩给 GPU 机器 | — |

模型注册表 + 体积估算集中在 **`model_manager.py:68-160`**（`ASR_MODEL_IDS` / `FUNASR_MODEL_PROFILES` / `ASR_DISPLAY_NAMES` / `_MODEL_SIZE_BYTES`）。

### 2.2 配置入口

- **`config.yaml` 的 `asr:` 段**（默认值）：`asr_engine`、`model_size`、`funasr_model`、`device`、`compute_type`、`language`、`min/max_speech_duration`、`vad_threshold`、`sensevoice_pad_seconds`、`whisper_pad_seconds`
- **`user_settings.json`**（运行时，优先级更高）：由 `control_panel.py` 写入，键见 `control_panel.py:107-137`

> **结论**：ASR **已经是可配置的多引擎架构**。老大提的"ASR 模型设为可配置"这条，**上游已经做完了**，而且默认就是 SenseVoice。

### 2.3 SenseVoice 的实际能力与短板

- **支持语言**：`asr_sensevoice.py:16-22` 的 `LANG_MAP` 只映射 `zh / en / ja / ko / yue`——比官方 SenseVoice 声称的语言集窄，中文场景足够。
- **中文优势**：SenseVoice 对中文的识别质量与速度都优于同体积 Whisper，**这正是老大场景（中文视频字幕）最需要的**。
- **可调项**：输入 padding 桶（`sensevoice_pad_seconds`，默认 0.5s）、`use_itn=True`（数字/标点规整）、fp16 自动（CUDA 时）。
- **短板**：`LANG_MAP` 语言码偏少；`set_language` 只接受 `auto` 或单一语言。

---

## 3. 翻译现状（调研项 ②）

### 3.1 协议与默认配置

- 实现：`translator.py`，走 **OpenAI 兼容 `chat.completions`**（`openai` SDK）
- 默认端点（`config.yaml:35-38`）：**本地 LM Studio** `http://127.0.0.1:1234/v1`，模型 `hunyuan-mt-chimera-7b`
  - ⚠️ `config.yaml` 里硬编码了一个 `api_key` 值（`sk-lm-...`）。看着像本地 LM Studio 的占位 key，但**这属于不该进版本库的东西**，改造时应清掉并只留注释示例。
- 代理策略：`none`（`httpx.Client(trust_env=False)` 绕系统代理）/ `system` / 自定义 URL（`translator.py:102-114`）

### 3.2 已有能力

流式逐字输出、`json_schema` 结构化输出、上下文轮次（`context_turns`）、逐模型覆盖参数（`overrides` / `extra_body`）、重复循环检测（`RepetitionError`）、空补全诊断（"thinking 烧光预算"告警）。

4 个 prompt 预设（`translator.py:53-99`）：`daily` / `esports` / `anime` / `webid`。

### 3.3 耦合点（解耦的靶心）

翻译**被无条件调用**，共两处，逻辑完全重复：

- `main.py:1313-1352`（`_process_segment`，整段 ASR 路径）
- `main.py:1553-1577`（`_process_segment_text`，增量 ASR 路径）

```python
target_lang = self._target_language
...
if source_lang == target_lang:
    log.info(f"Same language ({source_lang}), no translation")
    ...   # 只显示原文
else:
    self._tl_executor.submit(self._translate_async, msg_id, original_text, source_lang, ...)
```

**唯一的"免翻译"路径是 `source_lang == target_lang`**。也就是说：想关掉翻译，你得把目标语言设成和源语言一样——**这是个 trick，不是功能**。

### 3.4 ⚠️ 关键洞察：老大的场景可能根本不需要翻译

老大的原始需求是：**"针对没有字幕的视频，静音观看，通过看字幕获得相近观影体验"**。

拆开看是两件事：
- **A. 有声音没字幕** → 需要 ASR 生成字幕。**这是主线。**
- **B. 是外语** → 才需要再叠一层翻译。

如果视频本来就是**中文**（老大是信息技术教师，看中文课程/讲座的概率不低），那么：
- SenseVoice 出来的**就是中文字幕**，翻译纯属多余；
- 多余的翻译会**引入额外延迟**（首字延迟 + 流式刷新）并**可能篡改原文**（LLM 会"顺口"重写）。

所以正确的产品形态是**三档模式**，而不是当前强制单档：

| 模式 | 行为 | 适用 |
|---|---|---|
| `captions` | 仅 ASR，原样显示 | **中文视频**（零翻译延迟，老大主线） |
| `translate` | ASR → 翻译（当前行为） | 外语视频，只要译文 |
| `bilingual` | ASR → 翻译，原文+译文同显 | 外语视频，想对照学习 |

这个开关是本轮改造的**第一优先级**。

---

## 4. 解耦设计方案（调研项 ③）

### 4.1 ASR：`if/elif` 链 → 后端注册表

**现状问题**：新增一个引擎要同时改 4 处——`asr_worker.py` 的 `_load_engine`、`model_manager.py` 的 4 个 dict、`control_panel.py` 的下拉框、`dialogs.py` 的下载逻辑。

**目标结构**：每个后端一个模块，自描述 + 自注册。

```python
# livetranslate/asr/registry.py
@dataclass(frozen=True)
class BackendSpec:
    key: str                     # "sensevoice"
    display_name: str            # "SenseVoice Small"
    family: str                  # "funasr"
    build: Callable[[dict], Any] # 构造引擎
    model_id: dict[str, str]     # {"ms": "...", "hf": "..."}
    estimated_bytes: int
    supports_padding: bool
    supports_language: bool

_REGISTRY: dict[str, BackendSpec] = {}

def register(spec: BackendSpec) -> BackendSpec: ...
def get(key: str) -> BackendSpec: ...
def all_specs() -> list[BackendSpec]: ...
```

每个 `backends/*.py` 末尾 `register(BackendSpec(...))`。`_load_engine` 变成：

```python
def _load_engine(config):
    spec = registry.get(config["engine_type"])
    return spec.build(config)
```

于是 `model_manager` 的 4 个 dict、`control_panel` 的下拉框、`dialogs` 的下载列表**全部从注册表派生**，新增引擎 = 新增 1 个文件。**这是重构里收益最高的一步。**

### 4.2 翻译：从"焊死"到"可关"

```yaml
# config.yaml 新增
pipeline:
  mode: captions        # captions | translate | bilingual
```

- `captions`：`_process_segment*` 走到"输出原文"就 return，**完全不进 `_tl_executor`**
- `translate`：现行为
- `bilingual`：现行为 + overlay 同时显示原文与译文

实现上把重复的两段收敛成一个方法（顺便消掉 `main.py` 里那 60 行重复）：

```python
def _dispatch_output(self, msg_id, text, source_lang):
    mode = self._pipeline_mode
    if mode == "captions" or source_lang == self._target_language:
        self._transcript.finalize_no_translation(msg_id)
        self._overlay.update_translation(msg_id, "" if mode == "captions" else ..., 0)
        return
    self._tl_executor.submit(self._translate_async, msg_id, text, source_lang, ...)
```

### 4.3 配置分层（保持现有约定）

```
config.yaml         默认值（进版本库）
   ↓ 覆盖
user_settings.json  运行时（.gitignore 已忽略）
```
保持。补一个 `config/loader.py` 做**默认值合并 + 旧键迁移**，把散落在 `control_panel.py:139-165` 的 `setdefault` 集中起来。

---

## 5. 目录重构方案（调研项 ④）

### 5.1 目标结构

```
LiveTranslate/
├── main.py                      # 薄入口：只做 python -m 转发，保持 start.bat 兼容
├── config.yaml
├── requirements.txt
├── livetranslate/
│   ├── __init__.py
│   ├── app.py                   # ← main.py 的 LiveTranslateApp（管线编排）
│   ├── config/
│   │   ├── loader.py            # 默认值合并 + 迁移
│   │   └── defaults.yaml        # ← config.yaml（代码默认值）
│   ├── audio/
│   │   ├── capture.py           # ← audio_capture.py
│   │   ├── devices.py           # 设备/会话枚举（新增，为增强 1 铺路）
│   │   └── vad.py               # ← vad_processor.py
│   ├── asr/
│   │   ├── registry.py          # 新增：后端注册表
│   │   ├── client.py            # ← asr_client.py
│   │   ├── worker.py            # ← asr_worker.py
│   │   └── backends/
│   │       ├── whisper.py       # ← asr_engine.py
│   │       ├── funasr.py        # ← asr_funasr.py
│   │       ├── sensevoice.py    # ← asr_sensevoice.py
│   │       ├── funasr_nano.py   # ← asr_funasr_nano.py
│   │       ├── anime_whisper.py # ← asr_anime_whisper.py
│   │       ├── remote.py        # ← asr_remote.py
│   │       └── nano_src/        # ← funasr_nano/（vendored 包）
│   ├── translate/
│   │   ├── translator.py        # ← translator.py
│   │   ├── prompts.py           # 抽离 PROMPT_PRESETS / DEFAULT_PROMPT
│   │   └── thinking.py          # 抽离 provider thinking 策略
│   ├── output/
│   │   ├── transcript.py        # ← transcript_writer.py
│   │   └── srt.py               # 新增：SRT 生成
│   ├── ui/
│   │   ├── overlay.py           # ← subtitle_overlay.py
│   │   ├── styles.py            # 新增：样式预设（含电影字幕组）
│   │   ├── subtitle_window.py   # ← subtitle_window.py
│   │   ├── subtitle_settings.py # ← subtitle_settings.py
│   │   ├── control_panel/       # ← control_panel.py（拆成 panel + tabs/）
│   │   ├── dialogs.py           # ← dialogs.py
│   │   ├── log_window.py        # ← log_window.py
│   │   └── i18n.py              # ← i18n.py
│   ├── models/
│   │   └── manager.py           # ← model_manager.py（瘦身：只留下载/缓存）
│   └── benchmark.py             # ← benchmark.py
├── tools/
│   └── asr_server.py            # ← asr_server.py（独立服务，不入包）
├── doc/
├── i18n/  (yaml + changelog)
└── tests/
```

### 5.2 风险的诚实评估

| 项 | 风险 | 处置 |
|---|---|---|
| `main.py` 94KB | 一次性拆成 app + pipeline + output 三块，回归面大 | **不拆**。先只改 import 路径，逻辑零改动 |
| `control_panel.py` 65KB | 7 个页签全在一个类里 | **暂不拆**，后续按页签逐个抽 tab 类 |
| `funasr_nano/` vendored 包 | 内部可能用绝对 import | 移动后需核对 `ctypes.py`/`model.py` 的 import，必要时保留顶层 shim |
| `asr_worker.py` 走 `spawn` | 子进程 import 路径变了，**spawn 会重新导入模块** | ⚠️ 重点验证：worker 子进程必须能正确 import 新包路径 |
| `install.bat` / `start.bat` | 引用 `main.py` | 保留根目录 `main.py` 作为入口，兼容不破 |
| 上游 `update.bat`（git pull） | 重构后与上游彻底分叉 | 已 fork，接受分叉；不再用上游 update |

### 5.3 迁移策略：**纯搬移 + 转口，不改逻辑**

原则：**P1 阶段一行业务逻辑都不改**，只做文件搬移 + `import` 改写 + 新增 `__init__.py`。这样每一步都能用 `git diff --stat` 核对，用 `tests/` 4 个用例 + 一次真实启动验证。逻辑改动全部留到 P2。

---

## 6. 五项增强的可行性评估（调研项 ⑤）

### 增强 1：音频源过滤 —— ⚠️ **有硬约束，需要老大拍板**

**现状**：`audio_capture.py` **已有设备级选择**：
- `list_output_devices()` / `list_input_devices()`（:13-56）
- `set_device(name)` 运行时可切换（:233-241），`"__disabled__"` = 纯麦克风模式
- 默认输出设备变更自动重连（:312-334）

**问题**：老大要的是**进程级**——"只捕获你正在观看的视频播放器的音频"。

**技术真相**：
- `pyaudiowpatch` 的 WASAPI loopback 是**设备级**的：它抓**整个输出设备**的混音，包含系统通知音。**它没有进程过滤能力。**
- 要做到真·进程级，只有一条路：WASAPI **Process Loopback**（`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` + `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`），**需要 Windows 10 2004+**，并且必须用 `ctypes`/`comtypes` 手写 COM 绑定，**无法与 `pyaudiowpatch` 复用，要另开一条 capture 通道**。工作量约 300–500 行原生绑定 + 独立音频线程。

**三条路线**：

| 路线 | 能力 | 工作量 | 风险 |
|---|---|---|---|
| **A. 设备级 + 会话枚举**（务实） | UI 里列出**正在出声的应用**（`pycaw`/`IAudioSessionManager2`）+ 设备下拉。能看见"谁在出声"，但**过滤不了** | 低（约 1 天） | 低 |
| **B. 独立输出设备隔离**（零代码） | 把播放器切到虚拟声卡/独立输出，LiveTranslate 只抓那个设备 → **等效进程隔离** | 0 | 需用户手动路由 |
| **C. WASAPI Process Loopback**（彻底） | 真·只抓目标进程 | 高（3–5 天） | Win 版本门槛、COM 生命周期管理、与现有 capture 双轨 |

**建议**：**先做 A + 文档化 B**，把 C 作为可选后续。理由：老大场景是"公共场合静音看视频"，真正的干扰源是**系统通知音**——而 A 能让你**看见**干扰源来自哪个进程，B 能让你**物理隔离**它。C 的收益（自动识别播放器）不值得第一批投入。

> 需要老大确认：**是否接受 A+B 路线**，还是坚持要 C？

### 增强 2：字幕样式预设 —— ✅ 低难度，但有一个关键发现

**现状**：`subtitle_overlay.py:48-184` 有 **14 个预设**，但**全是终端配色主题**（Dracula / Nord / Monokai / Solarized / Gruvbox / Tokyo Night / Catppuccin / One Dark / Everforest / Kanagawa）——**没有一个是电影字幕风格**。

**方案**：新增 `cinema` 组预设（放 `ui/styles.py`）：

| 预设 | 样式 | 对标 |
|---|---|---|
| `cinema_black` | 半透明黑底（alpha≈180）+ 白字 | Netflix |
| `cinema_outline` | 全透明底 + 白字黑描边（3px） | 影院字幕 |
| `cinema_yellow` | 半透明黑底 + 亮黄字 | YouTube 自动字幕 |
| `cinema_soft` | 深灰半透明 + 米白字，圆角 | Apple TV |

**⭐ 关键发现**：主悬浮窗 `subtitle_overlay.py` 是**聊天列表形态**（每条消息带 `[时间戳][语言] ASR xxms` 元信息），**它不是"电影字幕"的载体**。

真正的地基在 **`subtitle_window.py`**——它已经用 `QPainterPath` 实现了**逐行描边文字渲染**（`_SubtitleTextWidget`，`subtitle_window.py:120`），有自动换行、进出场动画、逐行独立字体/颜色/描边/对齐（见 `CLAUDE.md` 的 "Subtitle Window" 一节）。

**所以"电影字幕风格"应该落到 `subtitle_window`，并且顺带做一件事：给它加一个"纯净模式"**——隐藏所有元信息（时间戳/语言/耗时），只留字幕文字。这样才是"相近的观影体验"。overlay 保留信息面板角色。

> 需要老大确认：电影字幕主题落在 `subtitle_window`（纯净模式），overlay 维持列表形态——**是否同意？**

### 增强 3：内存泄漏 —— ✅ 已定位 3 处真实问题

逐一核对后的诚实结论：

| 位置 | 问题 | 严重度 |
|---|---|---|
| `audio_capture.py:382` | `_mic_buf = np.concatenate([self._mic_buf, mic_16k])` —— **每 32ms 重新分配整个数组**，且 `_mic_buf` **无上限**。在混音消费路径被跳过（loopback 读失败 / `_stream is None` 走 `continue`）时**会持续增长** | 🔴 真实泄漏 |
| `subtitle_overlay.py:257-264` | `ChatMessage._streaming_timer` 惰性创建后**从不显式 `stop()`**；消息被 `deleteLater()` 淘汰（:1153-1157）时定时器仍挂在 widget 上 | 🟡 悬垂引用 |
| `audio_capture.py:100-113` | 设备轮询 `_query_current_default()` **每 2 秒新建 + terminate 一个 `PyAudio()` 实例**（:314-334 调用） | 🟡 churn，非泄漏 |
| `subtitle_overlay.py:1153-1157` | 消息淘汰 `removeWidget` + `deleteLater()`，**缺 `setParent(None)`**，Qt 侧释放依赖事件循环 | 🟡 需实测确认 |
| `main.py:1222` | `_tl_executor.shutdown(wait=True)` ✅ **已正确释放** | ✅ 无问题 |
| `main.py:977-1042` | ASR worker RSS 超基线自动回收 ✅ **上游已做** | ✅ 无问题 |
| `audio_capture.py:65` | `audio_queue` = `Queue(maxsize=100)` + drop-oldest ✅ | ✅ 无问题 |

**方案**：
1. `_mic_buf` 换**有界环形缓冲**（`collections.deque(maxlen=N)` 或预分配 `np.ndarray` + 读写指针），加**溢出丢帧**策略
2. `ChatMessage` 加 `closeEvent`/显式 `dispose()`，淘汰时先 `stop()` 定时器
3. 淘汰加 `setParent(None)`
4. 设备轮询改为**复用**单个 `PyAudio` 实例
5. **配套写一个长跑脚本**（`tools/mem_soak.py`）：跑 2 小时，每 30s 采样 RSS（主进程 + worker + GPU），落 CSV 出曲线，**用数据确认修复有效**——而不是"看着像修好了"

> ⚠️ 这一条**必须实测**，不能只靠看代码。渲染/音频路径的泄漏经常藏在原生侧。

### 增强 4：录制与导出 SRT —— ✅ 低难度，但数据链路要补

**现状**：`transcript_writer.py` 已经在写 `.txt`（三个文件：`original` / `translation` / `all`），但时间戳是**墙钟字符串 `datetime.now().strftime("%H:%M:%S")`**（`transcript_writer.py:68-95`）——**只有开始时刻，没有时长**，**无法直接转 SRT**。

**好消息**：所需数据**已经存在**——VAD 段的时长在 `main.py:1257` 就有（`seg_len = len(speech_segment) / 16000`）；overlay 的 `export_messages`（`subtitle_overlay.py:1207-1253`）已有导出 UI 骨架和右键菜单入口。

**方案**：
1. 在信息流里补两个字段：`session_start`（会话起点 monotonic）和每段的 `start / end`（相对秒）
2. 新增 `livetranslate/output/srt.py`：累积 `(start, end, text)` → 标准 SRT（`HH:MM:SS,mmm`），规则：最小时长 0.8s、重叠时顺延、空文本跳过
3. `transcript_writer` 增加 `.srt` 输出（与 `.txt` 并行），或独立 `SRTRecorder`
4. overlay 右键"导出"菜单加 **SRT 选项**（现有 `export_messages` 已支持 mode 参数，扩展即可）
5. **推流式写入**（边识别边追加 fragment），避免长时间观影后内存里堆全量字幕

> 附带收益：既然是"边看边生成 SRT"，**看完就能直接复用给视频播放器挂载**——正好回扣老大的原始需求。

### 增强 5：ASR 可配置 + 引入 SenseVoice —— ⚠️ **上游已完成，需对齐预期**

这条要**明确澄清**，避免做重复劳动：

| 老大的诉求 | 实际情况 |
|---|---|
| "设置模型为可配置项" | ✅ **已是**：`config.yaml` + 设置面板下拉，支持 6 种引擎 |
| "DeepSeek harness 采用的 ASR 模型为 SenseVoice，应该也有独到之处" | ✅ **本项目默认就是 SenseVoice**（`DEFAULT_FUNASR_MODEL = "sensevoice-small"`，`model_manager.py:110`） |

**上游判断和老大一致**——SenseVoice 确实是对的选择：非自回归、推理快、中文强、940MB 体积适中，比 Whisper-small/medium 更适合**实时字幕**。

**剩余可做**：
1. 后端**注册表化**（4.1），让配置成为唯一真相源
2. **补齐 SenseVoice 语言码**（当前 `LANG_MAP` 仅 zh/en/ja/ko/yue）
3. 为"字幕优先"场景**固化推荐默认值**（SenseVoice + `compute_type` 按显存自适应 + padding 0.5s）
4. 设置面板里把 ASR 引擎选择从 `_create_vad_tab` 的混杂布局里**独立成一个页签**

---

## 7. 环境方案（conda）

**硬约束：不污染全局 / base。**

```
conda create -n livetranslate python=3.12 -y
conda activate livetranslate

# PyTorch —— RTX 4060 (Ada, sm_89)，CUDA 12.x 轮子
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

要点：
- **绕过项目自带的 `install.bat`**（它会建 `.venv` 并 winget 装 Python）——我们直接用 conda env
- `requirements.txt` 里 `PyAudioWPatch` / `funasr` / `modelscope` 都是 pip 包，conda 环境下正常安装
- `numpy>=1.24,<2.3` 的上限**别动**（注释说明了：不锁会导致 Numba/llvmlite 回溯到不支持 Py3.12 的版本）
- `models/` 缓存目录在项目内（`MODELS_DIR = APP_DIR / "models"`），不写 `~/.cache`，符合"不污染系统"的要求
- 首启动会拉 Silero VAD + SenseVoice（≈1GB），需要网络

---

## 8. 分阶段执行计划（含确认门）

| 阶段 | 内容 | 交付 | 验证 |
|---|---|---|---|
| **P0 环境** | conda env + 依赖 + **跑通未改动版本** | 可启动的基线 | 启动成功，能出字幕 |
| **P1 重构** | 目录模块化（**纯搬移 + 转口，零逻辑改动**） | `livetranslate/` 包结构 | `pytest tests/` 全绿 + 真实启动 |
| **P2 解耦** | ASR 注册表 + 翻译三档模式（`captions`/`translate`/`bilingual`） | 可关闭翻译 | 中文视频零翻译延迟出字幕 |
| **P3 增强** | ①样式预设(含电影字幕+纯净模式) → ②SRT 导出 → ③内存修复(+长跑验证) → ④音频源过滤(A+B) | 逐项可用 | 逐项实测 |
| **P4 收尾** | 更新 `README` / `CLAUDE.md` / 文档；commit 分阶段 | 分阶段提交历史 | — |

**P0 必须先做**：先确认**未改动的原版能跑起来**，才有基线可比。如果原版就跑不起来，"重构后坏了"和"本来就坏"就分不清了。

---

## 9. 需要老大拍板的三个问题

1. **进程级音频捕获**：接受 **A（会话枚举）+ B（设备隔离）** 的务实路线，还是要投入做 **C（WASAPI Process Loopback 原生绑定）**？
   → 我的建议：先 A+B。理由：通知音的真实解法是"看见来源 + 物理隔离"，C 的自动识别收益不值第一批投入。

2. **重构幅度**：`main.py`(94KB) / `control_panel.py`(65KB) 这两个巨型文件，**本轮只搬目录不改内部结构**（低风险），还是**一并拆分**（收益大、回归风险高）？
   → 我的建议：**本轮只搬目录**。拆巨型文件单独开一轮，改完能独立回归。

3. **电影字幕的载体**：把"电影字幕风格 + 纯净模式"落在 `subtitle_window`（已有描边渲染），主 overlay 维持信息列表形态——**是否同意**？

---

## 附：调研证据索引

| 结论 | 证据位置 |
|---|---|
| ASR 引擎 if/elif 分发 | `asr_worker.py:54-102` |
| 模型注册表 | `model_manager.py:68-160` |
| SenseVoice 语言码偏少 | `asr_sensevoice.py:16-22` |
| 翻译耦合点（两处） | `main.py:1313-1352`、`main.py:1553-1577` |
| 翻译 client 构造 | `translator.py:102-114` |
| thinking 策略 | `translator.py:141-177` |
| 14 个终端样式预设 | `subtitle_overlay.py:48-184` |
| 主 overlay 是列表形态 | `subtitle_overlay.py:193-350`、`:1147-1157` |
| 字幕窗描边渲染 | `subtitle_window.py:120`、`CLAUDE.md` "Subtitle Window" |
| `_mic_buf` 无界增长 | `audio_capture.py:382`、`:393-401` |
| `_streaming_timer` 未释放 | `subtitle_overlay.py:257-264`、`:1153-1157` |
| transcript 无时长 | `transcript_writer.py:68-95` |
| 段时长可得 | `main.py:1257` |
| 导出 UI 骨架已有 | `subtitle_overlay.py:1207-1253` |
| 设备级选择已有 | `audio_capture.py:13-56`、`:233-241` |
| executor 已正确释放 | `main.py:1222` |
| worker RSS 回收 | `main.py:977-1042` |
