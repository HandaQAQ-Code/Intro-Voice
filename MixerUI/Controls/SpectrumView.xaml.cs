using System.Numerics;
using Microsoft.UI.Composition;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Hosting;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using Windows.UI;

namespace MixerUI.Controls;

/// <summary>
/// 32 段实时频谱：条形容器由代码生成，高度用 Composition 的 Scale 动画驱动，
/// 不参与 XAML 布局重算，20 fps 更新也不会掉帧。
/// </summary>
public sealed partial class SpectrumView : UserControl
{
    private const int BandCount = 32;
    private const double BarGap = 2.0;

    private readonly List<Rectangle> _bars = new();
    private readonly List<Visual> _visuals = new();
    private Compositor? _compositor;

    public SpectrumView()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        SizeChanged += (_, _) => LayoutBars();
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (_bars.Count > 0) return;
        _compositor = ElementCompositionPreview.GetElementVisual(this).Compositor;
        var brush = new LinearGradientBrush
        {
            StartPoint = new Windows.Foundation.Point(0, 1),
            EndPoint = new Windows.Foundation.Point(0, 0),
        };
        brush.GradientStops.Add(new GradientStop { Offset = 0.0, Color = Color.FromArgb(255, 214, 138, 40) });
        brush.GradientStops.Add(new GradientStop { Offset = 0.55, Color = Color.FromArgb(255, 235, 176, 76) });
        brush.GradientStops.Add(new GradientStop { Offset = 1.0, Color = Color.FromArgb(255, 255, 224, 160) });

        for (int i = 0; i < BandCount; i++)
        {
            var bar = new Rectangle
            {
                Fill = brush,
                RadiusX = 2,
                RadiusY = 2,
                Opacity = 0.92,
            };
            Bars.Children.Add(bar);
            _bars.Add(bar);
            var visual = ElementCompositionPreview.GetElementVisual(bar);
            visual.Scale = new Vector3(1f, 0.02f, 1f);
            _visuals.Add(visual);
        }
        LayoutBars();
    }

    private void LayoutBars()
    {
        if (_bars.Count == 0 || ActualWidth <= 0 || ActualHeight <= 0) return;
        double height = Math.Max(1, ActualHeight);
        double width = Math.Max(1, (ActualWidth - BarGap * (BandCount - 1)) / BandCount);
        for (int i = 0; i < _bars.Count; i++)
        {
            var bar = _bars[i];
            bar.Width = width;
            bar.Height = height;
            Canvas.SetLeft(bar, i * (width + BarGap));
            Canvas.SetTop(bar, 0);
            // 以底边为缩放原点
            _visuals[i].CenterPoint = new Vector3((float)(width / 2), (float)height, 0);
            _visuals[i].Scale = new Vector3(1f, Math.Max(0.02f, _visuals[i].Scale.Y), 1f);
        }
    }

    /// <summary>传入 0..1 的频带电平；内部做 120 ms 的平滑过渡。</summary>
    public void SetLevels(IReadOnlyList<float> levels)
    {
        if (_compositor is null || _visuals.Count == 0) return;
        int count = Math.Min(levels.Count, _visuals.Count);
        for (int i = 0; i < count; i++)
        {
            float target = Math.Clamp(levels[i], 0.015f, 1f);
            var animation = _compositor.CreateVector3KeyFrameAnimation();
            animation.InsertKeyFrame(1f, new Vector3(1f, target, 1f));
            animation.Duration = TimeSpan.FromMilliseconds(120);
            _visuals[i].StartAnimation("Scale", animation);
        }
    }
}
