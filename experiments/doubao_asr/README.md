# 豆包网页匿名 ASR 实验

从空 Cookie 会话正常访问豆包首页，取得服务器分配的游客设备标识，再通过网页语音 WebSocket 转写音频。无需读取浏览器资料、复制登录凭据或安装豆包客户端；音频会发送给豆包服务端。

这是非官方接口的实验模块。它提供 Python 调用和命令行，并已在 v0.3.0 接入 HTTP 服务、桌面窗口和打包流程，统一服务中使用 model=doubao-voice。当前流程等待最终文本，不承诺边说边出字。

## 安装和使用

在仓库根目录，用 Python 3.11 的 conda 环境运行：

```powershell
conda create -p .\env python=3.11 -y
conda activate .\env
python -m pip install -r .\experiments\doubao_asr\requirements.txt
python -m experiments.doubao_asr .\tests\fixtures\test.wav --json --verbose
```

已有项目 `env` 时，只需安装此目录的额外依赖，不需要重新创建环境。该模块不依赖 Frida 或已登录微信；完整项目的其他功能仍需要根目录依赖。

输入必须是 **16 kHz、单声道、PCM16 WAV**，文件最大 25 MiB。其他格式应先自行转换；当前模块不会截断音频。无效输入会在创建游客身份和提交音频之前拒绝。

默认只向 stdout 输出最终文字；`--json` 输出 `text`、`duration`（音频秒数）、`elapsed`（含游客初始化的总秒数）。`--verbose` 在 stderr 输出事件和状态码，不输出设备标识。`--timeout 15` 控制初始化、连接以及每个控制阶段的超时，不是整个音频的时长上限。失败以非零状态退出。

Python 调用：

```python
from experiments.doubao_asr.client import transcribe

result = transcribe("audio.wav")
print(result.text)
```

## 会话与数据

- 每次调用使用新建的内存 CookieJar，通过官方首页返回的 `launchCore.web_id` 取得游客设备标识；不猜测或借用设备身份。
- 网关地址为 `wss://frontier-audio-web-ws.doubao.com/api/v2/sami/voicegenie`。源码中的应用标识来自公开网页前端，不是账号 API Key。
- 首页返回的 Cookie、ttwid 不转发给语音 WebSocket；设备标识、音频和响应不会由模块自动写入文件。
- 正常流程为 `StartTask → StartSession → TaskRequest → EndASR → FinishSession`，只返回服务端明确标记的最终转写。
- 无最终结果、服务端拒绝、连接中断和阶段超时均报错。失败时尝试取消会话并关闭本地连接，不自动重发录音或循环初始化身份。异常退出不保证收到服务端取消确认。

## 验证

离线协议、输入验证、初始化和会话失败路径测试：

```powershell
python -m unittest discover -s tests -p "test_doubao_asr.py" -v
```

上述测试不会联网。真实合成音频的验证情况见 [VERIFICATION.md](VERIFICATION.md)。

目前未验证长音频、并发、断网恢复、设备标识有效期、长期运行或服务端限额。网页结构和私有协议可能变化；本实验不能作为长期稳定性保证。离线测试与一次真实转写分别说明代码路径和当时服务可用性。
