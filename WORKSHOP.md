# 创意工坊 · 插件格式

在 Intro Voice 音拓 →「创意工坊」里可以直接写一个声音效果器：一段 Python 代码 + 一组参数声明。
保存后立即进入效果链（内置效果之后、VST3 之前），也可以一键发布到
[Intro-Voice-Plugin-Market](https://github.com/HandaQAQ-Code/Intro-Voice-Plugin-Market)。

## 从示例代码开始

页面右上角的「插入示例代码」下拉里内置了几个现成效果（空气感、立体声加宽、温和饱和、
低频收紧、空白模板）。点一下就把完整源码填入代码编辑器，改两行就能用；
也可以从「新建」开始写一个空的 process 骨架。页面右上角「接口文档」里有一份完整的
API 说明，方便随时查。

## 一个插件长什么样

```python
PARAMS = [
    {"id": "amount", "name": "空气感", "min": 0.0, "max": 1.0, "default": 0.35, "step": 0.01},
    {"id": "trim", "name": "输出", "min": -12.0, "max": 6.0, "default": 0.0, "step": 0.5, "unit": "dB"},
]

def process(x, sr, params):
    high = np.diff(x, axis=0, prepend=x[:1])
    y = x + high * (params["amount"] * 2.0)
    return (y * db_to_gain(params["trim"])).astype(np.float32)
```

落盘位置：`data/mixer/workshop/<slug>/plugin.json`，其中 `code` 字段保存脚本原文。

## API

| 名称 | 说明 |
| --- | --- |
| `x` | `float32` 数组，形状 `(块长度, 2)`，左右声道 |
| `sr` | 采样率，当前链路固定 48000 |
| `params` | `dict[str, float]`，按 `PARAMS` 归一化后的值 |
| `np` / `numpy` | NumPy |
| `db_to_gain(db)` | 分贝转线性增益 |
| `gain_to_db(gain)` | 线性增益转分贝 |
| `soft_clip(x, drive)` | 双曲正切软削波 |

`PARAMS` 每项：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 字母/数字/下划线，作为 `params` 的键 |
| `name` | 是 | 界面显示名 |
| `min` / `max` | 是 | 滑块范围 |
| `default` | 否 | 默认值，缺省取 `min` |
| `step` | 否 | 步进，缺省为范围的 1/100 |
| `unit` | 否 | 单位，例如 `dB`、`Hz`、`ms` |

## 运行时约定

- `process` 必须返回与输入**同形状**的数组；否则该插件本块被旁通并记一次错误。
- 输出出现 `NaN` / `Inf` 同样旁通，不影响其它插件和整条链路。
- 单块预算约 40 ms（1920 样本 @ 48 kHz）；「测试」会显示实际耗时。
- 控制线程负责编译，音频线程只做非阻塞取快照，回调里不会 `exec`。

## 安全边界

工坊插件在**音频进程内以本机权限**执行。代码检查会拒绝 `import`、`open(`、`eval(`、`exec(`、
`__import__`、`subprocess`、`socket`、`urllib`，但这不是完整的沙箱——安装或运行别人的插件前，
请确认来源可信。市场条目会标注作者与许可。

## 发布

1. 先「保存并启用」，确认插件可用；
2. 点「发布到市场」，确认许可与作者信息；
3. 客户端会克隆 `git@github.com:HandaQAQ-Code/Intro-Voice-Plugin-Market.git`，
   写入 `plugins/<slug>/plugin.json` 与 `README.md`，重建 `catalog.json` 并推送。

发布需要本机已配置该仓库的 SSH 权限。失败时会明确报错，且不会留下半成品提交。
