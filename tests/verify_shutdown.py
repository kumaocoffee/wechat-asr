"""Live graceful-stop check. Stops a background instance started by start.ps1."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
import threading

import httpx
from openai import OpenAI


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = f'http://127.0.0.1:{args.port}'
    client = OpenAI(base_url=base + '/v1', api_key=os.getenv('WECHAT_API_KEY', 'wechat-local'), timeout=150, max_retries=0)
    barrier = threading.Barrier(2)
    def transcribe():
        with (root / 'tests' / 'fixtures' / 'test.wav').open('rb') as audio:
            barrier.wait(timeout=10)
            return client.audio.transcriptions.create(model='whisper-1', file=audio).text
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(transcribe) for _ in range(2)]
        for _ in range(100):
            if httpx.get(base + '/health', timeout=5).json()['active_requests'] == 2:
                break
            time.sleep(0.03)
        else:
            raise AssertionError('No in-flight request observed')
        stop = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(root / 'stop.ps1'), '-Port', str(args.port)],
            capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        texts = [request.result(timeout=150) for request in requests]
        assert stop.returncode == 0, stop.stderr.decode('utf-8', errors='replace')
        assert texts == ['你好，这是微信语音接口测试，今天阳光很好'] * 2
    try:
        connection = socket.create_connection(('127.0.0.1', args.port), timeout=1)
    except OSError:
        pass
    else:
        connection.close()
        raise AssertionError('Server still listening after stop.ps1')
    result = {'passed': True, 'observed_busy': True, 'request_completed': True,
              'texts': texts, 'concurrent_requests_completed': 2,
              'stop_exit_code': stop.returncode, 'server_stopped': True}
    runtime = root / '.runtime'
    runtime.mkdir(exist_ok=True)
    (runtime / 'shutdown-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
