"""Separate bundled read-only assets from per-user writable runtime files."""
import os
from pathlib import Path
import sys


def resource_dir():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def user_runtime_dir():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    return base / 'WeChatASRBridge' / 'runtime'


def runtime_dir():
    if getattr(sys, 'frozen', False):
        return user_runtime_dir()
    return Path(__file__).resolve().parent / '.runtime'
