"""OpenAI-compatible local audio transcription server backed by logged-in Weixin."""
import argparse
import hmac
import os
import time
import uuid
import threading
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
import uvicorn

from backend import BackendError, MAX_BYTES, WechatBackend

MODELS = ('wechat-voice', 'whisper-1')
MODEL_CREATED = int(time.time())
app = FastAPI(title='WeChat OpenAI Audio API', version='1.0.0')
backend = WechatBackend()
gate = anyio.Semaphore(1)


def error_response(message, status=400, code='invalid_request', param=None):
    kind = 'server_error' if status >= 500 else ('authentication_error' if status == 401 else 'invalid_request_error')
    return JSONResponse({'error': {'message': message, 'type': kind, 'param': param, 'code': code}}, status_code=status)


@app.middleware('http')
async def metadata_auth(request, call_next):
    request.state.request_id = f'req_{uuid.uuid4().hex}'
    key = os.environ.get('WECHAT_API_KEY')
    if key and request.url.path.startswith('/v1/'):
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + key).encode()):
            response = error_response('API Key 不正确。', 401, 'invalid_api_key')
            response.headers['x-request-id'] = request.state.request_id
            return response
    response = await call_next(request)
    response.headers['x-request-id'] = request.state.request_id
    return response


@app.exception_handler(BackendError)
async def backend_error(request, error):
    return error_response(str(error), error.status, error.code, error.param)


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return error_response(str(error.detail), error.status_code, 'invalid_request')


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return error_response('请求参数不正确。', 400, 'invalid_request')


@app.exception_handler(Exception)
async def unexpected_error(request, error):
    return error_response('内部调用失败，请查看服务状态并确认微信仍在运行。', 500, 'internal_error')


def model_object(name):
    return {'id': name, 'object': 'model', 'created': MODEL_CREATED, 'owned_by': 'wechat-local'}


@app.get('/v1/models')
async def models():
    return {'object': 'list', 'data': [model_object(name) for name in MODELS]}


@app.get('/v1/models/{name}')
async def model(name: str):
    if name not in MODELS:
        return error_response('不支持的模型。', 404, 'model_not_found', 'model')
    return model_object(name)


@app.get('/health')
async def health():
    # Read-only; does not inject or submit an audio request.
    return {'status': 'ok' if backend.healthy else 'degraded', 'busy': gate.value == 0,
            'last_pid': backend.last_pid, 'last_success': backend.last_success,
            'backend': 'wechat-4.1.13.65', 'formats': ['json', 'text'],
            'max_audio_seconds': 15, 'last_error': backend.last_error,
            'note': '健康检查不等于当前微信登录状态验证'}


@app.post('/v1/audio/transcriptions', openapi_extra={
    'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
        'type': 'object', 'required': ['file', 'model'], 'properties': {
            'file': {'type': 'string', 'format': 'binary'},
            'model': {'type': 'string', 'enum': list(MODELS), 'default': 'whisper-1'},
            'response_format': {'type': 'string', 'enum': ['json', 'text'], 'default': 'json'},
            'temperature': {'type': 'number', 'enum': [0], 'default': 0},
            'stream': {'type': 'boolean', 'enum': [False], 'default': False},
        }}}}},
})
async def transcriptions(request: Request):
    if not request.headers.get('content-type', '').startswith('multipart/form-data'):
        return error_response('请使用 multipart/form-data 上传音频。')
    # Reject obviously oversized requests before multipart parsing.
    length = request.headers.get('content-length')
    if length:
        try:
            if int(length) > MAX_BYTES + 1024 * 1024:
                return error_response('上传文件过大。', 413, 'file_too_large', 'file')
        except ValueError:
            return error_response('Content-Length 不正确。')
    async with request.form(max_files=1, max_fields=20, max_part_size=MAX_BYTES) as form:
        audio = form.get('file')
        if not isinstance(audio, UploadFile):
            return error_response('缺少 file 音频文件。', param='file')
        model = form.get('model')
        if model not in MODELS:
            return error_response('model 必须为 wechat-voice 或 whisper-1（兼容别名）。', param='model')
        fmt = form.get('response_format', 'json')
        if fmt not in ('json', 'text'):
            return error_response('当前后端仅支持 json 和 text，无逐词时间戳或说话人分离。', param='response_format')
        allowed = {'file', 'model', 'response_format', 'stream', 'temperature'}
        for key in form:
            if key not in allowed:
                return error_response(f'微信后端尚不支持参数 {key}。', param=key)
        if str(form.get('stream', 'false')).lower() not in ('false', '0', ''):
            return error_response('当前后端不支持流式响应。', param='stream')
        if 'temperature' in form:
            try:
                if float(form['temperature']) != 0:
                    return error_response('微信后端不支持调整 temperature。', param='temperature')
            except (ValueError, TypeError):
                return error_response('temperature 不正确。', param='temperature')
        data = await audio.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return error_response('音频文件超过 25 MiB。', 413, 'file_too_large', 'file')
        if gate.value == 0:
            response = error_response('微信正在处理另一个音频请求，请稍后重试。', 429, 'server_busy')
            response.headers['retry-after'] = '3'
            return response
        async with gate:
            with anyio.CancelScope(shield=True):
                result = await anyio.to_thread.run_sync(backend.transcribe, data, abandon_on_cancel=False)
        headers = {'x-wechat-audio-seconds': str(result.duration), 'x-wechat-session-cleaned': 'true'}
        if fmt == 'text':
            return PlainTextResponse(result.text, headers=headers)
        return JSONResponse({'text': result.text}, headers=headers)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--pid', type=int, help='Optional fixed Weixin main process PID')
    parser.add_argument('--shutdown-file', type=Path, help='Local graceful shutdown signal file')
    args = parser.parse_args()
    if args.pid:
        os.environ['WECHAT_PID'] = str(args.pid)
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get('WECHAT_API_KEY'):
        parser.error('非本机监听地址需要先设置 WECHAT_API_KEY。')
    # One worker only: the native bridge must not run concurrently across processes.
    runtime = Path(__file__).parent / '.runtime'
    if args.shutdown_file and not args.shutdown_file.resolve().is_relative_to(runtime.resolve()):
        parser.error('--shutdown-file 必须位于项目 .runtime 目录内。')
    controller = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, workers=1, log_level='info'))
    finished = threading.Event()
    def watch_shutdown():
        while not finished.wait(0.25):
            if args.shutdown_file.exists():
                controller.should_exit = True
                return
    if args.shutdown_file:
        threading.Thread(target=watch_shutdown, daemon=True).start()
    try:
        controller.run()
    finally:
        finished.set()
