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

---

## 10. 实施进展与落地决策

### 10.1 老大已拍板

1. **音频源过滤走 A+B**（会话枚举 + 设备隔离），不做 C（原生 WASAPI Process Loopback）。
   A+B **在音频链路上零新增延迟**——会话枚举只在打开选择器时调用，设备隔离靠系统路由。
2. **重构先搬目录、后拆文件**。`main.py`(94KB) / `control_panel.py`(65KB) 本轮只改 import 与路径锚点，内部结构不动。
3. **电影字幕是立项重点，必做**。
4. 环境用 **conda 独立 env**，不污染全局/base。

### 10.2 与原方案的两处偏离（实施中发现的更优解）

**① 翻译只做两档，不做三档。**
原方案提 `captions` / `translate` / `bilingual`。落地时发现 **`bilingual` 是冗余的**：overlay 本来就同时渲染原文与译文（`ChatMessage` 头部=原文，下方=译文），subtitle window 也接收 `(original, translations)` 两者。也就是说"双语"**已经是上游行为**，再开一个与 `translate` 完全等价的模式属于为了对称而对称。

真正的解耦只有一件事：**要不要调翻译**。所以落地为 `captions` / `translate` 两档 + overlay 的「纯净模式」显示开关（与模式正交的另一根轴）。

**② 电影字幕同时落在两套载体上。**
- **overlay**：4 个影院配色预设 + **纯净模式**（去掉时间戳/语言/ASR-TL 耗时那套"调试味"信息）。overlay 用 QLabel 渲染富文本，**画不了描边**，所以它的"电影感"来自低视觉噪音。
- **subtitle_window**：4 个影院预设，含**真·描边**（`QPainterPath` 逐行描边）——`cinema_outline` 透明底 + 白字黑描边 3px 才是"最接近原生播放器"的那一个。

### 10.3 实施中发现的三个真实缺陷（均已修）

| # | 缺陷 | 影响 | 处置 |
|---|---|---|---|
| 1 | **`.gitignore` 的 `models/` 未锚定根目录** | 它同时匹配 `livetranslate/models/`，会把 model-manager **代码包整个排除在提交之外**——静默丢失，代码直接废掉 | 运行时数据目录全部加前导 `/` 锚定 |
| 2 | **`requirements.txt` 漏了 `yasbd-lib`** | `main.py:_get_segmenter()` 依赖它做增量 ASR 断句。按 README 全新安装后，一进断句就 `ModuleNotFoundError` | 补进 requirements |
| 3 | **PyPI 的 Windows torch 是 CPU 版**（实测 106MB） | 想走国内镜像加速 CUDA 版是走不通的；CUDA 只能从 `download.pytorch.org` 拿（2.4GB，国际线路 0.1–0.8 MB/s） | 部署脚本优先用 `.pip-cache/wheels/` 里的本地 wheel |

### 10.4 验证体系（可复跑）

| 脚本 | 覆盖 | 当前结果 |
|---|---|---|
| `doc/migrations/verify_p1.py` | 包内导入是否全部解析（含子模块导入、被导入名是否真的定义） | 93 处 / 45 文件，0 问题 |
| `doc/migrations/verify_runtime.py` | 全模块导入、路径锚点、注册表懒加载不拉 torch、**ASR worker 真跑一次 spawn 往返**、模式归一化、影院预设接线 | 7/7 通过 |
| `doc/migrations/verify_ui.py` | 离屏 Qt：控制面板预设联动、字幕窗预设端到端、overlay 纯净模式与 captions 渲染 | 5/5 通过 |
| `pytest tests/` | 上游自带测试 | 33 通过 |
| `doc/migrations/prefetch_models.py` | 无需点 GUI 向导即可预下模型 | SenseVoiceSmall 897MB（ModelScope 峰值 23 MB/s） |

> **踩坑记录**：GUI 校验里 `QApplication` 若只作为函数局部变量持有，PyQt6 会回收 Python 包装对象并连带销毁底层 C++ application，之后创建任何控件都会**原生崩溃且无 traceback**（表现为 exit 127）。必须用模块级全局持有。

### 10.5 阶段状态

| 阶段 | 状态 |
|---|---|
| P0 conda 环境 | ✅ env `livetranslate`（Python 3.12）+ **CUDA torch 2.6.0+cu124**；RTX 4060 Laptop 认到，capability (8,9)=sm_89；**SenseVoice 已在 GPU 上跑通真实转写** |
| P1 搬目录 | ✅ 提交 `5715bcb` |
| P2 解耦 | ✅ 提交 `337abbe` |
| P3 电影字幕 | ✅ 提交 `bdb0233` |
| **P4.1 SRT 导出** | ✅ 提交 `e989090` |
| **P4.2 内存修复** | ✅ 见 10.8（含一处**实测推翻我原判断**的记录） |
| **P4.3 音频源过滤 A+B** | ✅ 见 10.10 |

### 10.10 P4.3 音频源过滤 A+B

**先做的事：可行性探测（`doc/migrations/probe_audio_source.py`），而不是先写 UI。**

**根本约束**：WASAPI loopback 抓到的是**端点混音**，不是某个应用。想把单个进程的声音从这个混音里取出来，只有 process loopback 一条路（`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`）。所以"只录某个播放器"的可行路线是：

- **A 枚举**：列出各应用的音频会话，并（因为会话是**按端点枚举**的）确认它在哪个设备上播放
- **B 隔离**：把该应用路由到**另一个输出端点**，然后录那个端点

**实测结论**

| 项 | 结论 |
|---|---|
| A 会话枚举 | ✅ 可用。`AudioUtilities.GetAllSessions()` **只覆盖默认端点**，且 pycaw 的 `AudioSession` 不暴露 meter，所以本项目自己走 `IAudioSessionManager2` 逐端点枚举 + 直接 QueryInterface 取 `IAudioMeterInformation` |
| B 设备隔离 | ⚠️ **需要第二个输出端点**。本机**当前只有一个可用渲染端点**（PortAudio 只报 `扬声器 (Realtek(R) Audio)`；pycaw 列出的 EDIFIER 耳机 / NVIDIA HDMI 等都是**注册过但未连接**的端点，激活时报 `AUDCLNT_E_DEVICE_INVALIDATED`）。接上耳机或显示器 HDMI 即有第二个；装虚拟声卡（VB-CABLE）则恒定可用 |
| C 进程 loopback | 技术上可行（build 26300 ≫ 19041，comtypes 在位），但**无现成 pip 包，需手写 `ActivateAudioInterfaceAsync`** |

**性能实测（决定了 UI 形态）**

| 操作 | 耗时 |
|---|---|
| `GetAllDevices()`（走设备树） | **~2100ms，每次都要**（OS 层无缓存） |
| 会话枚举（端点已解析） | ~200ms |
| 首屏（含器件树） | ~3800ms |
| 缓存端点后的刷新 | **~245ms** |

因此 `SessionMonitor` **缓存端点列表**，刷新只重读会话——快 15 倍，才让"刷新按钮"这件事成立。

**设计取舍**
- **只读**：枚举会话与读峰值表**绝不改变用户听到的声音**
- **不用定时器**：一次刷新是 UI 线程上 0.25s 的**阻塞** COM 调用，按按钮可以，每几秒跑一次会卡界面
- **面板构造时不枚举**：首次要 2.1s，会把"打开设置"卡住。改为显式点「刷新」，初始显示提示语
- **诊断要敢说"没隔离"**：`diagnose()` 明确区分三态——录的设备上没有应用发声 / 只有一个应用发声（此时系统回环**等效于**只录它）/ 多个应用同时发声（`warn`，会被一起录进去）。没有这层诊断，用户无法分辨"已隔离"和"其实在录全系统"

**新增**：`livetranslate/audio/sessions.py`（`SessionMonitor` / `diagnose()`）、控制面板「音频源」组、`tests/test_audio_sessions.py`（23 个用例，覆盖两种设备名方言的匹配、会话标签、峰值阈值、诊断三态）、`requirements.txt += pycaw`。


### 10.6 P4.1 录制与导出 SRT

**做法**：新增 `livetranslate/output/srt.py`（`SRTWriter`），与 `.txt` 转录**并行**写入 `transcripts/`，流式追加——播完文件即完整，中途崩溃也留下可用前缀。

**关键设计：时间轴用「捕获音频时钟」而不是墙钟。**
`_capture_loop` 每消费一个 chunk 就累加 `_audio_clock_s`（`len(chunk)/16000`）。理由是：被丢掉的音频（ASR 队列满、暂停、设备重启）本来就不在录像里，用墙钟会让字幕相对视频**越走越偏**——而字幕文件只有对齐才有用。

**第二个关键点**：cue 的 `t_end` 取 **VAD flush 时刻**快照的时钟读数（随队列一起传下去），不是 ASR 出结果时的读数。否则每条 cue 都会被**ASR 延迟**整体推后几百毫秒。

**实测抓到的两个问题（已修）**：
1. 无时长提示时，我最初写成 `start = end - 0` → cue 塌缩成一点，再被「最小时长 0.8s」补成 `[3.0, 3.8]`，**落在语音结束之后**。正确行为是从上一 cue 结束延伸到现在。
2. `pip` **会校验 wheel 文件名**（PEP 427）。我图方便把 torchaudio 存成 `torchaudio-cu124.whl`，直接报 `Invalid wheel filename (wrong number of parts)`。**必须保留官方原始文件名**（`torchaudio-2.6.0+cu124-cp312-cp312-win_amd64.whl`）。这条已写进 `setup-conda.ps1`，并在检测到重命名文件时给出明确报错而不是静默回退到慢速网络下载。

**配套**：`tests/test_srt_writer.py`（10 个用例：时间戳格式、最小时长、禁止重叠、空文本跳过、时间倒置修复、双语双行、运行时开关），以及 `verify_runtime.py` 的第 9 项——**驱动真实管线**确认 `captions` 模式**从不向翻译器提交任务**、且 SRT cue 用音频时钟落盘。

### 10.7 CUDA 环境验证

`doc/migrations/check_gpu_asr.py`：加载真实 SenseVoice 到真实 GPU 并跑一次转写。这是唯一能抓到 CUDA/DLL 问题（torch 构建不对、驱动不匹配、缺 cudnn）的手段——这类问题在模型碰到设备之前完全不可见。

实测结果：`worker ready 71.7s` → `SenseVoice loaded ... <All keys matched successfully>` → GPU 转写 1013ms。

> **待优化（下一轮）**：worker 冷启动 71.7s 偏长。`multiprocessing` 的 spawn 会用 `_fixup_main_from_path` **重新 import 入口 `main.py`**，而 `main.py` 顶部有 `import torch` + PyQt6——也就是说**每次起 ASR worker 都白跑一遍 GUI 的 import**。把 worker 入口挪到一个轻量模块可以省掉这部分。

### 10.8 P4.2 内存修复

三处"只有长跑才暴露"的泄漏。都用回归测试钉住了（`tests/test_memory_regressions.py`）——五分钟手测完全看不出来。

**① `_mic_buf` 无上限（`audio/capture.py`）**
原代码：`_read_loop` 里 mic 数据持续 `np.concatenate` 进缓冲，靠"每次 loopback chunk 消耗等长数据"来排空。但 **loopback 流不可用时（输出设备被拔 → `loopback_audio is None` → `continue`）没有任何东西排空它**，于是整个中断期间无限增长；又因为每次 append 都是 `np.concatenate`，随着缓冲变大还会**退化到 O(n²)**。
修：加 `MIC_BUFFER_MAX_SECONDS = 2.0` 上限，溢出时丢弃**最旧**的音频（最新的才和即将混入的 loopback chunk 对齐）。

**② overlay 的 50 条消息上限其实没限制住内存（`ui/overlay.py`）**
`ChatMessage.update_streaming` 里 `self._streaming_timer = QTimer()` —— **没有父对象**，而 `timeout.connect(self._flush_streaming)` 是**绑定方法连接**，会让这个 QTimer 持有对 ChatMessage 的强引用。结果是：被淘汰的消息**永远不会被释放**，那个"50 条上限"约束的只是 **layout 的条数，不是内存**。
修：`QTimer(self)` 让它成为子对象，随控件一起销毁。

**③ 淘汰只用了 `deleteLater()`**
`removeWidget()` 只是从布局里摘掉，控件**仍是父控件的子对象**，会一直保留到事件循环处理 `deleteLater` 为止（期间还按旧几何位置绘制）。
修：新增 `ChatMessage.prepare_for_removal()` —— 停掉 streaming timer、断开连接、`setParent(None)`，淘汰与清空两条路径都先调它。顺带把 `_pending_streaming` 清掉。

**④ 附带修掉**：`AudioCapture.__del__` 里 `if self._pa:` 在 `__init__` 未完成时（或解释器关闭阶段）会抛 `AttributeError`，被 Python 吞成噪声警告。改用 `getattr` + `try`。这是**测试跑出来的**，不是我预先想到的。

### 10.9 ⚠️ 一处实测推翻的假设（保留记录）

我在 10.7 写过"worker 冷启动慢是因为 spawn 重新 import `main.py`（torch + PyQt6）"。**实测证明这条是错的**：

| import | 耗时 |
|---|---|
| `torch` | 4.72s |
| `PyQt6.QtWidgets` | **0.15s** |
| **`funasr`** | **25.64s** |
| `main`（全量，其内部含 torch） | 4.92s |

PyQt6 只要 0.15s，`main.py` 相对 `import torch` 的**额外开销只有 0.2s**。真正的成本是 **funasr 的 import（25.6s）+ SenseVoice 模型初始化（~45s）**。

**所以"把 worker 入口挪到轻量模块"的重构不做**——最多省 0.2s，却要把 94KB 的 `main.py` 拆开，违背"先搬目录后拆文件"的约定。**收益不成比例的重构就该砍掉。**

另注：先前那次 71.7s 的测量本身也有偏差——当时 `__main__` 是 `check_gpu_asr.py`（轻量脚本），**根本没触发 main.py 被重新 import**，所以那个数字反映的是"funasr + 模型初始化"的真实成本，不含任何 main.py 开销。



