"""Use one verified, persistent Frida agent per Weixin process."""
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import wave

import frida
import imageio_ffmpeg
from app_paths import resource_dir

ROOT = resource_dir()
EXPECTED_SHA256 = '8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74'
MAX_BYTES = 25 * 1024 * 1024
log = logging.getLogger(__name__)


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
        'Where-Object { $_.MainWindowHandle -ne 0 -and $_.Responding }); '
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
    """Decode the complete audio without persisting or truncating uploads."""
    if not data:
        raise BackendError('音频文件为空。', 400, 'invalid_audio', 'file')
    if len(data) > MAX_BYTES:
        raise BackendError('音频文件超过 25 MiB。', 413, 'file_too_large', 'file')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) == (1, 2, 16000, 'NONE'):
                duration = audio.getnframes() / 16000
                if duration <= 0:
                    raise BackendError('音频没有有效采样。', 400, 'invalid_audio', 'file')
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
             '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'],
            input=data, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        raise BackendError('音频解码失败或超时。', 400, 'invalid_audio', 'file')
    if output.returncode or not output.stdout:
        raise BackendError('无法解码音频，请上传有效的 WAV、MP3、FLAC、OGG 或 WebM。', 400, 'invalid_audio', 'file')
    duration = len(output.stdout) / 32000
    pcm = output.stdout
    return pcm + b'\0' * (-len(pcm) % 640), duration


class WechatBackend:
    def __init__(self):
        self.lock = threading.Condition(threading.RLock())
        self._active_requests = 0
        self._closing = False
        self.healthy = True
        self.last_pid = None
        self.last_error = None
        self.last_success = None
        self._fingerprint = None
        self._session = None
        self._script = None
        self._pid = None
        self._generation = 0

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

    def _ensure_agent(self, pid):
        if self._session is not None and not self._session.is_detached and self._pid == pid:
            return self._script
        self._close_agent()
        session = frida.attach(pid)
        script = None
        try:
            # Verify the actual loaded DLL before installing native hooks.
            metadata = session.create_script("rpc.exports.info = () => { const m=Process.getModuleByName('Weixin.dll'); return {pid:Process.id,path:m.path,base:m.base.toString(),size:m.size,arch:Process.arch}; };")
            metadata.load()
            try:
                self.verify_dll(Path(metadata.exports_sync.info()['path']))
            finally:
                metadata.unload()
            script = session.create_script((ROOT / 'agent.js').read_text(encoding='utf-8-sig'))
            script.load()
        except Exception:
            if script:
                script.unload()
            session.detach()
            raise
        self._session, self._script, self._pid = session, script, pid
        self._generation += 1
        self.last_pid = pid
        log.info('Native agent attached: pid=%s generation=%s', pid, self._generation)
        return script

    def _close_agent(self):
        session, script = self._session, self._script
        if session is None:
            return
        if not session.is_detached:
            # A native cleanup failure must never unload live JS callbacks.
            result = script.exports_sync.dispose()
            if not result.get('cleaned'):
                self.healthy = False
                self.last_error = 'cleanup_failed'
                raise BackendError('微信会话尚未安全清理，保留 agent 并暂停调用。', code='cleanup_failed')
            script.unload()
            session.detach()
        log.info('Native agent closed: pid=%s generation=%s', self._pid, self._generation)
        self._session = self._script = self._pid = None

    def close(self):
        with self.lock:
            self._closing = True
            self.lock.notify_all()
            while self._active_requests:
                self.lock.wait()
            self._close_agent()

    @property
    def agent_connected(self):
        return self._session is not None and not self._session.is_detached

    @property
    def agent_generation(self):
        return self._generation

    def transcribe(self, data):
        # Protect only agent ownership and replacement. RPC calls run outside
        # this lock so independent native sessions can remain in flight together.
        request_id = f'api-voice-{uuid.uuid4().hex}'
        script = session = None
        registered = False
        cleanup = {'cleaned': True}
        result = failure = None
        try:
            pcm, duration = normalize_audio(data)
            with self.lock:
                # A verified, attached process is stable even when Windows
                # chooses the picture viewer as its current "main" window.
                cached_pid = self._pid if self.agent_connected else None
            pid = discover_pid() if os.environ.get('WECHAT_PID') or cached_pid is None else cached_pid
            with self.lock:
                # A changed or detached target cannot replace the agent until
                # all callers of the previous agent have finished unwinding.
                while self._active_requests and (self._pid != pid or self._session.is_detached):
                    if self._closing:
                        raise BackendError('服务正在停止。', code='server_stopping')
                    self.lock.wait()
                if self._closing:
                    raise BackendError('服务正在停止。', code='server_stopping')
                if not self.healthy and self._session is not None and self._session.is_detached:
                    self._close_agent()
                    self.healthy = True
                if not self.healthy:
                    raise BackendError('上次会话清理未完成，暂停调用；请退出微信后重新登录。', code='cleanup_failed')
                script = self._ensure_agent(pid)
                session = self._session
                generation = self._generation
                self._active_requests += 1
                registered = True
            result = script.exports_sync.recognize(list(pcm), request_id, 36, 5)
        except Exception as error:
            failure = error
        finally:
            if registered:
                if not session.is_detached:
                    try:
                        # Never finalize another request's live native session.
                        cleanup = script.exports_sync.finalize(request_id)
                    except Exception as error:
                        cleanup = {'cleaned': session.is_detached, 'error': str(error)}
                with self.lock:
                    if not cleanup.get('cleaned'):
                        self.healthy = False
                        self.last_error = 'cleanup_failed'
                    self._active_requests -= 1
                    if self._active_requests == 0 and session.is_detached:
                        # A terminated target owns no live native allocations.
                        self._close_agent()
                        self.healthy = True
                    self.lock.notify_all()
        if not cleanup.get('cleaned'):
            raise BackendError('微信会话清理失败，已暂停后续调用。', 503, 'cleanup_failed')
        if failure:
            with self.lock:
                if self.healthy:
                    self.last_error = str(failure)
            if isinstance(failure, BackendError):
                raise failure
            raise BackendError('微信语音服务暂不可用，请确认已登录且 iLink/Opus 已启用。') from failure
        errors = [e for e in result['events'] if e['event'] in ('recognition_error', 'dispatch_error')]
        finals = [e for e in result['events'] if e['event'] == 'final_result']
        if errors or not result['done'] or not finals:
            code = 'recognition_failed' if errors else 'recognition_timeout'
            with self.lock:
                if self.healthy:
                    self.last_error = code
            if errors:
                raise BackendError('微信返回识别错误。', 502, code)
            raise BackendError('微信识别未在时限内完成。', 504, code)
        with self.lock:
            if self.healthy:
                self.last_error = None
            self.last_success = time.time()
        log.info('Native recognition completed: pid=%s generation=%s seconds=%.3f', pid, generation, duration)
        return Transcription(finals[-1]['text'], duration, pid, result['frames'], result['bytes'])
