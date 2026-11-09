# 第三方组件

| 组件                                   | 用途          | 是否随仓库分发  | 说明                                       |
| ------------------------------------ | ----------- | -------- | ---------------------------------------- |
| Steinberg VST3 SDK                   | 插件宿主与接口     | **否**    | 需自行获取；许可为 GPLv3 或 Steinberg 专有双授权，分发前须核对 |
| Microsoft.WindowsAppSDK              | WinUI 3 运行时 | 通过 NuGet | 构建时还原                                    |
| VB-Audio Virtual Cable               | 虚拟声卡路由      | **否**    | 用户自行安装，仓库与发行包都不包含                        |
| VoxLock / DPDFNet / FlashSR / LavaSR | 声纹、降噪、超分    | **否**    | 模型权重不随仓库分发                               |

`third_party/` 已在 `.gitignore` 中排除，避免把 SDK 源码与许可证义务带进本仓库。

