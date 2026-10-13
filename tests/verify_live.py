"""Live SDK integration, conversion and concurrent-admission verification."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx
import imageio_ffmpeg
from openai import OpenAI, APIStatusError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name('fixtures')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-url', default='http://127.0.0.1:8765/v1')
parser.add_argument('--out', type=Path, default=ROOT / '.runtime' / 'api-verification.json')
args = parser.parse_args()
URL = args.base_url.rstrip('/')
HEALTH_URL = URL.removesuffix('/v1') + '/health'
sys.stdout.reconfigure(encoding='utf-8')
import os
API_KEY = os.environ.get('WECHAT_API_KEY', 'wechat-local')
client = OpenAI(base_url=URL, api_key=API_KEY, timeout=150, max_retries=0)
checks = []
models = client.models.list()
assert {'wechat-voice', 'whisper-1'} == {m.id for m in models.data}
checks.append({'check': 'sdk_models', 'models': [m.id for m in models.data]})
with (FIXTURES / 'test.wav').open('rb') as audio:
    first = client.audio.transcriptions.create(model='whisper-1', file=audio)
assert first.text == '你好，这是微信语音接口测试，今天阳光很好'
checks.append({'check': 'sdk_json', 'response': first.model_dump(exclude_none=True)})

mp3 = FIXTURES / 'test-second.mp3'
with mp3.open('rb') as audio:
    second = client.audio.transcriptions.create(model='wechat-voice', file=audio, response_format='text')
assert second == '现在进行第二次接口验证，苹果、香蕉、橘子'
checks.append({'check': 'sdk_mp3_text', 'response': second})

try:
    with (FIXTURES / 'test.wav').open('rb') as audio:
        client.audio.transcriptions.create(model='whisper-1', file=audio, stream=True)
    raise AssertionError('stream must be rejected')
except APIStatusError as error:
    assert error.status_code == 400 and error.body['param'] == 'stream'
    checks.append({'check': 'sdk_error', 'status': error.status_code, 'body': error.body})

def call():
    with (FIXTURES / 'test.wav').open('rb') as audio:
        return client.audio.transcriptions.create(model='whisper-1', file=audio).text

with ThreadPoolExecutor(max_workers=1) as executor:
    running = executor.submit(call)
    for _ in range(100):
        if httpx.get(HEALTH_URL, timeout=5).json()['busy']:
            break
        time.sleep(0.03)
    else:
        raise AssertionError('Did not observe in-flight transcription')
    with (FIXTURES / 'test-second.wav').open('rb') as audio:
        refused = httpx.post(URL + '/audio/transcriptions', data={'model': 'whisper-1'},
                             files={'file': ('test.wav', audio, 'audio/wav')},
                             headers={'Authorization': 'Bearer ' + API_KEY}, timeout=10)
    assert refused.status_code == 429 and refused.json()['error']['code'] == 'server_busy'
    assert running.result(timeout=150) == first.text
    checks.append({'check': 'serial_native_calls', 'overlap_status': refused.status_code,
                   'completed_text': first.text})

health = httpx.get(HEALTH_URL).json()
assert not health['busy'] and health['status'] == 'ok'
checks.append({'check': 'post_request_health', 'response': health})
record = {'passed': True, 'base_url': URL, 'sdk': __import__('openai').__version__,
          'checks': checks}
args.out.parent.mkdir(parents=True, exist_ok=True)
args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(record, ensure_ascii=False, indent=2))
