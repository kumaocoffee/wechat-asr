"""Run from the repository root: python -m experiments.doubao_asr audio.wav."""
import argparse
from dataclasses import asdict
import json
import sys

from .client import ASRError, transcribe


def main():
    # Configure before argparse, whose help/error paths can exit immediately.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='豆包网页匿名游客 ASR 实验，仅支持 PCM16 / 16 kHz / 单声道 WAV')
    parser.add_argument('audio', help='将发送给豆包语音服务的 WAV 文件')
    parser.add_argument('--json', action='store_true', help='输出文字、音频时长和总耗时')
    parser.add_argument('--timeout', type=float, default=15, help='初始化、连接及每个控制阶段的超时秒数')
    parser.add_argument('--verbose', action='store_true', help='在 stderr 输出事件与状态码，不输出设备标识')
    args = parser.parse_args()
    def report(event):
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        result = transcribe(args.audio, timeout=args.timeout,
                            on_event=report if args.verbose else None)
    except ASRError as error:
        print(f'识别失败：{error}', file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False) if args.json else result.text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
