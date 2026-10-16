"""Shared public model routes and validated per-provider admission settings."""
PROVIDERS = {
    'wechat': {'name': '微信', 'model': 'wechat-voice', 'default': 4},
    'wetype': {'name': '微信输入法', 'model': 'wetype-voice', 'default': 1},
    'doubao': {'name': '豆包语音', 'model': 'doubao-voice', 'default': 3},
}
BRIDGE_ID = 'asr-bridge'
DEFAULT_CONCURRENCY = {key: value['default'] for key, value in PROVIDERS.items()}


def validate_concurrency(value=None):
    if value is None:
        return DEFAULT_CONCURRENCY.copy()
    if not isinstance(value, dict) or set(value) != set(PROVIDERS):
        raise ValueError('需要分别设置微信、微信输入法和豆包的并发数。')
    if any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in value.values()):
        raise ValueError('并发数必须为正整数。')
    return value.copy()
