"""Verify complete >35-second WAV/MP3 recognition using synthetic speech."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

import httpx
import imageio_ffmpeg
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name('fixtures')


def compact(text):
    return re.sub(r'[^\w]', '', text)


def synthetic_audio():
    """Place a distinct spoken tail after 37.68 seconds of included test speech."""
    parts = []
    for name in ('test.wav', 'test-second.wav'):
        with wave.open(str(FIXTURES / name), 'rb') as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
            parts.append(audio.readframes(audio.getnframes()))
    tail_start = len(parts[0]) * 6 / 32000
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(parts[0] * 6 + parts[1])
    return output.getvalue(), tail_start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765/v1')
    parser.add_argument('--audio', type=Path, help='Optional synthetic 16 kHz mono PCM16 WAV')
    parser.add_argument('--expect-tail', default='苹果香蕉橘子')
    parser.add_argument('--tail-start-seconds', type=float)
    parser.add_argument('--out', type=Path, default=ROOT / '.runtime' / 'long-verification.json')
    args = parser.parse_args()
    if args.audio:
        data = args.audio.read_bytes()
        if args.tail_start_seconds is None:
            parser.error('--audio requires --tail-start-seconds for the distinct spoken tail')
        tail_start = args.tail_start_seconds
    else:
        data, tail_start = synthetic_audio()
    with wave.open(io.BytesIO(data), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        duration = audio.getnframes() / 16000
    assert duration > tail_start > 35, (duration, tail_start)
    assert compact(args.expect_tail), 'A nonempty spoken tail is required'
    mp3 = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
         '-i', 'pipe:0', '-f', 'mp3', 'pipe:1'],
        input=data, capture_output=True, check=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    url = args.base_url.rstrip('/')
    client = OpenAI(base_url=url, api_key=os.getenv('WECHAT_API_KEY', 'wechat-local'),
                    timeout=150, max_retries=0)
    record = {'passed': False, 'source_seconds': duration, 'tail_start_seconds': tail_start,
              'expected_tail': args.expect_tail, 'checks': []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        for suffix, content in (('wav', data), ('mp3', mp3)):
            started = time.monotonic()
            response = client.audio.transcriptions.with_raw_response.create(
                model='wechat-voice', file=('synthetic-long.' + suffix, content),
            )
            text = response.parse().text
            check = {'format': suffix, 'status': response.status_code, 'text': text,
                     'seconds': float(response.headers['x-wechat-audio-seconds']),
                     'cleaned': response.headers.get('x-wechat-session-cleaned') == 'true',
                     'tail_present': compact(args.expect_tail) in compact(text),
                     'elapsed_seconds': round(time.monotonic() - started, 3)}
            record['checks'].append(check)
            print(json.dumps(check, ensure_ascii=False), flush=True)
            assert check['cleaned'] and check['tail_present'], check
            assert abs(check['seconds'] - duration) < 0.2, check
        health = httpx.get(url.removesuffix('/v1') + '/health', timeout=5).json()
        assert health['status'] == 'ok' and not health['busy'], health
        assert health['max_audio_seconds'] is None, health
        record['passed'] = True
    finally:
        args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Long WAV/MP3 verification passed', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
