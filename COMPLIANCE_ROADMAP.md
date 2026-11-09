# 路线图符合性核对（2026-11-08）

对照 [PRODUCT_ROADMAP_MIXER_ECOSYSTEM.md](PRODUCT_ROADMAP_MIXER_ECOSYSTEM.md) 与
[EXECUTION_PLAN_WINUI_VST3.md](EXECUTION_PLAN_WINUI_VST3.md) 逐条核对当前实现。
**只记录实际跑过的结果**；没验证的写「未验证」，规划项不写成已完成。

## 一、里程碑状态

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| P0 产品与许可证决策 | **未完成** | 未产出许可证表。当前仓库仍含个人声纹 `data/target.wav` 与模型，不可直接公开分发，见「四、阻塞项」 |
| P1 真实 VST3 Host Spike | **已满足（本机）** | 真实插件 Ozone 11 Equalizer 加载、改参数改变输出、旁通还原；详见下方证据 |
| P2 WinUI + AI/虚拟线 MVP | **已满足（本机）** | 真实设备枚举、CABLE 输出、AI 开/关、增益/静音/电平/限幅；Tkinter 旧入口文件保留 |
| P3 VST3 正式功能 + 预设 | **基本满足** | 扫描/加载/排序/旁通/参数/状态/预设均可用；「重启后恢复」与「缺失插件」有保护；双实例已验证 |
| P4 基础效果器 | **已满足** | 高通/低通、3 段 EQ、压缩、Gate、限幅；版本化参数与预设；测试样例进仓库 |
| P5 GitHub 发布与更新 | **未开始** | 无签名 Release、无更新检查；当前仅便携目录手工分发 |
| P6 插件目录 / 自有驱动 | **未开始** | 按路线图属研究阶段，未做 |

## 二、P1/P3：真实插件证据

命令（便携包内置解释器）：

```bat
runtime\python.exe tests\test_vst3_real.py
```

实测输出（2026-11-08，iZotope Ozone 11）：

```text
scan dirs: C:\Program Files\Common Files\VST3, C:\Program Files (x86)\Common Files\VST3
plugins: 18 errors: 0
chosen: Ozone 11 Equalizer | iZotope
loaded: Ozone 11 Equalizer | bypass: False | latency(samples): 0
params: 107
trying param: GAIN: Input L (Locked)
after param RMS: 0.015649  ratio: 0.22        <- 改参数后电平变为 0.22 倍（约 -13 dB）
chain bypass RMS: 0.070711                    <- 旁通后与输入完全一致
instances: 2 | after move, order: [EQ, EQ]
bypass flags: [False, True]
dual-process RMS: 0.070711  finite: True
RESULT: PASS
```

对应验收项：

- 「真实输出能证明效果器参数改变了音频」→ ratio 0.22，非静音、非 NaN。
- 「插件异常可定位，不会卡死 UI」→ 宿主异常统一抛 `Vst3Error`，UI 走 InfoBar 展示；扫描在独立子进程完成（`vst3_scan_worker.py`）。
- 「两个实例能够调序和旁通」→ 2 实例、`move` 后顺序正确、旁通标志 `[False, True]`、处理输出有限。
- 已知限制：只验证了 **1 个厂商的 1 个插件**，不能据此宣称「兼容所有 VST3」；乐器、侧链、多总线未支持。

## 三、P4：内置效果器证据

`tests/test_mixer_dsp.py`（随仓库交付，30 项断言全通过）：

```text
[OK] 极端参数 60 块无 NaN            nan_blocks=0
[OK] 极端参数输出有界                峰值=120.0        <- 上限由下游 PeakLimiter 负责
[OK] block=64 / 128 / 256 / 512 / 1920 / 4096 处理正常
[OK] 不足一块的尾巴正常返回          shape=(37, 2)
[OK] 预设载入 / 旁通切换无爆音       最大相邻样本跳变=0.0099
[OK] 单模块开关无爆音                最大相邻样本跳变=0.0098
[OK] 越界值夹取 / 非法斜率回退 / 非数值忽略 / 部分更新保留其他字段
[OK] 单块耗时在预算内                2.09 ms / 40 ms 预算
[OK] 引擎内 EQ 生效                  8.5 dB
RESULT: PASS
```

对应路线图 §3 的要求：

- 效果顺序：高通 → 低通 → 噪声门 → 压缩 → 三段 EQ → VST3 链 → 限幅。限幅只做输出保护（ceiling 0.98），不做响度增强。
- 参数边界、静音、削波、不同 block size、状态重置、长时间运行均有覆盖。
- **旁通/预设切换不爆音**：总开关 12 ms 干湿交叉淡化；单模块开关用「旧状态快照 + 新状态」交叉淡化，实测跳变 < 0.01。
- 参数 schema 带 `version: 1`，未知键忽略、越界夹取、旧预设向前兼容。
- 与路线图的偏离：§3 建议「独立 C++ 处理模块」，当前是 **Python/numpy 实现**（`mixer_dsp.py`，无 UI 依赖，实时线程只做有界运算，2 ms/40 ms 块）。理由：现有音频引擎整体在 Python 进程内，先保证行为与测试，再评估 C++ 重写；接口（`DspChain.set_params/process/metrics`）已按「可替换处理器」设计。**尚未做 C++ 版本，也不能宣称达到 C++ 的确定性。**

## 四、与文档不符或未验证的项（必须如实告知）

1. **许可证表缺失（P0 阻塞）**：未核对 VST3 SDK、Windows App SDK、VoxLock、LavaSR、DPDFNet 权重、VB-CABLE 的分发条款。当前便携包含 `data/target.wav`（个人声纹）与模型权重，**不能作为公开发布物**。
2. **OBS 实际采集未验证**：本机未操作 OBS，只验证了服务写入 CABLE Input、设备枚举正确。
3. **GPU 档未在本机跑通**：CUDA 版 PyTorch 需另行安装（`tools\install_gpu_support.bat`），本次未安装，GPU 链路未测。
4. **长时稳定性**：仅做块级与短时回归，未做数小时直播压测。
5. **VST3 覆盖有限**：只测 1 个插件；插件编辑器窗口的焦点/关闭生命周期只在代码层处理，未做多插件联调。
6. **VST3 宿主是进程内实现**：插件崩溃会带走音频进程。界面与文档不得声称「插件沙箱隔离」；扫描阶段才使用独立子进程。
7. **控制协议版本**：`hello` 已返回 `protocol: 1` 与 `presetVersion: 1`，但 UI 目前只做展示，不做版本不匹配的强制阻断。
8. **预设失败保护**：缺插件、版本过高、逐项加载异常都会报错并回滚到载入前的链（实测 `未改动当前链路`），但没有「跳过缺失插件继续载入」的交互选项。

## 五、复核命令

```bat
:: DSP 算法与边界（30 项）
runtime\python.exe tests\test_mixer_dsp.py --engine

:: 服务协议、DSP 参数落盘、预设回路由 tests\test_mixer_dsp.py 内的 --engine 覆盖

:: 真实 VST3（需本机装有 VST3 插件）
runtime\python.exe tests\test_vst3_real.py
```

便携版启动：双击 `启动调音台.bat`。日志：`data\mixer\service.log`、`data\mixer\ui.log`。


## 七、插件市场与自动更新（本轮新增）

| 能力 | 实现 | 验证状态 |
| --- | --- | --- |
| 远程目录拉取 | `market_service.fetch_catalog`，8 s 超时、10 分钟缓存、失败回退随包 `plugin-market/catalog.json` | 目录已迁到 `Intro-Voice-Plugin-Market`；GitHub API 确认 5 条目（1 工坊 + 1 zip + 3 link）。构建环境访问 raw.githubusercontent 被 TLS 拦截，实际走随包兜底 |
| 一键安装 | 下载 → SHA-256 校验 → 路径穿越防护解压 → `%LOCALAPPDATA%\Programs\Common\VST3\<slug>` | **通过**：本地 HTTP 目录 + 真实 zip 包，安装后目录存在、已安装列表出现；错误哈希被拒绝 |
| 卸载 | 删除安装目录并更新 `market_installed.json` | **通过**：卸载后目录消失、列表清空 |
| 目录内容 | 1 条 `workshop`（空气感）+ 1 条 `zip`（Surge XT Effects，GPL-3.0，官方 Nightly 资产，标注版权与源码地址）+ 3 条 `link`（LoudMax / TDR Nova / Voxengo SPAN） | 工坊与 zip 条目可一键安装；link 条目只打开官网 |
| 工坊安装/卸载 | 拉取 `manifest` → 校验 slug → 保存到 `data/mixer/workshop/<slug>`；卸载删除目录并清清单 | **通过**：本地 HTTP 目录跑通安装→清单→卸载；已安装的本地插件也能从市场卸载 |
| 更新检查 | GitHub Releases API + SemVer 比较 | 仓库尚无 Release，返回「仓库还没有发布版本」；新建的 Release 才能验证升级 |
| 下载与校验 | 下载到 `data/mixer/update`，校验 SHA-256 后写 `package.sha256` | 未验证（没有可用 Release 资产；构建环境 GitHub 资产 CDN 被拦截） |
| 安装更新 | 生成 `apply_update.bat`：等主程序退出 → 备份 `RecordOutMixer.bak` → 解压覆盖 → 失败回滚 → 重启 | 脚本已生成，**未做真实升级演练** |

因此「插件市场」的安装/卸载与真实下载均已验证；GPL 插件按许可证要求标注了版权与源码地址。
「自动更新」的检查与下载逻辑完成，**端到端升级未验证**，不能对外声称可用。

## 八、创意工坊（Python 效果器）

| 能力 | 实现 | 验证状态 |
| --- | --- | --- |
| 脚本格式 | `plugin.json`（元数据 + `code`），脚本内 `PARAMS` 声明参数、`process(x, sr, params)` 处理音频 | **通过**：保存、重新加载、参数解析 |
| 编译与执行 | 控制线程 `exec` 编译，音频线程非阻塞取快照；`import` / `open` / `eval` 等被拒绝 | **通过**：缺 `process`、`import` 均被明确拒绝 |
| 异常隔离 | 形状不符 / NaN 只旁通该插件并计数 | **通过**：输出与输入逐样本一致，`errors=1` |
| 引擎接入 | 内置效果之后、VST3 之前，`MixerEngine._process_block` 内执行 | **通过**：×2 测试插件实测放大 2.000 倍 |
| 离线试听 | 1 秒 440 Hz + 噪声，20 块，输出 RMS / 峰值 / 每块耗时 | **通过**：示例插件 +0.43 dB，0.011 ms/块 |
| 发布到市场 | 克隆自托管仓库 → 写 `plugins/<slug>` → 重建 `catalog.json` → commit/push → 同步随包目录 | **通过**：真实推送到 `Intro-Voice-Plugin-Market`（commit `1b48423`） |

回归：`tests\test_workshop.py`，19 项全部通过。

已知限制：Python 脚本不是完整沙箱，安装他人插件前需确认来源；脚本按块执行，无法使用需要跨块状态之外的宿主服务。

## 八、下一步（按路线图顺序）

1. 补 P0 许可证表，清除个人声纹与未授权权重后再谈公开分发。
2. P5：签名 Release + stable/beta 更新检查（哈希与签名双验证），先做「检查并打开 Release 页」的降级模式。
3. P6：插件目录仅做元数据索引；虚拟驱动单独立项，先做可行性原型与签名路径确认。
4. 工程侧：评估把 `mixer_dsp.py` 迁移为 C++ 处理器（保持现有接口与测试作为回归基线）。
5. 创意工坊：增加跨块状态 API、A/B 对比与波形预览；发布前做静态检查与签名（目前仅拒绝危险关键字）。
