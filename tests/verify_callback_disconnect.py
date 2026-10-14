"""Verify the production fallback code after host loss in our own child process.

Never attaches to or terminates Weixin. Run explicitly on Windows x64.
"""
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def emit(value):
    print(json.dumps(value), flush=True)


def target():
    get_module=ctypes.windll.kernel32.GetModuleHandleW
    get_module.argtypes=[ctypes.c_wchar_p]
    get_module.restype=ctypes.c_void_p
    emit({'ready': True})
    for line in sys.stdin:
        command = json.loads(line)
        if command['action'] == 'exit':
            return
        # These native no-ops must remain executable even after every hook and
        # the Frida DLL have been unloaded by an unexpected host termination.
        base = command['address']
        for _ in range(1000):
            for offset in (0, 16, 32):
                ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(base + offset)(None)
        emit({'passed': True, 'calls': 3000, 'frida_loaded': bool(get_module('frida-agent.dll'))})


def host(pid):
    import frida
    source = (ROOT / 'agent.js').read_text(encoding='utf-8-sig')
    start = source.index("const kernel32=Process.getModuleByName('kernel32.dll');")
    end = source.index('const callbackVtable=alloc(64);', start)
    # Execute exactly the allocation/protection/thunk bytes shipped by the
    # production agent, rather than an independently implemented substitute.
    code = source[start:end] + """
const hooks=[0,16].map(offset=>Interceptor.attach(callbackCode.add(offset),{onEnter(){}}));
rpc.exports.info=()=>({address:callbackCode.toString()});
rpc.exports.dispose=()=>{hooks.forEach(hook=>hook.detach());Interceptor.flush();return true;};
"""
    session = frida.attach(pid)
    script = session.create_script(code)
    script.load()
    emit(script.exports_sync.info())
    for line in sys.stdin:
        command = json.loads(line)
        if command['action'] == 'dispose':
            script.exports_sync.dispose()
            script.unload()
            session.detach()
            emit({'disposed': True})
            return


def child(*args):
    return subprocess.Popen([sys.executable, __file__, *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding='utf-8', creationflags=FLAGS)


def request(process, command):
    process.stdin.write(json.dumps(command) + '\n')
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f'Child stopped: {process.poll()}; {process.stderr.read()}')
    return json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', action='store_true')
    parser.add_argument('--host-pid', type=int)
    args = parser.parse_args()
    if args.target:
        return target()
    if args.host_pid:
        return host(args.host_pid)
    sys.stdout.reconfigure(encoding='utf-8')
    results = []
    for abrupt in (False, True):
        target_process = child('--target')
        host_process = None
        try:
            assert json.loads(target_process.stdout.readline())['ready']
            host_process = child('--host-pid', str(target_process.pid))
            info = json.loads(host_process.stdout.readline())
            address = int(info['address'], 16)
            assert request(target_process, {'action': 'call', 'address': address})['passed']
            if abrupt:
                # The terminated process is our disposable Frida host, never
                # the target child or any user application.
                host_process.terminate()
                host_process.wait(timeout=10)
                time.sleep(2)
            else:
                assert request(host_process, {'action': 'dispose'})['disposed']
                host_process.wait(timeout=10)
            result = request(target_process, {'action': 'call', 'address': address})
            assert result['passed']
            assert not result['frida_loaded'], 'Fallback was tested before the Frida DLL unloaded'
            assert target_process.poll() is None
            results.append({'mode': 'host_terminated' if abrupt else 'graceful_unload', **result})
        finally:
            if host_process is not None and host_process.poll() is None:
                host_process.terminate()
                host_process.wait(timeout=10)
            if target_process.poll() is None:
                target_process.stdin.write(json.dumps({'action': 'exit'}) + '\n')
                target_process.stdin.flush()
                target_process.wait(timeout=10)
            for process in (host_process, target_process):
                if process is not None:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
    report = {'passed': True, 'checks': results, 'wechat_touched': False}
    output = ROOT / '.runtime' / 'crash-analysis' / 'fix-callback-disconnect-verification.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    emit(report)


if __name__ == '__main__':
    main()
