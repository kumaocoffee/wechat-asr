"""Opt-in live verification of a frozen Windows app; synthetic audio only."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import user_runtime_dir
from desktop_service import ServiceController, health, transcribe_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--out', type=Path, default=ROOT / '.runtime' / 'portable-native-verification.json')
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    executable = args.exe.resolve()
    report = {'passed': False, 'checks': []}
    smoke = args.out.with_name('portable-runtime-smoke.json')
    run = subprocess.run([str(executable), '--self-test', '--output', str(smoke.resolve())], timeout=30)
    runtime = json.loads(smoke.read_text(encoding='utf-8'))
    assert run.returncode == 0 and runtime['passed'] and runtime['frozen'], runtime
    report['runtime'] = runtime
    controller = ServiceController(args.port, runtime=user_runtime_dir(), executable=executable)
    if health(args.port):
        raise RuntimeError('The test port already has a service; choose an unused port.')
    fixtures = ROOT / 'tests' / 'fixtures'
    texts = ['你好，这是微信语音接口测试，今天阳光很好', '现在进行第二次接口验证，苹果、香蕉、橘子']
    api_key = os.getenv('WECHAT_API_KEY', 'wechat-local')
    stopped = False
    try:
        controller.start()
        assert controller.owns_service()
        first = transcribe_file(fixtures / 'test.wav', args.port, api_key)
        assert first == texts[0], first
        report['checks'].append({'check': 'frozen_wav', 'text': first})
        barrier = threading.Barrier(4)
        def call(index):
            name = 'test.wav' if index % 2 == 0 else 'test-second.mp3'
            barrier.wait(timeout=10)
            result = transcribe_file(fixtures / name, args.port, api_key)
            assert result == texts[index % 2], (index, result)
            return {'fixture': name, 'text': result}
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(call, range(4)))
        report['checks'].append({'check': 'frozen_four_concurrent_wav_mp3', 'results': results})
        after = health(args.port)
        assert after['status'] == 'ok' and after['last_error'] is None
        assert after['agent_connected'] and after['agent_generation'] == 1
        report['checks'].append({'check': 'agent_reused', 'generation': after['agent_generation']})
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(call, index) for index in range(2)]
            for _ in range(100):
                if health(args.port)['active_requests'] == 2:
                    break
                time.sleep(0.03)
            else:
                raise AssertionError('Did not observe two in-flight packaged requests')
            controller.stop()
            stopped = True
            results = [request.result(timeout=180) for request in requests]
        assert health(args.port) is None
        report['checks'].append({'check': 'graceful_stop_with_two_requests', 'results': results})
        report['passed'] = True
    finally:
        if not stopped and controller.owns_service():
            controller.stop()
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
