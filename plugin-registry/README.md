# 插件目录（market catalog）

本目录只放**元数据**，不放第三方插件二进制。

## 字段

| 字段 | 说明 |
| --- | --- |
| `slug` | 稳定标识，小写字母/数字/`-`，用于安装目录名与卸载 |
| `name` / `vendor` / `version` / `license` | 展示信息 |
| `type` | `link`：只打开官方页面；`zip`：可一键安装 |
| `homepage` | 官方页面（`link` 类型必填） |
| `download` | 直链 zip（`zip` 类型必填） |
| `sha256` | 安装包哈希，`zip` 类型必填，缺失会被拒绝 |
| `sizeMb` | 展示用大小 |
| `copyright` | 版权/许可声明，界面上展示，**GPL 类插件必须写** |
| `include` | 可选：只解压匹配前缀的条目（例如 `["Surge XT Effects.vst3/"]`） |
| `summary` | 一句话说明 |

## 一键安装的前置条件

1. **有再分发许可**，或直链指向**上游官方发布物**（不得镜像商业插件）。
2. `download` 指向的 zip 里包含 `.vst3` 目录或 `.dll`。
3. 填入准确的 `sha256`（安装时会校验，不匹配直接拒绝）。

满足条件时把条目改成：

```json
{
  "slug": "example-fx",
  "name": "Example FX",
  "vendor": "Example",
  "version": "1.0.0",
  "license": "MIT",
  "type": "zip",
  "homepage": "https://example.com/example-fx",
  "download": "https://example.com/releases/example-fx-1.0.0-win64.zip",
  "sha256": "…64 位十六进制…",
  "sizeMb": 12.4,
  "summary": "示例条目"
}
```

安装位置：`%LOCALAPPDATA%\Programs\Common\VST3\<slug>`（当前用户，不需要管理员权限）。
卸载会删除该目录并从已安装列表移除。

## 当前状态

- `surge-xt-effects`：`zip` 类型，指向上游官方 Nightly 资产，**GPL-3.0**，已标注版权与源码地址；
  安装时只解压 `Surge XT Effects.vst3/`，不会装成乐器版。已用真实下载验证（44.6 MB，SHA-256 一致）。
- 其余条目为 `link`：只打开官方页面，不分发二进制。

新增 `zip` 条目前请确认：有再分发许可（GPL/MIT 等）或直链来自上游官方发布物，并填写准确的 `sha256`。
