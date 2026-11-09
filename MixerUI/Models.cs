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
    public string Subtitle => SavedAt.Length > 0 ? $"保存于 {SavedAt}" : "已保存的预设";
    public override string ToString() => Name;
}

public sealed class MarketItem
{
    public string Slug { get; init; } = "";
    public string Name { get; init; } = "";
    public string Badge { get; init; } = "";
    public string Subtitle { get; init; } = "";
    public string Summary { get; init; } = "";
    public bool Installed { get; init; }
    public bool CanInstall { get; init; }
    public string ActionText => Installed ? "卸载" : CanInstall ? "安装" : "查看官网";
}
