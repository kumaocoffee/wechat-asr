# WeChat ASR Bridge

An experimental Windows ASR bridge exposing the speech recognition used by Weixin's chat input as a programmable audio transcription API. [中文说明](README.md)

The actual backend is the user's logged-in Weixin client and Weixin's recognition service. Audio is sent through Weixin, **not to OpenAI**. `whisper-1` is a compatibility alias, not an actual Whisper model.

## Requirements

Windows x64, Miniconda/Anaconda, Python 3.11 x64, and a logged-in **Weixin 4.1.13.65** client with iLink and Opus enabled. The loaded DLL must match the SHA-256 documented in the Chinese README. Different builds are rejected before installing native hooks.

## Quick start

```powershell
git clone https://github.com/kuxiaowo/wechat-asr-bridge.git
cd wechat-asr-bridge
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
# Log in to Weixin manually.
.\env\python.exe .\doctor.py
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

In another terminal:

```powershell
.\env\python.exe .\example_openai.py
```

Use `http://127.0.0.1:8765/v1` as the Base URL, `wechat-local` as the default SDK key, and `wechat-voice` or `whisper-1` as the model. Swagger UI is available at `/docs`.

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="wechat-local", timeout=150)
with open("audio.mp3", "rb") as audio:
    print(client.audio.transcriptions.create(model="whisper-1", file=audio).text)
```

Optional authentication: set `WECHAT_API_KEY` before starting and use that same key in the client. The default listener is loopback only.

For background operation use `start.ps1 -Background`; use `stop.ps1` for graceful shutdown. Both support `-Port`. Runtime logs and control files are kept in the ignored `.runtime/` directory. `-WechatPid` allows selecting a specific main client process.

## Scope

- `POST /v1/audio/transcriptions`, `GET /v1/models`, and `GET /health`.
- `json` and `text` responses; WAV and MP3 integration-tested. FFmpeg is bundled through the Python dependency.
- No bridge-level total duration cap; maximum 25 MiB per upload. Synthetic 75.3-second WAV/MP3 inputs have been verified. Opus frames are submitted as numbered packets in one recognition session, with only the last packet marked final. Limits for longer inputs remain unverified.
- Up to **four concurrent native recognition sessions**; additional requests wait for a free slot. Each request owns its callbacks and cleanup. Use one Uvicorn worker so all requests share the same process-level agent.
- One persistent Frida agent per Weixin process, with a separate native voice session per request. Shutdown detaches hooks and unloads the agent. Uncertain cleanup pauses requests and retains callbacks until Weixin exits.
- No streaming, prompts, language controls, subtitles, timestamps, diarization, chat, TTS or Realtime API. Unsupported parameters are rejected explicitly.
- A health response does not prove live login or recognition readiness.
- Native instrumentation can affect Weixin stability; this build-specific bridge has not been validated for long-term operation.

Run offline tests with `env\python.exe -m unittest discover -s tests -p "test_*.py" -v`. Live integration uses `env\python.exe tests\verify_live.py` and sends only included synthetic fixtures to Weixin's service.

Run `env\python.exe tests\verify_long.py` to check long WAV/MP3 transcription, including a distinct spoken tail starting after 35 seconds, full decoded duration and native cleanup. See [long-audio findings](docs/LONG_AUDIO.md).

Run `env\python.exe tests\verify_stability.py --port 8765 --requests 20` to verify repeated calls reuse the same agent. Only one server instance may connect to Weixin. `tests\verify_shutdown.py --port 8765` verifies completion of an in-flight request during graceful shutdown and stops that background server.

MIT-licensed bridge code. No proprietary Weixin binaries, account keys, chat databases or personal recordings are distributed. Third-party components keep their own licenses. This project is not affiliated with Tencent, WeChat or OpenAI.
