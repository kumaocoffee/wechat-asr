"""Atomic desktop settings with a current-user Windows DPAPI protected key."""
import base64
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import uuid

from app_paths import runtime_dir
from bridge_config import validate_concurrency


def _crypt(data, *, protect):
    if sys.platform != 'win32':
        raise OSError('API Key 持久保存需要 Windows DPAPI。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p if protect else ctypes.c_void_p,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), 'WeChat ASR Bridge' if protect else None,
                    None, None, None, 1, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.data, ctypes.c_void_p))


def _target(runtime):
    return (Path(runtime) if runtime is not None else runtime_dir()) / 'desktop-settings.json'


def _read(runtime):
    try:
        data = json.loads(_target(runtime).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def saved_port(runtime=None):
    value = _read(runtime).get('port')
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 else 8765


def saved_api_key(runtime=None, default=''):
    value = _read(runtime).get('api_key_dpapi')
    if value is None:
        return default
    try:
        key = json.loads(_crypt(base64.b64decode(value, validate=True), protect=False).decode('utf-8'))
        if not isinstance(key, str):
            raise ValueError('Invalid protected key')
        return key
    except (OSError, ValueError, TypeError) as error:
        raise ValueError('已保存的 API Key 无法解密，请在连接设置中重新填写。') from error


def saved_concurrency(runtime=None):
    try:
        return validate_concurrency(_read(runtime).get('concurrency'))
    except ValueError:
        return validate_concurrency()


def saved_auth_enabled(runtime=None, default=False):
    data = _read(runtime)
    if isinstance(data.get('auth_enabled'), bool):
        return data['auth_enabled']
    # Existing installations enabled auth by saving a nonempty key. Preserve
    # that protection when migrating to the explicit switch.
    if 'api_key_dpapi' in data:
        return bool(saved_api_key(runtime))
    return default


def save_settings(port, api_key=None, runtime=None, *, concurrency=None, auth_enabled=None):
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError('端口必须介于 1 和 65535。')
    data = _read(runtime)
    if auth_enabled is not None:
        if not isinstance(auth_enabled, bool):
            raise ValueError('鉴权开关必须为布尔值。')
        if auth_enabled and not (api_key if api_key is not None else saved_api_key(runtime)).strip():
            raise ValueError('启用鉴权时需要填写 API Key。')
        data['auth_enabled'] = auth_enabled
    data['port'] = int(port)
    if concurrency is not None:
        data['concurrency'] = validate_concurrency(concurrency)
    if api_key is not None:
        if not isinstance(api_key, str):
            raise ValueError('API Key 必须为文本。')
        plain = json.dumps(api_key, ensure_ascii=False).encode('utf-8')
        data['api_key_dpapi'] = base64.b64encode(_crypt(plain, protect=True)).decode('ascii')
    target = _target(runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def save_port(port, runtime=None):
    save_settings(port, runtime=runtime)
