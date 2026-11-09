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

public sealed class PluginItem
{
    public int Index { get; init; }
    public string Name { get; init; } = "";
    public string Vendor { get; init; } = "";
    public string Path { get; init; } = "";
    public string ClassId { get; init; } = "";
    public bool Bypass { get; init; }
    public int ParamCount { get; init; }
    public string Header => Bypass ? $"{Name}（旁通）" : Name;
    public string Subtitle => Vendor;
    public override string ToString() => Header;
}

public sealed class PluginScanItem
{
    public string Name { get; init; } = "";
    public string Vendor { get; init; } = "";
    public string Path { get; init; } = "";
    public string ClassId { get; init; } = "";
    public override string ToString() => Vendor.Length > 0 ? $"{Name} · {Vendor}" : Name;
}

public sealed class VstParamItem : INotifyPropertyChanged
{
    private double _value;

    public int PluginIndex { get; init; }
    public int ParamIndex { get; init; }
    public uint ParamId { get; init; }
    public string Name { get; init; } = "";
    public bool ReadOnly { get; init; }

    public double Value
    {
        get => _value;
        set
        {
            if (Math.Abs(_value - value) < 1e-9) return;
            _value = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(Display));
        }
    }

    public string Display => ReadOnly ? $"{Value:0.###}（只读）" : $"{Value:0.###}";

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
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
