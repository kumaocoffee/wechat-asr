"""Live SDK integration, conversion and concurrent recognition verification."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import threading

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
assert {'wechat-voice'} == {m.id for m in models.data}
checks.append({'check': 'sdk_models', 'models': [m.id for m in models.data]})
with (FIXTURES / 'test.wav').open('rb') as audio:
    first = client.audio.transcriptions.create(model='wechat-voice', file=audio)
assert first.text == '你好，这是微信语音接口测试，今天阳光很好'
checks.append({'check': 'sdk_json', 'response': first.model_dump(exclude_none=True)})

mp3 = FIXTURES / 'test-second.mp3'
with mp3.open('rb') as audio:
    second = client.audio.transcriptions.create(model='wechat-voice', file=audio, response_format='text')
assert second == '现在进行第二次接口验证，苹果、香蕉、橘子'
checks.append({'check': 'sdk_mp3_text', 'response': second})

try:
    with (FIXTURES / 'test.wav').open('rb') as audio:
        client.audio.transcriptions.create(model='wechat-voice', file=audio, stream=True)
    raise AssertionError('stream must be rejected')
except APIStatusError as error:
    assert error.status_code == 400 and error.body['param'] == 'stream'
    checks.append({'check': 'sdk_error', 'status': error.status_code, 'body': error.body})

for concurrency in (2, 4, 8):
    barrier = threading.Barrier(concurrency)
    def call(index):
        name = 'test.wav' if index % 2 == 0 else 'test-second.wav'
        expected = first.text if index % 2 == 0 else second
        with (FIXTURES / name).open('rb') as audio:
            barrier.wait(timeout=10)
            started = time.monotonic()
            response = client.audio.transcriptions.with_raw_response.create(model='wechat-voice', file=audio)
            text = response.parse().text
            assert text == expected, (index, name, text)
            assert response.headers.get('x-wechat-session-cleaned') == 'true'
            return {'index': index, 'status': response.status_code, 'text': text,
                    'elapsed_seconds': round(time.monotonic() - started, 3)}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(call, index) for index in range(concurrency)]
        observations = []
        while not all(future.done() for future in futures):
            snapshot = httpx.get(HEALTH_URL, timeout=5).json()
            observations.append({'active': snapshot['active_requests'], 'queued': snapshot['queued_requests']})
            time.sleep(0.03)
        results = [future.result(timeout=150) for future in futures]
    peak_active = max((item['active'] for item in observations), default=0)
    peak_queued = max((item['queued'] for item in observations), default=0)
    assert peak_active == min(concurrency, 4), observations
    if concurrency > 4:
        assert peak_queued > 0, observations
    check = {'check': 'concurrent_recognition', 'concurrency': concurrency, 'peak_active': peak_active,
             'peak_queued': peak_queued, 'elapsed_seconds': round(time.monotonic() - started, 3), 'results': results}
    checks.append(check)
    print(json.dumps(check, ensure_ascii=False), flush=True)

health = httpx.get(HEALTH_URL).json()
assert not health['busy'] and health['status'] == 'ok'
checks.append({'check': 'post_request_health', 'response': health})
record = {'passed': True, 'base_url': URL, 'sdk': __import__('openai').__version__,
          'checks': checks}
args.out.parent.mkdir(parents=True, exist_ok=True)
args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(record, ensure_ascii=False, indent=2))
