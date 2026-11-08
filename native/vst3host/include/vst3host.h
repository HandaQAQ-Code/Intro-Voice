/* Minimal VST3 hosting API used by mixer_service.py through ctypes.
 * License of this file: MIT (part of the Record Out mixer).
 * The VST3 SDK used to build this DLL is MIT licensed (see third_party/vst3sdk/LICENSE.txt).
 */
#ifndef VST3HOST_H
#define VST3HOST_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifdef _WIN32
#define VSTHOST_API __declspec(dllexport)
#else
#define VSTHOST_API
#endif

typedef void* VstChainHandle;
/* json is a UTF-8 JSON object for one plugin class or one scan error. */
typedef void (*VstScanCallback)(const char* json, void* user);

/* --- scanning (call once per directory; errors are reported through the callback) --- */
VSTHOST_API int vst_scan_directory(const char* utf8_dir, VstScanCallback cb, void* user,
                                   char* err, int err_len);

/* --- chain lifetime --- */
VSTHOST_API VstChainHandle vst_chain_create(void);
VSTHOST_API void vst_chain_destroy(VstChainHandle chain);

/* --- chain structure (only call while audio is stopped) --- */
VSTHOST_API int vst_chain_add(VstChainHandle chain, const char* utf8_path, const char* class_id,
                              char* err, int err_len); /* returns index >= 0, -1 on failure */
VSTHOST_API int vst_chain_remove(VstChainHandle chain, int index, char* err, int err_len);
VSTHOST_API int vst_chain_move(VstChainHandle chain, int from, int to, char* err, int err_len);
VSTHOST_API int vst_chain_count(VstChainHandle chain);
VSTHOST_API int vst_chain_set_bypass(VstChainHandle chain, int index, int bypass);
VSTHOST_API int vst_chain_is_bypass(VstChainHandle chain, int index);
VSTHOST_API int vst_chain_info(VstChainHandle chain, int index, char* json, int json_len);

/* --- parameters --- */
VSTHOST_API int vst_chain_param_count(VstChainHandle chain, int index);
VSTHOST_API int vst_chain_param_info(VstChainHandle chain, int index, int param,
                                     char* json, int json_len);
VSTHOST_API int vst_chain_get_param(VstChainHandle chain, int index, int param);
VSTHOST_API int vst_chain_set_param(VstChainHandle chain, int index, int param, double normalized);

/* --- audio setup / processing (float32, stereo, planar) --- */
VSTHOST_API int vst_chain_setup(VstChainHandle chain, int sample_rate, int block_frames,
                                char* err, int err_len);
VSTHOST_API int vst_chain_start(VstChainHandle chain, char* err, int err_len);
VSTHOST_API void vst_chain_stop(VstChainHandle chain);
VSTHOST_API int vst_chain_process(VstChainHandle chain, const float* left_in, const float* right_in,
                                  float* left_out, float* right_out, int frames);
VSTHOST_API int vst_chain_latency(VstChainHandle chain);
VSTHOST_API const char* vst_chain_last_error(VstChainHandle chain);
VSTHOST_API int vst_chain_is_faulted(VstChainHandle chain);
VSTHOST_API void vst_chain_clear_fault(VstChainHandle chain);

/* --- plugin editor windows --- */
VSTHOST_API int64_t vst_chain_open_editor(VstChainHandle chain, int index, char* err, int err_len);
VSTHOST_API int vst_chain_close_editor(VstChainHandle chain, int index);
VSTHOST_API int vst_chain_editor_open(VstChainHandle chain, int index);

/* --- binary plugin state --- */
VSTHOST_API int vst_chain_get_state(VstChainHandle chain, int index, void** data, int* size);
VSTHOST_API int vst_chain_set_state(VstChainHandle chain, int index, const void* data, int size,
                                    char* err, int err_len);
VSTHOST_API void vst_free(void* data);

#ifdef __cplusplus
}
#endif

#endif /* VST3HOST_H */
