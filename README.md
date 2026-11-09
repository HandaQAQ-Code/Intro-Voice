# Intro Voice 音拓 · 直播调音台

Windows 本地软调音台：AI 人声处理 + 内置效果器 + 原生 VST3 宿主，处理结果经 VB-CABLE 送入 OBS。
还可以把手机当成无线麦克风，走局域网直连电脑。

- **界面**：WinUI 3（.NET 8，非打包模式），单层标题栏 + 左侧 NavigationView 七个分页（主控 / 麦克风配置 / 内置效果 / 创意工坊 / VST3 插件 / 插件市场 / 帮助与更新）、明暗主题自适应、中文使用微软雅黑 UI
- **可视化**：自研 `SpectrumView` 32 段实时频谱（Composition 动画）、电平表、信号链示意
- **音频引擎**：Python 进程内的 WASAPI 链路，命名管道控制协议（`protocol: 1`）
- **AI**：声纹提取（VoxLock）+ 降噪（DPDFNet）+ 超分（FlashSR / LavaSR）
- **内置效果**：高通/低通、三段 EQ、压缩、噪声门、输出限幅（`mixer_dsp.py`）
- **插件**：原生 C++ VST3 宿主（`native/vst3host`），支持扫描、排序、旁通、参数与状态
- **创意工坊**：Python 写效果器；新手模式只挑模板拖滑块，进阶模式写代码，一键发布到自托管市场
- **麦克风配置**：一页决定输入来源 —— 电脑声卡即插即用，手机麦克风作为无线扩展（Android App 采集 → Wi-Fi 直传 → 增益 / 高通 / 噪声门 / 抖动缓冲，扫码配对）
- **市场**：远程目录指向 `Intro-Voice-Plugin-Market`，支持工坊插件与 VST3（SHA-256 校验）两类安装/卸载
- **更新**：GitHub Releases 检查、下载校验、退出后替换与失败回滚（待真实 Release 验证）

## 麦克风配置

「麦克风配置」是输入的唯一入口：顶部两个按钮选来源，下面的内容跟着切换。

**电脑声卡**（默认）：显示当前输入设备、实时输入电平、信号路径与链路自检（采样率、延迟、掉块），
设备与增益仍在主控页调整，避免两处状态打架。

**手机麦克风**（派生来源）：点「手机麦克风」后启动服务，会给出局域网地址、6 位 PIN 和两张二维码：

1. 手机扫码安装 `Intro Voice Mic`（安装包随程序放在 `mobile/IntroVoiceMic.apk`）；
2. 扫码连接，或手填电脑 IP + PIN；
3. 选好来源后到「主控」页点开始即可。

手机端可调采样率 / 帧长 / 采集源、系统降噪与回声消除、本地增益，并显示实时电平、码率、
往返延迟；屏幕熄灭后由前台服务继续采集。电脑端与手机端都能实时改 DSP 参数。
音频只在局域网内传输，不经过任何服务器。

## 目录

```text
MixerUI/            WinUI 3 前端（页面、样式、素材）
mixer_service.py    音频服务与命名管道协议
mixer_dsp.py        内置 DSP 链（无 UI 依赖）
mobile_mic.py       手机麦克风：环形缓冲 + 局域网 HTTP/WebSocket
workshop_service.py 创意工坊：脚本编译、试听、发布到市场
market_service.py   插件市场与自动更新
mobile-android/     手机端 Android 工程（Java + OkHttp WebSocket）
mobile/             构建好的安装包 IntroVoiceMic.apk
native/vst3host/    原生 VST3 宿主（C++）
tests/              DSP、工坊、手机麦克风与真实插件回归
plugin-market/      随包插件目录（离线兜底，不含第三方二进制）
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

## 自测

```bat
python tests\test_mixer_dsp.py --engine
python tests\test_workshop.py          :: 工坊编译/隔离/市场安装
python tests\test_mobile_mic.py        :: 环形缓冲、DSP、HTTP/WebSocket 端到端
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
