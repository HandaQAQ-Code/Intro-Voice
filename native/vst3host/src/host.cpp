// VST3 host implementation for the Record Out mixer.
// Built against the Steinberg VST3 SDK (MIT licensed). See third_party/vst3sdk/LICENSE.txt.
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <objbase.h>
#include <algorithm>
#include <atomic>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "vst3host.h"

#include "public.sdk/source/common/memorystream.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/module.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include "public.sdk/source/vst/hosting/plugprovider.h"
#include "public.sdk/source/vst/utility/stringconvert.h"
#include "public.sdk/source/vst/utility/uid.h"

#include "pluginterfaces/base/funknownimpl.h"
#include "pluginterfaces/gui/iplugview.h"
#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivsthostapplication.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include "pluginterfaces/vst/vstspeaker.h"

namespace Vst = Steinberg::Vst;
namespace U = Steinberg::U;

using Steinberg::int32;
using Steinberg::int64;
using Steinberg::IPtr;
using Steinberg::kResultOk;
using Steinberg::kResultTrue;
using Steinberg::kResultFalse;
using Steinberg::tresult;
using Steinberg::owned;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
static void setErr(char* err, int len, const std::string& text)
{
    if (!err || len <= 0)
        return;
    int n = (int)text.size();
    if (n >= len)
        n = len - 1;
    std::memcpy(err, text.data(), (size_t)n);
    err[n] = 0;
}

static std::string jsonEscape(const std::string& in)
{
    std::string out;
    out.reserve(in.size() + 8);
    for (char c : in)
    {
        switch (c)
        {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if ((unsigned char)c < 0x20)
                {
                    char buf[7];
                    std::snprintf(buf, sizeof buf, "\\u%04x", (unsigned char)c);
                    out += buf;
                }
                else
                {
                    out += c;
                }
        }
    }
    return out;
}

static std::string jstr(const std::string& value)
{
    return "\"" + jsonEscape(value) + "\"";
}

static std::string tcharToUtf8(const Vst::TChar* text)
{
    if (!text)
        return {};
    return Vst::StringConvert::convert(text);
}

static std::wstring utf8ToWide(const std::string& utf8)
{
    std::u16string u16 = Vst::StringConvert::convert(utf8);
    std::wstring w;
    w.resize(u16.size());
    for (size_t i = 0; i < u16.size(); ++i)
        w[i] = (wchar_t)u16[i];
    return w;
}

// ---------------------------------------------------------------------------
// editor window context
// ---------------------------------------------------------------------------
struct EditorContext;

class PlugFrame : public U::Implements<U::Directly<Steinberg::IPlugFrame>>
{
public:
    explicit PlugFrame(EditorContext* context) : ctx(context) {}
    tresult PLUGIN_API resizeView(Steinberg::IPlugView* view, Steinberg::ViewRect* newSize) override;
    EditorContext* ctx;
};

struct EditorContext
{
    Vst::PlugProvider* provider {nullptr};
    std::string title;
    std::string error;
    HWND hwnd {nullptr};
    HANDLE ready {nullptr};
    bool failed {false};
    bool closing {false};
    std::thread thread;
    IPtr<Steinberg::IPlugView> view;
    IPtr<Steinberg::IPlugFrame> frame;
};

static const wchar_t* kEditorClass = L"RecordOutVst3Editor";
static std::atomic<bool> gEditorClassRegistered {false};

static LRESULT CALLBACK editorWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam)
{
    EditorContext* ctx = reinterpret_cast<EditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg)
    {
        case WM_CLOSE:
            if (ctx && ctx->view)
            {
                ctx->view->removed();
                ctx->view.reset();
            }
            DestroyWindow(hwnd);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
        case WM_SIZE:
            if (ctx && ctx->view && !ctx->closing)
            {
                Steinberg::ViewRect r {};
                r.right = (int32)LOWORD(lParam);
                r.bottom = (int32)HIWORD(lParam);
                ctx->view->onSize(&r);
            }
            return 0;
        default:
            break;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

tresult PLUGIN_API PlugFrame::resizeView(Steinberg::IPlugView* view, Steinberg::ViewRect* newSize)
{
    if (!ctx || !ctx->hwnd || !newSize)
        return kResultFalse;
    SetWindowPos(ctx->hwnd, nullptr, 0, 0, newSize->right - newSize->left,
                 newSize->bottom - newSize->top, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    return kResultOk;
}

static void editorThreadInner(EditorContext* ctx)
{
    auto signalReady = [ctx] { if (ctx->ready) SetEvent(ctx->ready); };
    Vst::IEditController* controller = ctx->provider ? ctx->provider->getControllerPtr().get() : nullptr;
    if (!controller)
    {
        ctx->failed = true;
        ctx->error = "插件没有编辑器控制器";
        signalReady();
        return;
    }
    ctx->view = controller->createView(Vst::ViewType::kEditor);
    if (!ctx->view)
    {
        ctx->failed = true;
        ctx->error = "插件没有提供编辑器视图";
        signalReady();
        return;
    }
    if (ctx->view->isPlatformTypeSupported(Steinberg::kPlatformTypeHWND) != kResultTrue)
    {
        ctx->failed = true;
        ctx->error = "插件编辑器不支持 HWND 窗口";
        ctx->view.reset();
        signalReady();
        return;
    }

    Steinberg::ViewRect rect {};
    if (ctx->view->getSize(&rect) != kResultTrue || rect.right <= 0 || rect.bottom <= 0)
    {
        rect.left = 0;
        rect.top = 0;
        rect.right = 960;
        rect.bottom = 640;
    }

    if (!gEditorClassRegistered.exchange(true))
    {
        WNDCLASSEXW wc {};
        wc.cbSize = sizeof wc;
        wc.style = CS_DBLCLKS;
        wc.lpfnWndProc = editorWndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.hCursor = LoadCursorW(nullptr, (LPCWSTR)IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = kEditorClass;
        RegisterClassExW(&wc);
    }

    std::wstring wtitle = utf8ToWide(ctx->title.empty() ? "VST3 插件" : ctx->title);
    DWORD style = WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN;
    RECT wr {0, 0, rect.right - rect.left, rect.bottom - rect.top};
    AdjustWindowRect(&wr, style, FALSE);
    int w = wr.right - wr.left, h = wr.bottom - wr.top;
    int sw = GetSystemMetrics(SM_CXSCREEN), sh = GetSystemMetrics(SM_CYSCREEN);
    HWND hwnd = CreateWindowExW(0, kEditorClass, wtitle.c_str(), style,
                                (sw - w) / 2, (sh - h) / 2, w, h, nullptr, nullptr,
                                GetModuleHandleW(nullptr), nullptr);
    if (!hwnd)
    {
        ctx->failed = true;
        ctx->error = "创建插件窗口失败";
        ctx->view.reset();
        signalReady();
        return;
    }
    ctx->hwnd = hwnd;
    SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(ctx));

    ctx->frame = owned(new PlugFrame(ctx));
    ctx->view->setFrame(ctx->frame);
    if (ctx->view->attached(hwnd, Steinberg::kPlatformTypeHWND) != kResultOk)
    {
        ctx->failed = true;
        ctx->error = "插件编辑器无法附着到窗口";
        ctx->view->setFrame(nullptr);
        ctx->view.reset();
        DestroyWindow(hwnd);
        ctx->hwnd = nullptr;
        signalReady();
        return;
    }
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    signalReady();

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0)
    {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    if (ctx->view)
    {
        ctx->view->removed();
        ctx->view.reset();
    }
    ctx->frame = nullptr;
    ctx->hwnd = nullptr;
}

static void editorThreadMain(EditorContext* ctx)
{
    ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    static std::once_flag dpiOnce;
    std::call_once(dpiOnce, [] {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    });
    editorThreadInner(ctx);
    if (ctx->ready)
        SetEvent(ctx->ready);
    ::CoUninitialize();
}

// ---------------------------------------------------------------------------
// plugin instance + chain
// ---------------------------------------------------------------------------
class ComponentHandler;

struct PluginInstance
{
    std::string path;
    std::string classId;
    std::string name;
    std::string vendor;
    std::string version;
    std::string category;
    VST3::Hosting::Module::Ptr module;
    std::unique_ptr<Vst::PlugProvider> provider;
    IPtr<Vst::IComponent> component;
    IPtr<Vst::IEditController> controller;
    IPtr<Vst::IAudioProcessor> processor;
    IPtr<ComponentHandler> handler;
    std::unique_ptr<Vst::ParameterChangeTransfer> transfer;
    std::unique_ptr<Vst::ParameterChanges> paramChanges;
    std::unique_ptr<EditorContext> editor;
    bool bypass {false};
    bool active {false};
    int32 latency {0};
    std::string lastError;
};

class ComponentHandler : public U::Implements<U::Directly<Vst::IComponentHandler, Vst::IComponentHandler2>>
{
public:
    ComponentHandler(Vst::IEditController* c, Vst::ParameterChangeTransfer* t) : controller(c), transfer(t) {}

    // IComponentHandler
    tresult PLUGIN_API beginEdit(Vst::ParamID) override { return kResultOk; }
    tresult PLUGIN_API performEdit(Vst::ParamID id, Vst::ParamValue value) override
    {
        if (controller)
            controller->setParamNormalized(id, value);
        if (transfer && std::isfinite((double)value))
            transfer->addChange(id, value, 0);
        return kResultOk;
    }
    tresult PLUGIN_API endEdit(Vst::ParamID) override { return kResultOk; }
    tresult PLUGIN_API restartComponent(int32) override { return kResultOk; }

    // IComponentHandler2
    tresult PLUGIN_API setDirty(Steinberg::TBool) override { return kResultOk; }
    tresult PLUGIN_API requestOpenEditor(Steinberg::FIDString) override { return kResultOk; }
    tresult PLUGIN_API startGroupEdit() override { return kResultOk; }
    tresult PLUGIN_API finishGroupEdit() override { return kResultOk; }

    IPtr<Vst::IEditController> controller;
    Vst::ParameterChangeTransfer* transfer {nullptr};
};

struct VstChain
{
    std::mutex mutex;
    std::vector<std::unique_ptr<PluginInstance>> plugins;
    IPtr<Vst::IHostApplication> hostApp;
    std::string lastError;
    bool faulted {false};
    bool running {false};
    bool setupDone {false};
    int32 sampleRate {48000};
    int32 blockFrames {2048};
    std::atomic<int32> latency {0};
};

// ---------------------------------------------------------------------------
// instance creation
// ---------------------------------------------------------------------------
static bool classIsEffect(const VST3::Hosting::ClassInfo& info)
{
    const std::string& cat = info.category();
    const auto& subs = info.subCategories();
    bool isInstrument = false;
    bool isFx = false;
    for (const auto& s : subs)
    {
        if (s == "Instrument" || s == "Synthesizer" || s == "Sampler")
            isInstrument = true;
        if (s == "Fx" || s == "Audio Effect" || s == "Audio Effect Class")
            isFx = true;
    }
    if (isInstrument)
        return false;
    if (cat.find("Audio Effect") != std::string::npos)
        return true;
    // Modern VST3 modules report "Audio Module Class" plus Fx sub-categories.
    return isFx && cat.find("Audio Module") != std::string::npos;
}

static bool createInstance(VstChain* chain, const std::string& path, const std::string& classId,
                           std::unique_ptr<PluginInstance>& out, std::string& error)
{
    PluginInstance inst;
    inst.path = path;
    inst.classId = classId;

    std::string modErr;
    inst.module = VST3::Hosting::Module::create(path, modErr);
    if (!inst.module)
    {
        error = "加载插件模块失败：" + modErr + " (" + path + ")";
        return false;
    }

    VST3::Hosting::PluginFactory factory = inst.module->getFactory();
    if (chain->hostApp)
        factory.setHostContext(chain->hostApp.get());

    const auto classes = factory.classInfos();
    const VST3::Hosting::ClassInfo* found = nullptr;
    for (const auto& info : classes)
    {
        if (info.ID().toString() == classId)
        {
            found = &info;
            break;
        }
    }
    if (!found)
    {
        error = "插件模块中找不到类 " + classId;
        return false;
    }
    if (!classIsEffect(*found))
    {
        error = "不是音频效果器插件：" + found->name() + " (" + found->category() + ")";
        return false;
    }

    inst.name = found->name();
    inst.vendor = found->vendor();
    inst.version = found->version();
    inst.category = found->category();

    inst.provider = std::make_unique<Vst::PlugProvider>(factory, *found, false);
    if (!inst.provider->initialize())
    {
        error = "插件初始化失败：" + inst.name;
        return false;
    }
    inst.component = owned(inst.provider->getComponent());
    inst.controller = owned(inst.provider->getController());
    if (!inst.component)
    {
        error = "插件没有音频组件：" + inst.name;
        return false;
    }
    inst.processor = nullptr;
    {
        Vst::IAudioProcessor* proc = nullptr;
        if (inst.component->queryInterface(Vst::IAudioProcessor::iid, (void**)&proc) != kResultTrue || !proc)
        {
            error = "插件不支持音频处理：" + inst.name;
            return false;
        }
        inst.processor = owned(proc);
    }
    if (!inst.processor)
    {
        error = "插件不支持音频处理：" + inst.name;
        return false;
    }
    if (!inst.controller)
    {
        error = "插件没有编辑控制器（无法调整参数）：" + inst.name;
        return false;
    }

    inst.transfer = std::make_unique<Vst::ParameterChangeTransfer>(128);
    inst.paramChanges = std::make_unique<Vst::ParameterChanges>(64);
    inst.handler = owned(new ComponentHandler(inst.controller.get(), inst.transfer.get()));
    inst.controller->setComponentHandler(inst.handler.get());

    out = std::make_unique<PluginInstance>(std::move(inst));
    return true;
}

// ---------------------------------------------------------------------------
// setup / teardown
// ---------------------------------------------------------------------------
static void deactivateAll(VstChain* chain)
{
    for (auto& p : chain->plugins)
    {
        if (p->processor && p->active)
        {
            p->processor->setProcessing(false);
            if (p->component)
                p->component->setActive(false);
            p->active = false;
        }
    }
    chain->setupDone = false;
}

static int32 sumLatency(VstChain* chain)
{
    int64 total = 0;
    for (auto& p : chain->plugins)
        total += p->latency;
    if (total > INT32_MAX)
        total = INT32_MAX;
    return (int32)total;
}

static bool setupChain(VstChain* chain, int32 sampleRate, int32 blockFrames, std::string& error)
{
    chain->latency = 0;
    chain->setupDone = false;
    if (chain->plugins.empty())
    {
        chain->sampleRate = sampleRate;
        chain->blockFrames = blockFrames;
        chain->setupDone = true;
        return true;
    }
    for (auto& p : chain->plugins)
    {
        auto* comp = p->component.get();
        auto* proc = p->processor.get();

        int32 inCount = comp->getBusCount(Vst::kAudio, Vst::kInput);
        int32 outCount = comp->getBusCount(Vst::kAudio, Vst::kOutput);
        if (outCount <= 0)
        {
            error = "插件没有音频输出总线：" + p->name;
            return false;
        }

        std::vector<Vst::SpeakerArrangement> ins((size_t)std::max<int32>(inCount, 0), Vst::SpeakerArr::kStereo);
        std::vector<Vst::SpeakerArrangement> outs((size_t)outCount, Vst::SpeakerArr::kStereo);
        if (inCount > 0 && proc->setBusArrangements(ins.data(), inCount, outs.data(), outCount) != kResultTrue)
        {
            error = "插件不支持立体声布局（需要 2 声道输入/输出）：" + p->name;
            return false;
        }

        // Only the main input/output bus is active; side-chain buses get no buffers.
        for (int32 i = 0; i < inCount; ++i)
            comp->activateBus(Vst::kAudio, Vst::kInput, i, i == 0);
        for (int32 i = 0; i < outCount; ++i)
            comp->activateBus(Vst::kAudio, Vst::kOutput, i, i == 0);

        Vst::ProcessSetup ps {};
        ps.processMode = Vst::kRealtime;
        ps.symbolicSampleSize = Vst::kSample32;
        ps.maxSamplesPerBlock = blockFrames;
        ps.sampleRate = (Vst::SampleRate)sampleRate;
        if (proc->setupProcessing(ps) != kResultOk)
        {
            error = "插件拒绝音频格式（48 kHz / float32 / 立体声）：" + p->name;
            return false;
        }
        if (comp->setActive(true) != kResultOk)
        {
            error = "插件无法激活：" + p->name;
            return false;
        }
        p->active = true;
        p->latency = (int32)proc->getLatencySamples();
        p->lastError.clear();
    }
    chain->sampleRate = sampleRate;
    chain->blockFrames = blockFrames;
    chain->setupDone = true;
    chain->latency = sumLatency(chain);
    return true;
}

static void destroyEditor(PluginInstance& p)
{
    if (!p.editor)
        return;
    if (p.editor->hwnd)
        PostMessageW(p.editor->hwnd, WM_CLOSE, 0, 0);
    if (p.editor->thread.joinable())
        p.editor->thread.join();
    p.editor.reset();
}

// ---------------------------------------------------------------------------
// exported API
// ---------------------------------------------------------------------------
extern "C" {

VSTHOST_API int vst_scan_directory(const char* utf8_dir, VstScanCallback cb, void* user,
                                   char* err, int err_len)
{
    if (!utf8_dir || !cb)
    {
        setErr(err, err_len, "扫描参数无效");
        return -1;
    }
    namespace fs = std::filesystem;
    std::string dirStr = utf8_dir;
    fs::path dir = fs::u8path(dirStr);
    std::error_code ec;
    if (!fs::is_directory(dir, ec))
    {
        setErr(err, err_len, "目录不存在：" + dirStr);
        return -1;
    }
    fs::recursive_directory_iterator it(dir, fs::directory_options::skip_permission_denied, ec);
    if (ec)
    {
        setErr(err, err_len, "无法读取目录：" + dirStr);
        return -1;
    }
    fs::recursive_directory_iterator end;
    int count = 0;
    while (it != end)
    {
        fs::path path = it->path();
        std::error_code ec2;
        std::string ext = path.extension().u8string();
        if (ext == ".vst3")
        {
            // A bundle or a flat dll; Module::create handles both.
            std::string moduleErr;
            auto module = VST3::Hosting::Module::create(path.u8string(), moduleErr);
            if (!module)
            {
                std::string json = "{\"error\":" + jstr(moduleErr) + ",\"path\":" +
                                   jstr(path.u8string()) + "}";
                cb(json.c_str(), user);
            }
            else
            {
                const auto infos = module->getFactory().classInfos();
                for (const auto& info : infos)
                {
                    std::string json = "{\"path\":" + jstr(path.u8string()) +
                                       ",\"classId\":" + jstr(info.ID().toString()) +
                                       ",\"name\":" + jstr(info.name()) +
                                       ",\"vendor\":" + jstr(info.vendor()) +
                                       ",\"version\":" + jstr(info.version()) +
                                       ",\"category\":" + jstr(info.category()) +
                                       ",\"subCategories\":" + jstr(info.subCategoriesString()) +
                                       ",\"sdkVersion\":" + jstr(info.sdkVersion()) +
                                       ",\"isEffect\":" + (classIsEffect(info) ? "true" : "false") + "}";
                    cb(json.c_str(), user);
                    ++count;
                }
            }
        }
        if (it->is_directory())
        {
            // Don't descend into bundles; their contents are not plugins themselves.
            std::string name = path.filename().u8string();
            if (ext == ".vst3" || name == "Contents")
                it.disable_recursion_pending();
        }
        it.increment(ec2);
        if (ec2)
            break;
    }
    return count;
}

VSTHOST_API VstChainHandle vst_chain_create(void)
{
    ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    auto* chain = new VstChain();
    chain->hostApp = owned(new Vst::HostApplication());
    Vst::PluginContextFactory::instance().setPluginContext(chain->hostApp.get());
    return chain;
}

VSTHOST_API void vst_chain_destroy(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return;
    {
        std::lock_guard<std::mutex> lock(chain->mutex);
        chain->running = false;
        for (auto& p : chain->plugins)
            destroyEditor(*p);
        deactivateAll(chain);
        chain->plugins.clear();
    }
    delete chain;
}

VSTHOST_API int vst_chain_add(VstChainHandle handle, const char* utf8_path, const char* class_id,
                              char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || !utf8_path || !class_id)
    {
        setErr(err, err_len, "插件参数无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    std::unique_ptr<PluginInstance> inst;
    std::string error;
    if (!createInstance(chain, utf8_path, class_id, inst, error))
    {
        setErr(err, err_len, error);
        return -1;
    }
    chain->plugins.push_back(std::move(inst));
    chain->setupDone = false;
    return (int)chain->plugins.size() - 1;
}

VSTHOST_API int vst_chain_remove(VstChainHandle handle, int index, char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
    {
        setErr(err, err_len, "链路无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
    {
        setErr(err, err_len, "插件序号超出范围");
        return -1;
    }
    deactivateAll(chain);
    destroyEditor(*chain->plugins[(size_t)index]);
    chain->plugins.erase(chain->plugins.begin() + index);
    chain->setupDone = false;
    chain->latency = sumLatency(chain);
    return 0;
}

VSTHOST_API int vst_chain_move(VstChainHandle handle, int from, int to, char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
    {
        setErr(err, err_len, "链路无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    int n = (int)chain->plugins.size();
    if (from < 0 || from >= n || to < 0 || to >= n)
    {
        setErr(err, err_len, "插件序号超出范围");
        return -1;
    }
    if (from == to)
        return 0;
    deactivateAll(chain);
    auto item = std::move(chain->plugins[(size_t)from]);
    chain->plugins.erase(chain->plugins.begin() + from);
    chain->plugins.insert(chain->plugins.begin() + to, std::move(item));
    chain->setupDone = false;
    return 0;
}

VSTHOST_API int vst_chain_count(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return 0;
    std::lock_guard<std::mutex> lock(chain->mutex);
    return (int)chain->plugins.size();
}

VSTHOST_API int vst_chain_set_bypass(VstChainHandle handle, int index, int bypass)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    chain->plugins[(size_t)index]->bypass = bypass != 0;
    return 0;
}

VSTHOST_API int vst_chain_is_bypass(VstChainHandle handle, int index)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    return chain->plugins[(size_t)index]->bypass ? 1 : 0;
}

VSTHOST_API int vst_chain_info(VstChainHandle handle, int index, char* json, int json_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || !json || json_len <= 0)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& p = *chain->plugins[(size_t)index];
    std::string text = "{\"name\":" + jstr(p.name) + ",\"vendor\":" + jstr(p.vendor) +
                       ",\"version\":" + jstr(p.version) + ",\"category\":" + jstr(p.category) +
                       ",\"path\":" + jstr(p.path) + ",\"classId\":" + jstr(p.classId) +
                       ",\"bypass\":" + (p.bypass ? "true" : "false") +
                       ",\"latency\":" + std::to_string(p.latency) +
                       ",\"editorOpen\":" + ((p.editor && p.editor->hwnd) ? "true" : "false") + "}";
    setErr(json, json_len, text);
    return 0;
}

VSTHOST_API int vst_chain_param_count(VstChainHandle handle, int index)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& c = chain->plugins[(size_t)index]->controller;
    return c ? c->getParameterCount() : 0;
}

VSTHOST_API int vst_chain_param_info(VstChainHandle handle, int index, int param,
                                     char* json, int json_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || !json || json_len <= 0)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& c = chain->plugins[(size_t)index]->controller;
    if (!c)
        return -1;
    Vst::ParameterInfo info {};
    if (c->getParameterInfo(param, info) != kResultOk)
        return -1;
    double value = c->getParamNormalized(info.id);
    std::string text = "{\"index\":" + std::to_string(param) +
                       ",\"id\":" + std::to_string((unsigned long long)info.id) +
                       ",\"title\":" + jstr(tcharToUtf8(info.title)) +
                       ",\"shortTitle\":" + jstr(tcharToUtf8(info.shortTitle)) +
                       ",\"units\":" + jstr(tcharToUtf8(info.units)) +
                       ",\"stepCount\":" + std::to_string(info.stepCount) +
                       ",\"default\":" + std::to_string(info.defaultNormalizedValue) +
                       ",\"flags\":" + std::to_string((unsigned long long)info.flags) +
                       ",\"value\":" + std::to_string(value) + "}";
    setErr(json, json_len, text);
    return 0;
}

VSTHOST_API int vst_chain_get_param(VstChainHandle handle, int index, int param)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& c = chain->plugins[(size_t)index]->controller;
    if (!c)
        return -1;
    Vst::ParameterInfo info {};
    if (c->getParameterInfo(param, info) != kResultOk)
        return -1;
    double value = c->getParamNormalized(info.id);
    return (int)std::lround(value * 1000.0);
}

VSTHOST_API int vst_chain_set_param(VstChainHandle handle, int index, int param, double normalized)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& p = *chain->plugins[(size_t)index];
    if (!p.controller)
        return -1;
    Vst::ParameterInfo info {};
    if (p.controller->getParameterInfo(param, info) != kResultOk)
        return -1;
    if (normalized < 0) normalized = 0;
    if (normalized > 1) normalized = 1;
    p.controller->setParamNormalized(info.id, normalized);
    if (p.transfer)
        p.transfer->addChange(info.id, (Vst::ParamValue)normalized, 0);
    return 0;
}

VSTHOST_API int vst_chain_setup(VstChainHandle handle, int sample_rate, int block_frames,
                                char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
    {
        setErr(err, err_len, "链路无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    chain->running = false;
    deactivateAll(chain);
    std::string error;
    if (!setupChain(chain, sample_rate, block_frames, error))
    {
        setErr(err, err_len, error);
        chain->faulted = true;
        chain->lastError = error;
        return -1;
    }
    chain->faulted = false;
    chain->lastError.clear();
    return 0;
}

VSTHOST_API int vst_chain_start(VstChainHandle handle, char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
    {
        setErr(err, err_len, "链路无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (!chain->setupDone)
    {
        setErr(err, err_len, "插件链尚未完成音频格式协商");
        return -1;
    }
    for (auto& p : chain->plugins)
    {
        if (p->processor)
            p->processor->setProcessing(true);
    }
    chain->running = true;
    return 0;
}

VSTHOST_API void vst_chain_stop(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return;
    std::lock_guard<std::mutex> lock(chain->mutex);
    for (auto& p : chain->plugins)
    {
        if (p->processor)
            p->processor->setProcessing(false);
    }
    chain->running = false;
}

VSTHOST_API int vst_chain_process(VstChainHandle handle, const float* left_in, const float* right_in,
                                  float* left_out, float* right_out, int frames)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || frames <= 0)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (!chain->running || !chain->setupDone)
        return -1;

    // Copies are done by the caller; this chain processes in place in left_out/right_out.
    if (left_out != left_in && left_out)
        std::memcpy(left_out, left_in, sizeof(float) * (size_t)frames);
    if (right_out != right_in && right_out)
        std::memcpy(right_out, right_in ? right_in : left_in, sizeof(float) * (size_t)frames);

    if (frames > chain->blockFrames)
        return -1;

    int32 channels = 2;
    float* channelBufs[2] = {left_out, right_out};
    float** channelPtrs = channelBufs;

    for (size_t pi = 0; pi < chain->plugins.size(); ++pi)
    {
        auto& p = *chain->plugins[pi];
        if (p.bypass || !p.processor || !p.active)
            continue;

        Vst::AudioBusBuffers inBus {};
        inBus.numChannels = channels;
        inBus.channelBuffers32 = channelPtrs;
        Vst::AudioBusBuffers outBus {};
        outBus.numChannels = channels;
        outBus.channelBuffers32 = channelPtrs;

        if (p.paramChanges)
        {
            p.paramChanges->clearQueue();
            if (p.transfer)
                p.transfer->transferChangesTo(*p.paramChanges);
        }

        Vst::ProcessContext context {};
        context.state = Vst::ProcessContext::kPlaying | Vst::ProcessContext::kContTimeValid;
        context.sampleRate = (Vst::SampleRate)chain->sampleRate;
        static thread_local int64 samplePos = 0;
        context.projectTimeSamples = samplePos;
        context.continousTimeSamples = samplePos;
        samplePos += frames;

        Vst::ProcessData data {};
        data.processContext = &context;
        data.processMode = Vst::kRealtime;
        data.symbolicSampleSize = Vst::kSample32;
        data.numSamples = frames;
        data.numInputs = 1;
        data.inputs = &inBus;
        data.numOutputs = 1;
        data.outputs = &outBus;
        data.inputParameterChanges = p.paramChanges.get();
        data.outputParameterChanges = nullptr;
        data.inputEvents = nullptr;
        data.outputEvents = nullptr;

        tresult result = p.processor->process(data);
        if (result != kResultOk)
        {
            chain->faulted = true;
            chain->lastError = "插件处理失败：" + p.name;
            p.lastError = chain->lastError;
            return -1;
        }
    }
    return 0;
}

VSTHOST_API int vst_chain_latency(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    return chain ? chain->latency.load() : 0;
}

VSTHOST_API const char* vst_chain_last_error(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return "链路无效";
    return chain->lastError.c_str();
}

VSTHOST_API int vst_chain_is_faulted(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    return chain && chain->faulted ? 1 : 0;
}

VSTHOST_API void vst_chain_clear_fault(VstChainHandle handle)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return;
    std::lock_guard<std::mutex> lock(chain->mutex);
    chain->faulted = false;
    chain->lastError.clear();
}

VSTHOST_API int64_t vst_chain_open_editor(VstChainHandle handle, int index, char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
    {
        setErr(err, err_len, "链路无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
    {
        setErr(err, err_len, "插件序号超出范围");
        return -1;
    }
    auto& p = *chain->plugins[(size_t)index];
    if (p.editor && !p.editor->hwnd)
    {
        if (p.editor->thread.joinable())
            p.editor->thread.join();
        p.editor.reset();
    }
    if (p.editor && p.editor->hwnd)
        return (int64_t)(intptr_t)p.editor->hwnd;

    p.editor = std::make_unique<EditorContext>();
    p.editor->provider = p.provider.get();
    p.editor->title = p.name;
    p.editor->closing = false;
    p.editor->ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    p.editor->thread = std::thread(editorThreadMain, p.editor.get());
    WaitForSingleObject(p.editor->ready, 5000);
    CloseHandle(p.editor->ready);
    p.editor->ready = nullptr;
    if (p.editor->failed || !p.editor->hwnd)
    {
        std::string message = p.editor->error.empty() ? "插件编辑器创建失败" : p.editor->error;
        if (p.editor->thread.joinable())
            p.editor->thread.join();
        p.editor.reset();
        setErr(err, err_len, message);
        return -1;
    }
    return (int64_t)(intptr_t)p.editor->hwnd;
}

VSTHOST_API int vst_chain_close_editor(VstChainHandle handle, int index)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    destroyEditor(*chain->plugins[(size_t)index]);
    return 0;
}

VSTHOST_API int vst_chain_editor_open(VstChainHandle handle, int index)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain)
        return 0;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return 0;
    auto& p = *chain->plugins[(size_t)index];
    return (p.editor && p.editor->hwnd) ? 1 : 0;
}

VSTHOST_API int vst_chain_get_state(VstChainHandle handle, int index, void** data, int* size)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || !data || !size)
        return -1;
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
        return -1;
    auto& p = *chain->plugins[(size_t)index];
    if (!p.component || !p.controller)
        return -1;

    auto componentState = owned(new Steinberg::MemoryStream());
    if (p.component->getState(componentState) != kResultOk)
        return -1;
    auto controllerState = owned(new Steinberg::MemoryStream());
    tresult cr = p.controller->getState(controllerState);

    int32 compSize = (int32)componentState->getSize();
    int32 ctrlSize = cr == kResultOk ? (int32)controllerState->getSize() : 0;
    size_t total = 4 + 4 + (size_t)compSize + (size_t)ctrlSize;
    auto* blob = (unsigned char*)std::malloc(total);
    if (!blob)
        return -1;
    blob[0] = 'R';
    blob[1] = 'O';
    blob[2] = 'P';
    blob[3] = 'S';
    std::memcpy(blob + 4, &compSize, 4);
    std::memcpy(blob + 8, componentState->getData(), (size_t)compSize);
    if (ctrlSize > 0)
        std::memcpy(blob + 8 + compSize, controllerState->getData(), (size_t)ctrlSize);
    *data = blob;
    *size = (int)total;
    return 0;
}

VSTHOST_API int vst_chain_set_state(VstChainHandle handle, int index, const void* data, int size,
                                    char* err, int err_len)
{
    auto* chain = static_cast<VstChain*>(handle);
    if (!chain || !data || size < 8)
    {
        setErr(err, err_len, "插件状态数据无效");
        return -1;
    }
    std::lock_guard<std::mutex> lock(chain->mutex);
    if (index < 0 || index >= (int)chain->plugins.size())
    {
        setErr(err, err_len, "插件序号超出范围");
        return -1;
    }
    auto& p = *chain->plugins[(size_t)index];
    const unsigned char* blob = (const unsigned char*)data;
    if (!(blob[0] == 'R' && blob[1] == 'O' && blob[2] == 'P' && blob[3] == 'S'))
    {
        setErr(err, err_len, "插件状态格式不匹配");
        return -1;
    }
    int32 compSize = 0;
    std::memcpy(&compSize, blob + 4, 4);
    if (compSize < 0 || compSize > size - 8)
    {
        setErr(err, err_len, "插件状态长度损坏");
        return -1;
    }
    int32 ctrlSize = size - 8 - compSize;
    if (p.component)
    {
        auto stream = owned(new Steinberg::MemoryStream((void*)(blob + 8), compSize));
        if (p.component->setState(stream) != kResultOk)
        {
            setErr(err, err_len, "插件无法恢复组件状态");
            return -1;
        }
    }
    if (ctrlSize > 0 && p.controller)
    {
        auto stream = owned(new Steinberg::MemoryStream((void*)(blob + 8 + compSize), ctrlSize));
        if (p.controller->setState(stream) != kResultOk)
        {
            setErr(err, err_len, "插件无法恢复控制器状态");
            return -1;
        }
    }
    return 0;
}

VSTHOST_API void vst_free(void* data)
{
    std::free(data);
}

} // extern "C"
