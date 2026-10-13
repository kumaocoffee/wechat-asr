"""Read-only environment diagnosis; never loads the native hook agent."""
import json
import platform
from pathlib import Path
import sys

from backend import BackendError, WechatBackend, discover_pid
import frida
import imageio_ffmpeg


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    checks = {'windows': sys.platform == 'win32', 'python_64bit': platform.architecture()[0] == '64bit',
              'python_version': platform.python_version(), 'ffmpeg_available': Path(imageio_ffmpeg.get_ffmpeg_exe()).exists()}
    if not checks['windows'] or not checks['python_64bit']:
        print(json.dumps({'ok': False, 'checks': checks, 'error': '需要 Windows x64 和 64 位 Python。'}, ensure_ascii=False, indent=2))
        return 1
    session = None
    try:
        pid = discover_pid()
        session = frida.attach(pid)
        script = session.create_script("rpc.exports.path = () => Process.getModuleByName('Weixin.dll').path;")
        script.load()
        WechatBackend().verify_dll(Path(script.exports_sync.path()))
        checks.update(wechat_process_found=True, dll_fingerprint_matches=True)
        print(json.dumps({'ok': True, 'checks': checks, 'note': '诊断通过不代表已经提交识别请求；请继续运行 example_openai.py。'}, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'checks': checks, 'error': str(error)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        if session:
            session.detach()


if __name__ == '__main__':
    raise SystemExit(main())
