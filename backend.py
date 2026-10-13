"""Use the verified Weixin native bridge, one isolated Frida session per call."""
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import wave

import frida
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
EXPECTED_SHA256 = '8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74'
MAX_SECONDS = 15
MAX_BYTES = 25 * 1024 * 1024


class BackendError(Exception):
    def __init__(self, message, status=503, code='wechat_unavailable', param=None):
        super().__init__(message)
        self.status, self.code, self.param = status, code, param


@dataclass
class Transcription:
    text: str
    duration: float
    pid: int
    frames: int
    encoded_bytes: int


def discover_pid():
    override = os.environ.get('WECHAT_PID')
    if override:
        try:
            return int(override)
        except ValueError:
            raise BackendError('WECHAT_PID 必须为整数。')
    command = (
        '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; '
        '$taskCandidates=@(Get-Process -Name Weixin -ErrorAction SilentlyContinue | '
        'Where-Object { $_.MainWindowTitle -eq "微信" -and $_.Responding }); '
        'ConvertTo-Json -InputObject @($taskCandidates | ForEach-Object { $_.Id }) -Compress'
    )
    try:
        output = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        ids = json.loads(output.stdout.decode('utf-8-sig'))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackendError('无法确定微信主进程，可通过 WECHAT_PID 指定。')
    if len(ids) != 1:
        raise BackendError('请先登录微信并保留聊天主窗口；多个微信窗口时请指定 WECHAT_PID。')
    return ids[0]


def normalize_audio(data):
    """Decode without persisting uploaded audio; detect overlength rather than truncate."""
    if not data:
        raise BackendError('音频文件为空。', 400, 'invalid_audio', 'file')
    if len(data) > MAX_BYTES:
        raise BackendError('音频文件超过 25 MiB。', 413, 'file_too_large', 'file')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) == (1, 2, 16000, 'NONE'):
                duration = audio.getnframes() / 16000
                if duration <= 0 or duration > MAX_SECONDS:
                    raise BackendError('当前版本仅支持 0–15 秒音频。', 400, 'audio_too_long', 'file')
                pcm = audio.readframes(audio.getnframes())
                if len(pcm) != audio.getnframes() * 2:
                    raise BackendError('WAV 数据不完整。', 400, 'invalid_audio', 'file')
                return pcm + b'\0' * (-len(pcm) % 640), duration
    except (wave.Error, EOFError):
        pass
    try:
        output = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
             '-nostdin', '-protocol_whitelist', 'pipe', '-i', 'pipe:0', '-vn',
             '-t', '15.02', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'],
            input=data, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        raise BackendError('音频解码失败或超时。', 400, 'invalid_audio', 'file')
    if output.returncode or not output.stdout:
        raise BackendError('无法解码音频，请上传有效的 WAV、MP3、FLAC、OGG 或 WebM。', 400, 'invalid_audio', 'file')
    duration = len(output.stdout) / 32000
    if duration > MAX_SECONDS:
        raise BackendError('当前版本仅支持 0–15 秒音频，文件未被截断转写。', 400, 'audio_too_long', 'file')
    pcm = output.stdout
    return pcm + b'\0' * (-len(pcm) % 640), duration


class WechatBackend:
    def __init__(self):
        self.lock = threading.Lock()
        self.healthy = True
        self.last_pid = None
        self.last_error = None
        self.last_success = None
        self._fingerprint = None

    def verify_dll(self, path):
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if self._fingerprint == key:
                return
            if hashlib.sha256(path.read_bytes()).hexdigest() != EXPECTED_SHA256:
                raise BackendError('微信 DLL 指纹不匹配，仅支持已验证的 4.1.13.65。')
            self._fingerprint = key
        except OSError:
            raise BackendError('无法读取微信 DLL，请检查安装路径。')

    def transcribe(self, data):
        # Extra process-level protection if this method is used outside FastAPI.
        with self.lock:
            if not self.healthy:
                raise BackendError('上次会话清理未完成，暂停调用；请重新启动本 API 服务。', code='cleanup_failed')
            pcm, duration = normalize_audio(data)
            pid = discover_pid()
            session = script = None
            cleanup = {'cleaned': True}
            result = failure = None
            try:
                session = frida.attach(pid)
                metadata = session.create_script("rpc.exports.info = () => { const m=Process.getModuleByName('Weixin.dll'); return {pid:Process.id,path:m.path,base:m.base.toString(),size:m.size,arch:Process.arch}; };")
                metadata.load()
                try:
                    info = metadata.exports_sync.info()
                    self.verify_dll(Path(info['path']))
                finally:
                    metadata.unload()
                script = session.create_script((ROOT / 'agent.js').read_text(encoding='utf-8-sig'))
                script.load()
                # Verify the loaded module itself, not just a configured file on disk.
                self.last_pid = pid
                result = script.exports_sync.recognize(list(pcm), f'api-voice-{uuid.uuid4().hex}', 36, 5)
            except Exception as error:
                failure = error
            finally:
                if script:
                    try:
                        cleanup = script.exports_sync.finalize()
                    except Exception as error:
                        cleanup = {'cleaned': False, 'error': str(error)}
                if session:
                    session.detach()
            if not cleanup.get('cleaned'):
                self.healthy = False
                self.last_error = 'cleanup_failed'
                raise BackendError('微信会话清理失败，已暂停后续调用。', 503, 'cleanup_failed')
            if failure:
                self.last_error = str(failure)
                if isinstance(failure, BackendError):
                    raise failure
                raise BackendError('微信语音服务暂不可用，请确认已登录且 iLink/Opus 已启用。') from failure
            errors = [e for e in result['events'] if e['event'] in ('recognition_error', 'dispatch_error')]
            finals = [e for e in result['events'] if e['event'] == 'final_result']
            if errors:
                raise BackendError('微信返回识别错误。', 502, 'recognition_failed')
            if not result['done'] or not finals:
                raise BackendError('微信识别未在时限内完成。', 504, 'recognition_timeout')
            self.last_error = None
            self.last_success = time.time()
            return Transcription(finals[-1]['text'], duration, pid, result['frames'], result['bytes'])
