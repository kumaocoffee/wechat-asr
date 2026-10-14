"""Opt-in repeated live calls with synthetic fixtures; never operates the UI."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import httpx
from openai import OpenAI


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--requests', type=int, default=100)
    args = parser.parse_args()
    if args.requests < 2:
        parser.error('--requests must be at least 2')
    root = Path(__file__).resolve().parents[1]
    base = f'http://127.0.0.1:{args.port}'
    started = time.monotonic()
    with OpenAI(base_url=base + '/v1', api_key=os.getenv('WECHAT_API_KEY', 'wechat-local'),
                timeout=150, max_retries=0) as client, httpx.Client(timeout=5) as probe:
        identity = None
        for i in range(args.requests):
            with (root / 'tests' / 'fixtures' / 'test.wav').open('rb') as audio:
                result = client.audio.transcriptions.create(model='whisper-1', file=audio)
            assert result.text == '你好，这是微信语音接口测试，今天阳光很好', f'Unexpected text at call {i + 1}'
            health = probe.get(base + '/health').json()
            assert health['agent_connected'] and not health['busy'], health
            current = (health['last_pid'], health['agent_generation'])
            if identity is None:
                identity = current
            assert current == identity, f'Agent was replaced: {identity} -> {current}'
            if (i + 1) % 10 == 0:
                print(f'{i + 1}/{args.requests} calls passed; pid={identity[0]} generation={identity[1]}', flush=True)
        record = {'passed': True, 'requests': args.requests, 'pid': identity[0],
                  'agent_generation': identity[1], 'elapsed_seconds': round(time.monotonic() - started, 2)}
        target = root / '.runtime' / 'stability-verification.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
