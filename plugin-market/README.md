# Intro Voice Plugin Market

Intro Voice 音拓（直播调音台）的插件市场目录。应用默认从这个仓库拉取 `catalog.json`。

## 目录结构

```text
catalog.json                 市场目录（应用读取的唯一入口）
plugins/<slug>/plugin.json   创意工坊插件（JSON + Python 效果代码）
plugins/<slug>/README.md     插件说明页
```

## 两类条目

| 类型 | 字段 | 安装行为 |
| --- | --- | --- |
| `workshop` | `manifest` 指向 `plugins/<slug>/plugin.json` | 下载清单并保存到 `data/mixer/workshop/<slug>/`，在「创意工坊」页启用 |
| `zip` | `download` 直链 + `sha256` | 下载、校验、解压到当前用户 VST3 目录 |
| `link` | `homepage` | 只打开官方页面，不分发二进制 |

## 发布创意工坊插件

在 Intro Voice →「创意工坊」写好后点「发布到市场」。客户端会：

1. 克隆本仓库到本地缓存；
2. 写入 `plugins/<slug>/plugin.json` 与 `README.md`；
3. 重建 `catalog.json` 并提交推送。

也可以手动提交 PR，只要保证 `catalog.json` 里的 `slug` 与 `plugins/<slug>` 一致。

## 版权

每个 `workshop` 条目必须带 `license` 与 `copyright`；`zip` 条目必须是上游官方发布物或已获再分发许可。
