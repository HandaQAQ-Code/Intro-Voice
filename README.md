# Intro Voice 音拓 · 直播调音台

Windows 本地软调音台：AI 人声处理 + 内置效果器 + 原生 VST3 宿主，处理结果经 VB-CABLE 送入 OBS。
还可以把手机当成无线麦克风，走局域网直连电脑；处理后的声音经虚拟麦克风输出给 OBS / 会议软件。

- **界面**：WinUI 3（.NET 8，非打包模式），单层标题栏 + 左侧 NavigationView 七个分页（主控 / 麦克风配置 / 内置效果 / 创意工坊 / VST3 插件 / 插件市场 / 帮助与更新）、明暗主题自适应、中文使用微软雅黑 UI
- **可视化**：自研 `SpectrumView` 32 段实时频谱（Composition 动画）、电平表、信号链示意
- **音频引擎**：Python 进程内的 WASAPI 链路，命名管道控制协议（`protocol: 1`）
- **AI**：声纹提取（VoxLock）+ 降噪（DPDFNet）+ 超分（FlashSR / LavaSR）
- **内置效果**：高通/低通、三段 EQ、压缩、噪声门、输出限幅（`mixer_dsp.py`）
- **插件**：原生 C++ VST3 宿主（`native/vst3host`），支持扫描、排序、旁通、参数与状态
- **创意工坊**：Python 写效果器；新手模式只挑模板拖滑块，进阶模式写代码，一键发布到自托管市场
- **虚拟麦克风输出**：启动时自动检测 CABLE Input / CABLE Output 端点，自动选中输出，未安装时一键引导到官方安装器或官网（不捆绑、不自动安装驱动）
- **麦克风配置**：一页管输入与输出 —— 虚拟麦克风输出为主视图，手机麦克风为派生输入（Android App 采集 → Wi-Fi 直传 → 增益 / 高通 / 噪声门 / 抖动缓冲，扫码配对）
- **市场**：远程目录指向 `Intro-Voice-Plugin-Market`，支持工坊插件与 VST3（SHA-256 校验）两类安装/卸载
- **更新**：GitHub Releases 检查、下载校验、退出后替换与失败回滚（待真实 Release 验证）

## 麦克风配置

「麦克风配置」页顶部有两个视图：**虚拟麦克风输出**（默认）与 **手机麦克风**（Wi-Fi 输入）。

### 虚拟麦克风输出

系统级虚拟麦克风需要内核驱动。本软件**只检测、不分发、不自动安装**驱动：
启动时枚举音频端点，判断 CABLE Input（本软件写入）与 CABLE Output（OBS 采集）是否存在，
并把输出设备自动选中 CABLE Input；未检测到时，页面上给出「打开官方安装器」与
「官网下载 VB-CABLE」两个一键引导按钮，由你手动以管理员身份运行官方 Setup 并重启。

> VB-CABLE 版权归 VB-Audio / Vincent Burel 所有。其许可允许原样复制扩散，
> 但不允许未经作者同意集成进其他软件的安装流程，因此仓库与发行包均不包含驱动文件。
> 页面显示的「本机已有官方解压包」只指向用户自己解压的目录，不会复制或执行安装。

路由：输入来源（声卡 / 手机）→ AI 降噪与超分 → 内置效果 → VST3 → 创意工坊
→ **CABLE Input** → **CABLE Output** → OBS / 会议软件。

1. 左侧确认「虚拟麦克风」显示已就绪；没有就按引导安装并重启，回来点「刷新状态」。
2. 回「主控」页点开始，输出会自动选中 CABLE Input。
3. OBS 添加「音频输入捕获」并选 CABLE Output；会议软件把麦克风改成同一设备。

输入来源（电脑声卡 / 手机麦克风）也在这一页切换，切换后回主控页重新开始生效。

### 手机麦克风（派生输入）

点顶部「手机麦克风」启动服务后，会给出局域网地址、6 位 PIN 和两张二维码：

1. 手机扫码安装 `Intro Voice Mic`（安装包随程序放在 `mobile/IntroVoiceMic.apk`）；
2. 扫码连接，或手填电脑 IP + PIN；
3. 回「主控」页点开始，处理后的声音仍从虚拟麦克风输出。

手机端可调采样率 / 帧长 / 采集源、系统降噪与回声消除、本地增益，并显示实时电平、码率、
往返延迟；屏幕熄灭后由前台服务继续采集。电脑端与手机端都能实时改 DSP 参数。
音频只在局域网内传输，不经过任何服务器。

## 目录

```text
MixerUI/            WinUI 3 前端（页面、样式、素材）
mixer_service.py    音频服务与命名管道协议（源码；发行包封装为 mixer_service.exe）
mixer_dsp.py        内置 DSP 链（无 UI 依赖）
mobile_mic.py       手机麦克风：环形缓冲 + 局域网 HTTP/WebSocket
virtual_device.py   虚拟麦克风端点检测与官方安装引导（不分发驱动）
app_paths.py        统一根目录解析（源码 / PyInstaller frozen 两种模式）
workshop_service.py 创意工坊：脚本编译、试听、发布到市场
market_service.py   插件市场与自动更新
mobile-android/     手机端 Android 工程（Java + OkHttp WebSocket）
mobile/             构建好的安装包 IntroVoiceMic.apk
native/vst3host/    原生 VST3 宿主（C++）
tests/              DSP、工坊、手机麦克风与真实插件回归
plugin-market/      随包插件目录（离线兜底，不含第三方二进制）
tools/mixer_service.spec  PyInstaller 打包配置（源码 → onedir 目录）
tools/pack_full.py  便携发行包打包脚本（WinUI + onedir 引擎 + data + native）
gpu_install.py      GPU 档 CUDA PyTorch 按需下载（装到 gpu_packages/，主包瘦身）
```

## 构建

```bat
:: 1) 取 VST3 SDK（未随仓库分发，见 THIRD_PARTY.md）
::    git clone --depth 1 https://github.com/steinbergmedia/vst3sdk third_party\vst3sdk
:: 2) 原生宿主
native\vst3host\build.bat
:: 3) 前端
dotnet build MixerUI\MixerUI.csproj -c Release -p:Platform=x64
```

手机端（需要 JDK 21 与 Android SDK 34）：

```bat
cd mobile-android
gradlew.bat assembleRelease
copy app\build\outputs\apk\release\app-release.apk ..\mobile\IntroVoiceMic.apk
```

运行需要 Python 3.11 + `requirements.txt` 依赖（含生成二维码的 `segno`）；便携发行包自带解释器与模型，不在本仓库内。

### 封装引擎（发布时，隐藏源码）

```bat
:: 把 Python 音频引擎封装成 onedir 目录（不暴露 .py 源码，启动快、可增量更新）
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean tools\mixer_service.spec
:: 产物为 dist\mixer_service\（mixer_service.exe + _internal\）
:: 产出便携发行包（WinUI + mixer_service\ + data + native + mobile）
.venv\Scripts\python.exe tools\pack_full.py
```

封装后的 `mixer_service\` 目录把引擎代码与 numpy / onnxruntime / 声纹 / 超分等依赖
封进 `_internal\`（无 .py 源码）；`data/`、`native/`、`mobile/` 等运行时资源放在引擎目录旁。

GPU 档的 CUDA PyTorch（约 2.5 GB）**不随主包分发**以瘦身。用户在主控页 GPU 档不可用时
有两种方式获取，界面按优先级给出选项：

1. **复用本机 Python 环境**（优先）：若本机已装 Python 3.11 且其 `torch` 带 CUDA
   （`torch.cuda.is_available()`），点「使用本机 Python 环境」直接复用，无需下载、无需重启；
2. **下载独立安装**：点「下载 GPU 支持」，程序用系统 Python 3.11 把依赖装到
   `mixer_service\gpu_packages\`，装完重启即可选 GPU 档。

详见 `gpu_install.py`（含 `detect_native_torch` / `apply_native_to_syspath`）。

## 自测

```bat
python tests\test_mixer_dsp.py --engine
python tests\test_workshop.py          :: 工坊编译/隔离/市场安装
python tests\test_mobile_mic.py        :: 环形缓冲、DSP、HTTP/WebSocket、lan_ip 检测
python tests\test_vst3_real.py         :: 需要本机已装 VST3 插件
```

## 文档

- 产品路线图：[PRODUCT_ROADMAP_MIXER_ECOSYSTEM.md](PRODUCT_ROADMAP_MIXER_ECOSYSTEM.md)
- 执行约束：[EXECUTION_PLAN_WINUI_VST3.md](EXECUTION_PLAN_WINUI_VST3.md)
- 创意工坊插件格式：[WORKSHOP.md](WORKSHOP.md)
- 符合性核对（含未完成项）：[COMPLIANCE_ROADMAP.md](COMPLIANCE_ROADMAP.md)

## 分发状态

**未就绪**。许可证表未完成（P0），便携包内含个人声纹与模型权重，不可作为公开发布物。
详见 `COMPLIANCE_ROADMAP.md`。
