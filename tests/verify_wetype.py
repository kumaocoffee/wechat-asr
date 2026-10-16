"""Live synthetic-audio SDK and lifecycle verification for the WeType backend."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sys
import time
import unicodedata
import wave

import httpx
from openai import OpenAI, DefaultHttpxClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import normalize_audio


def normalize_text(text):
    return ''.join(c for c in text if not unicodedata.category(c).startswith(('P', 'Z')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8766/v1')
    parser.add_argument('--out', type=Path, default=Path('.runtime/wetype-live-verification.json'))
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip('/')
    fixtures = Path(__file__).with_name('fixtures')
    client = OpenAI(base_url=args.base_url, api_key=os.environ.get('WECHAT_API_KEY', 'local'), timeout=180,
                    max_retries=0, http_client=DefaultHttpxClient(trust_env=False))
    health_url = args.base_url.removesuffix('/v1') + '/health'
    checks = []
    before = httpx.get(health_url, trust_env=False).json()
    assert before['backend'] == 'wetype-3.0.0.34-experimental', before
    assert before['max_concurrency'] == 4
    model_names = [m.id for m in client.models.list()]
    assert model_names == ['wetype-voice'], model_names

    def call(data, name, expected, response_format='json'):
        started = time.monotonic()
        raw = client.audio.transcriptions.with_raw_response.create(
            model='wetype-voice', file=(name, data), response_format=response_format)
        result = raw.parse()
        text = result if isinstance(result, str) else result.text
        assert normalize_text(text) == normalize_text(expected), text
        assert raw.headers['x-wechat-session-cleaned'] == 'true'
        assert raw.headers['x-asr-completion'] == 'server_end_flag'
        record = {'name': name, 'text': text, 'completion': raw.headers['x-asr-completion'],
                  'seconds': round(time.monotonic() - started, 3)}
        checks.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return text

    first = '你好，这是微信语音接口测试，今天阳光很好'
    second = '现在进行第二次接口验证，苹果、香蕉、橘子'
    first_bytes = (fixtures / 'test.wav').read_bytes()
    call(first_bytes, 'test.wav', first)
    call((fixtures / 'test-second.mp3').read_bytes(), 'test-second.mp3', second, 'text')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call, first_bytes, f'concurrent-{i}.wav', first) for i in range(2)]
        samples = []
        while not all(f.done() for f in futures):
            samples.append(httpx.get(health_url, trust_env=False).json())
            time.sleep(.05)
        for f in futures:
            f.result()
        assert max(s['active_requests'] for s in samples) == 1
        assert max(s['queued_requests'] for s in samples) >= 1

    # 7 synthetic phrases followed by a distinct tail, > 40 s total.
    pcm, _ = normalize_audio(first_bytes)
    tail, _ = normalize_audio((fixtures / 'test-second.wav').read_bytes())
    long_pcm = pcm * 7 + tail
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(long_pcm)
    call(output.getvalue(), 'synthetic-long.wav', first * 7 + second)
    after = httpx.get(health_url, trust_env=False).json()
    assert after['last_pid'] == before['last_pid'] or before['last_pid'] is None
    assert after['active_requests'] == 0 and after['queued_requests'] == 0
    assert after['agent_generation'] == 1 and after['status'] == 'ok'
    assert before['agent_generation'] == 0, 'Use a fresh test server for this verification.'
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'passed': True, 'long_audio_seconds': len(long_pcm) / 32000,
                                  'checks': checks, 'health': after}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS', str(args.out), flush=True)


if __name__ == '__main__':
    main()
