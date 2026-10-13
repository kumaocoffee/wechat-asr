"""Run with the OpenAI Python SDK; no OpenAI account or real OpenAI API key needed."""
import argparse
import os
import sys
from pathlib import Path
from openai import OpenAI

sys.stdout.reconfigure(encoding='utf-8')

parser = argparse.ArgumentParser()
parser.add_argument('file', type=Path, nargs='?', default=Path(__file__).parent / 'tests' / 'fixtures' / 'test.wav')
parser.add_argument('--base-url', default='http://127.0.0.1:8765/v1')
args = parser.parse_args()
client = OpenAI(base_url=args.base_url, api_key=os.getenv('WECHAT_API_KEY', 'wechat-local'), timeout=150, max_retries=0)
with args.file.open('rb') as audio:
    result = client.audio.transcriptions.create(model='whisper-1', file=audio)
print(result.text)
