using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace MixerUI;

public sealed class DeviceItem
{
    public int Id { get; init; }
    public string Label { get; init; } = "";
    public override string ToString() => Label;
}

public sealed class AiModeItem
{
    public string Value { get; init; } = "off";
    public string Label { get; init; } = "";
    public override string ToString() => Label;
}

public sealed class LatencyItem
{
    public string Value { get; init; } = "balanced";
    public string Label { get; init; } = "";
    public override string ToString() => Label;
}

/// <summary>设置页里「本机找到的 Python」一行：版本、路径、是否可用。</summary>
public sealed class PythonEntry
{
    public string Version { get; init; } = "";
    public string Path { get; init; } = "";
    public string State { get; init; } = "";
    public bool Usable { get; init; }
}

/// <summary>新手引导里的设备下拉项。</summary>
public sealed class GuideDevice
{
    public int Id { get; init; }
    public string Label { get; init; } = "";
    public bool IsCable { get; init; }

    /// <summary>CABLE 端点额外标注，帮用户一眼认出该选哪个。</summary>
    public string Display => IsCable ? Label + "   ← 选这个" : Label;

    public override string ToString() => Display;
}

/// <summary>新手引导「Python 环境」下拉项：版本、路径与 CUDA torch 状态。</summary>
public sealed class GuidePythonItem
{
    public string Path { get; init; } = "";
    public string Version { get; init; } = "";
    public bool Compatible { get; init; }
    public bool Cuda { get; init; }
    public string TorchVersion { get; init; } = "";

    public string Display
    {
        get
        {
            string state = Cuda
                ? $"✓ CUDA torch {TorchVersion}"
                : Compatible ? "未装 CUDA torch" : "版本不兼容（需 3.11）";
            return $"Python {Version} · {state} · {Path}";
        }
    }

    public override string ToString() => Display;
}

/// <summary>新手引导「音质档」下拉项；不可用的档位会在文字里说明原因。</summary>
public sealed class GuideQualityItem
{
    public string Value { get; init; } = "off";
    public string Label { get; init; } = "";
    public bool Enabled { get; init; } = true;

    public string Display => Enabled ? Label : Label + "   （当前不可用）";

    public override string ToString() => Display;
}

public sealed class PresetItem
{
    public string Name { get; init; } = "";
    public string SavedAt { get; init; } = "";
    public int Plugins { get; init; }
    public string Ai { get; init; } = "AI 关";
    public string Effects { get; init; } = "";
    public int Workshop { get; init; }

    public string Subtitle => SavedAt.Length > 0 ? $"保存于 {SavedAt}" : "已保存的预设";

    /// <summary>载入前的参考数据：这套预设到底包含什么。</summary>
    public string Detail
    {
        get
        {
            var parts = new List<string> { $"{Plugins} 个插件", Ai };
            parts.Add(Effects.Length > 0 ? Effects : "效果器旁通");
            if (Workshop > 0) parts.Add($"工坊 {Workshop} 个");
            parts.Add(Subtitle);
            return string.Join(" · ", parts);
        }
    }

    public override string ToString() => Name;
}

public sealed class MarketItem
{
    public string Slug { get; init; } = "";
    public string Name { get; init; } = "";
    public string Badge { get; init; } = "";
    public string Subtitle { get; init; } = "";
    public string Summary { get; init; } = "";
    public string Kind { get; init; } = "link";
    public bool Installed { get; init; }
    public bool CanInstall { get; init; }
    public bool IsWorkshop => Kind == "workshop";
    public string ActionText => Installed ? "卸载" : CanInstall || IsWorkshop ? "安装" : "查看官网";
}

public sealed class WorkshopItem
{
    public string Slug { get; init; } = "";
    public string Name { get; init; } = "";
    public string Author { get; init; } = "";
    public string Version { get; init; } = "";
    public string License { get; init; } = "";
    public string Description { get; init; } = "";
    public string Code { get; init; } = "";
    public string CompileError { get; init; } = "";
    public bool Enabled { get; init; }
    public List<WorkshopParam> Params { get; init; } = new();

    public string Subtitle
    {
        get
        {
            var state = Enabled ? "已启用" : "未启用";
            var error = CompileError.Length > 0 ? " · 编译失败" : "";
            return $"v{Version} · {Params.Count} 参数 · {state}{error}";
        }
    }

    public override string ToString() => Name;
}

public sealed class WorkshopParam : INotifyPropertyChanged
{
    private double _value;

    public string Id { get; init; } = "";
    public string Name { get; init; } = "";
    public double Min { get; init; }
    public double Max { get; init; } = 1;
    public double Step { get; init; }
    public string Unit { get; init; } = "";

    public double Value
    {
        get => _value;
        set
        {
            if (Math.Abs(_value - value) < 1e-9) return;
            _value = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Value)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Display)));
        }
    }

    public string Display => Unit.Length > 0 ? $"{Value:0.##} {Unit}" : $"{Value:0.##}";

    public event PropertyChangedEventHandler? PropertyChanged;
}
