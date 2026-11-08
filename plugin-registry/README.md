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

仓库里的条目全部是 `link` 类型：只提供官方下载页，不代用户下载或安装二进制。
原因见根目录 `COMPLIANCE_ROADMAP.md` 的 P0（许可证尚未核实）与 §2（不做未授权的二进制分发）。
