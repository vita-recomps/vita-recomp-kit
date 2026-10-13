// proccap: record what one process (and its children) plays, to a WAV file.
//
// Windows 10 2004+ process loopback (AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK): the audio
// is taken from the process's own streams, so other applications are not recorded. Used to
// measure the game's sound output in scripted runs (docs/02-baseline.md, audio baseline).
//
// usage: proccap <pid> <out.wav> [--quiet]
//   Records 48 kHz stereo, written as 16-bit PCM, until the process exits or <out.wav>.stop
//   exists. Gaps in which the process plays nothing are filled with silence, so the file's time
//   axis is wall-clock time.
//   --quiet: turn the process's audio sessions down to QUIET_LEVEL while recording, so nothing is
//   heard, and restore them on exit. The capture comes after the session volume (a muted session
//   records silence), so it is taken as 32-bit float and scaled back up by 1 / QUIET_LEVEL.
//
// build (MSVC developer shell): cl /nologo /O2 /EHsc /std:c++20 proccap.cpp ole32.lib mmdevapi.lib
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <audiopolicy.h>
#include <mmdeviceapi.h>
#include <mmreg.h>
#include <wrl/implements.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr DWORD RATE = 48000;
constexpr WORD CHANNELS = 2;
constexpr DWORD OUT_FRAME_BYTES = CHANNELS * 2; // 16-bit PCM out
constexpr float QUIET_LEVEL = 0.0001f;          // -80 dB

class Activation : public Microsoft::WRL::RuntimeClass<Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
                       Microsoft::WRL::FtmBase, IActivateAudioInterfaceCompletionHandler> {
public:
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HRESULT result = E_FAIL;
    ComPtr<IAudioClient> client;

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation *op) override {
        ComPtr<IUnknown> unknown;
        const HRESULT hr = op->GetActivateResult(&result, &unknown);
        if (FAILED(hr))
            result = hr;
        else if (SUCCEEDED(result))
            result = unknown.As(&client);
        SetEvent(done);
        return S_OK;
    }
};

void write_header(FILE *f, DWORD data_bytes) {
    const DWORD byte_rate = RATE * OUT_FRAME_BYTES;
    const WORD block = OUT_FRAME_BYTES, fmt = 1, bits = 16;
    const DWORD fmt_size = 16, riff_size = 36 + data_bytes;
    fseek(f, 0, SEEK_SET);
    fwrite("RIFF", 1, 4, f);
    fwrite(&riff_size, 4, 1, f);
    fwrite("WAVEfmt ", 1, 8, f);
    fwrite(&fmt_size, 4, 1, f);
    fwrite(&fmt, 2, 1, f);
    fwrite(&CHANNELS, 2, 1, f);
    fwrite(&RATE, 4, 1, f);
    fwrite(&byte_rate, 4, 1, f);
    fwrite(&block, 2, 1, f);
    fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f);
    fwrite(&data_bytes, 4, 1, f);
    fseek(f, 0, SEEK_END);
}

// Sets the volume of every audio session of the process on the default render device.
// Returns the previous level of the first one (1 if there is none).
float set_session_volume(DWORD pid, float level) {
    ComPtr<IMMDeviceEnumerator> devices;
    ComPtr<IMMDevice> device;
    ComPtr<IAudioSessionManager2> manager;
    ComPtr<IAudioSessionEnumerator> sessions;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&devices)))
        || FAILED(devices->GetDefaultAudioEndpoint(eRender, eConsole, &device))
        || FAILED(device->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, &manager))
        || FAILED(manager->GetSessionEnumerator(&sessions)))
        return 1;
    float previous = 1;
    bool first = true;
    int count = 0;
    sessions->GetCount(&count);
    for (int i = 0; i < count; i++) {
        ComPtr<IAudioSessionControl> control;
        ComPtr<IAudioSessionControl2> control2;
        ComPtr<ISimpleAudioVolume> volume;
        DWORD session_pid = 0;
        if (FAILED(sessions->GetSession(i, &control)) || FAILED(control.As(&control2))
            || FAILED(control2->GetProcessId(&session_pid)) || session_pid != pid || FAILED(control.As(&volume)))
            continue;
        if (first) {
            volume->GetMasterVolume(&previous);
            first = false;
        }
        volume->SetMasterVolume(level, nullptr);
        fprintf(stderr, "proccap: session %d of %lu at volume %g\n", i, pid, level);
    }
    return previous;
}

} // namespace

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: proccap <pid> <out.wav> [--quiet]\n");
        return 2;
    }
    const DWORD pid = strtoul(argv[1], nullptr, 10);
    const std::string out = argv[2];
    const bool quiet = argc > 3 && std::string(argv[3]) == "--quiet";
    const std::string stop_file = out + ".stop";
    DeleteFileA(stop_file.c_str());

    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!process) {
        fprintf(stderr, "proccap: no process %lu\n", pid);
        return 1;
    }

    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT activation{};
    activation.vt = VT_BLOB;
    activation.blob.cbSize = sizeof(params);
    activation.blob.pBlobData = reinterpret_cast<BYTE *>(&params);

    auto handler = Microsoft::WRL::Make<Activation>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> op;
    HRESULT hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &activation,
        handler.Get(), &op);
    if (FAILED(hr)) {
        fprintf(stderr, "proccap: ActivateAudioInterfaceAsync failed (0x%08lx)\n", hr);
        return 1;
    }
    WaitForSingleObject(handler->done, INFINITE);
    if (FAILED(handler->result)) {
        fprintf(stderr, "proccap: activation failed (0x%08lx)\n", handler->result);
        return 1;
    }
    IAudioClient *client = handler->client.Get();

    // capture as float, so a quiet session keeps its detail
    WAVEFORMATEX format{};
    format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    format.nChannels = CHANNELS;
    format.nSamplesPerSec = RATE;
    format.wBitsPerSample = 32;
    format.nBlockAlign = CHANNELS * 4;
    format.nAvgBytesPerSec = RATE * format.nBlockAlign;
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        2000000, 0, &format, nullptr);
    if (FAILED(hr)) {
        fprintf(stderr, "proccap: Initialize failed (0x%08lx)\n", hr);
        return 1;
    }
    HANDLE ready = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    client->SetEventHandle(ready);
    ComPtr<IAudioCaptureClient> capture;
    if (FAILED(client->GetService(IID_PPV_ARGS(&capture)))) {
        fprintf(stderr, "proccap: no capture client\n");
        return 1;
    }

    FILE *f = fopen(out.c_str(), "wb");
    if (!f) {
        fprintf(stderr, "proccap: cannot write %s\n", out.c_str());
        return 1;
    }
    write_header(f, 0);
    float restore = 1, gain = 1;
    if (quiet) {
        restore = set_session_volume(pid, QUIET_LEVEL);
        gain = 1 / QUIET_LEVEL;
    }

    LARGE_INTEGER freq, start, now;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&start);
    unsigned long long written = 0, clipped = 0; // frames, samples
    const std::vector<int16_t> zeros(RATE / 10 * CHANNELS);
    std::vector<int16_t> pcm;
    client->Start();
    fprintf(stderr, "proccap: recording %lu to %s\n", pid, out.c_str());

    for (;;) {
        HANDLE waits[2] = { ready, process };
        const DWORD w = WaitForMultipleObjects(2, waits, FALSE, 100);
        if (w == WAIT_OBJECT_0 + 1)
            break;
        if (GetFileAttributesA(stop_file.c_str()) != INVALID_FILE_ATTRIBUTES)
            break;
        UINT32 packet = 0;
        while (SUCCEEDED(capture->GetNextPacketSize(&packet)) && packet) {
            BYTE *data;
            UINT32 frames;
            DWORD flags;
            if (FAILED(capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr)))
                break;
            pcm.assign(static_cast<size_t>(frames) * CHANNELS, 0);
            if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                const float *in = reinterpret_cast<const float *>(data);
                for (size_t i = 0; i < pcm.size(); i++) {
                    const float v = std::round(in[i] * gain * 32767.0f);
                    if (std::fabs(v) > 32767.0f)
                        clipped++;
                    pcm[i] = static_cast<int16_t>(std::clamp(v, -32768.0f, 32767.0f));
                }
            }
            fwrite(pcm.data(), OUT_FRAME_BYTES, frames, f);
            written += frames;
            capture->ReleaseBuffer(frames);
        }
        // nothing played for a while: keep the time axis with silence
        QueryPerformanceCounter(&now);
        const unsigned long long expected = (now.QuadPart - start.QuadPart) * RATE / freq.QuadPart;
        while (expected > written + RATE / 5) {
            fwrite(zeros.data(), sizeof(int16_t), zeros.size(), f);
            written += zeros.size() / CHANNELS;
        }
    }

    client->Stop();
    if (quiet)
        set_session_volume(pid, restore);
    write_header(f, static_cast<DWORD>(written * OUT_FRAME_BYTES));
    fclose(f);
    DeleteFileA(stop_file.c_str());
    fprintf(stderr, "proccap: %.1f s written, %llu samples clipped\n", static_cast<double>(written) / RATE, clipped);
    return 0;
}
