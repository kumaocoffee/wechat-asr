"""Normal visitor initialization and one bounded ASR session per invocation."""
from dataclasses import dataclass
import http.cookiejar
import json
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import urlencode
import urllib.request
import wave

from .protocol import ProtocolError, decode_response, encode_request

HOMEPAGE = 'https://www.doubao.com/chat/'
GATEWAY = 'wss://frontier-audio-web-ws.doubao.com/api/v2/sami/voicegenie'
# Public application identifier in the web frontend, not an account API credential.
APPKEY = 'GOqQpfo1fO7slHv8'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class ASRError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transcription:
    text: str
    duration: float
    elapsed: float


def bootstrap_guest(timeout=15):
    """Use a fresh in-memory cookie jar, without consulting any browser profile."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request = urllib.request.Request(HOMEPAGE, headers={
        'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9'})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ASRError('豆包首页响应过大')
        html = data.decode('utf-8')
        marker = re.search(r'window\._ROUTER_DATA\s*=\s*', html)
        if not marker:
            raise ASRError('首页缺少游客初始化数据，网页结构可能已改变')
        router, _ = json.JSONDecoder().raw_decode(html[marker.end():])
        layout = router['loaderData']['chat_layout']['chat_layout']
        device_id = str(layout['launchCore']['web_id'])
        if not device_id.isascii() or not device_id.isdigit() or int(device_id) <= 0:
            raise ASRError('首页未返回有效的游客设备标识')
        version = str(layout['pc_version'])
        aid = str(layout['aid'])
    except (KeyError, TypeError, ValueError) as error:
        raise ASRError('无法解析游客初始化数据，网页结构可能已改变') from error
    except OSError as error:
        raise ASRError(f'访问豆包首页失败（{type(error).__name__}）') from error
    # The homepage cookies and ttwid are intentionally not forwarded or persisted.
    return {
        'api_app_key': APPKEY, 'namespace': 'VoiceGenie', 'version_code': '20800',
        'language': 'zh', 'device_platform': 'web', 'pkg_type': 'release_version',
        'pc_version': version, 'doubao_pc_version': version,
        'region': 'CN', 'sys_region': 'CN', 'samantha_web': '1',
        'use-olympus-account': '1', 'doubao_device_platform': 'web',
        'aid': aid, 'real_aid': aid, 'device_id': device_id, 'web_platform': 'browser',
    }


def load_wav(path):
    """Validate the entire file before creating a remote visitor or ASR task."""
    try:
        if Path(path).stat().st_size > MAX_AUDIO_BYTES:
            raise ASRError('WAV 文件超过 25 MiB')
        with wave.open(str(path), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(),
                    audio.getcomptype()) != (16000, 1, 2, 'NONE'):
                raise ASRError('需要单声道、16 kHz、PCM16 WAV')
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
        if not frames or len(pcm) != frames * 2:
            raise ASRError('WAV 音频为空或不完整')
        return pcm, frames / 16000
    except (OSError, EOFError, wave.Error) as error:
        raise ASRError('无法读取有效 WAV 文件') from error


def transcribe(path, *, timeout=15, on_event=None):
    """Send PCM at approximately real-time speed; return only a final result."""
    if not 0 < timeout <= 300:
        raise ASRError('超时必须大于 0 且不超过 300 秒')
    pcm, duration = load_wav(path)
    try:
        import websocket
    except ImportError as error:
        raise ASRError('请先安装 experiments/doubao_asr/requirements.txt') from error
    started = time.monotonic()
    context = bootstrap_guest(timeout)
    try:
        ws = websocket.create_connection(
            GATEWAY + '?' + urlencode(context), origin='https://www.doubao.com',
            timeout=timeout, header={'User-Agent': USER_AGENT})
    except (OSError, websocket.WebSocketException) as error:
        raise ASRError(f'语音连接失败（{type(error).__name__}）') from error
    incoming = queue.Queue()
    stopped = threading.Event()
    task_id = ''
    finals = []
    session_started = False
    session_finished = False

    def send(event, **kwargs):
        ws.send_binary(encode_request(event, APPKEY, task_id=task_id, **kwargs))

    def reader():
        while not stopped.is_set():
            try:
                raw = ws.recv()
                if not raw:
                    raise ASRError('语音连接在会话结束前关闭')
                incoming.put(decode_response(raw))
            except websocket.WebSocketTimeoutException:
                continue
            except (OSError, websocket.WebSocketException, ProtocolError, ASRError) as error:
                incoming.put(ASRError(f'读取语音响应失败（{type(error).__name__}）'))
                return

    def process(response):
        if isinstance(response, Exception):
            raise response
        event = response.get('event', '')
        code = response.get('status_code')
        if code not in (None, 20000000) or event in ('TaskFailed', 'SessionFailed'):
            detail = str(response.get('status_text', ''))[:200]
            raise ASRError(f'语音服务返回失败：{event} / {code} / {detail}')
        if on_event:
            on_event({'event': event, 'status_code': code})
        if event == 'ASRResponse':
            try:
                payload = response.get('payload', '{}')
                payload = json.loads(payload) if isinstance(payload, str) else payload
                results = payload.get('results', [])
                if not isinstance(results, list):
                    raise ValueError('results must be a list')
                for result in results:
                    if result.get('is_interim') is False:
                        text = result['text']
                        if not isinstance(text, str):
                            raise ValueError('text must be a string')
                        finals.append(text)
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                raise ASRError('语音识别响应格式无效') from error
        return event

    def wait_for(expected):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ASRError(f'等待 {expected} 超时')
            try:
                response = incoming.get(timeout=remaining)
            except queue.Empty as error:
                raise ASRError(f'等待 {expected} 超时') from error
            if process(response) == expected:
                return response

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        send('StartTask')
        response = wait_for('TaskStarted')
        task_id = response.get('task_id')
        if not isinstance(task_id, str) or not task_id:
            raise ASRError('创建语音任务后未收到 task_id')
        send('StartSession', payload={'business': 1, 'request_type': 3, 'asr': {
            'audio_info': {'channel': 1, 'format': 'pcm', 'sample_rate': 16000}}})
        wait_for('SessionStarted')
        session_started = True
        stream_start = time.monotonic()
        for sequence, offset in enumerate(range(0, len(pcm), 3200), 1):
            while True:
                try:
                    process(incoming.get_nowait())
                except queue.Empty:
                    break
            send('TaskRequest', audio=pcm[offset:offset + 3200], sequence=sequence)
            remaining = stream_start + min(offset + 3200, len(pcm)) / 32000 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        send('EndASR')
        wait_for('ASREnded')
        send('FinishSession')
        wait_for('SessionFinished')
        session_finished = True
        if not finals:
            raise ASRError('会话已结束，但没有最终识别结果')
        return Transcription(''.join(finals), duration, time.monotonic() - started)
    except (OSError, websocket.WebSocketException, ProtocolError) as error:
        raise ASRError(f'语音调用失败（{type(error).__name__}）') from error
    finally:
        if session_started and not session_finished:
            try:
                send('CancelSession')
            except (OSError, websocket.WebSocketException):
                pass
        stopped.set()
        try:
            ws.close(timeout=1)
        finally:
            thread.join(timeout=2)
