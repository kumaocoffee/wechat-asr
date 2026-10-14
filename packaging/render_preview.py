"""Render the actual Tk application into a PNG; no desktop automation."""
import argparse
import ctypes
from pathlib import Path
import sys
import tkinter as tk

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import DesktopApplication


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state', choices=['ready', 'result', 'stopped', 'error'], default='result')
    parser.add_argument('--width', type=int, default=1000)
    parser.add_argument('--height', type=int, default=720)
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    app = DesktopApplication(root)
    for timer in root.tk.call('after', 'info'):
        root.after_cancel(timer)
    root.geometry(f'{args.width}x{args.height}+0+0')
    # Populate deterministic preview data through the app's real view methods.
    # No native recognition, file upload, OS input, or unrelated window capture.
    state = None if args.state == 'stopped' else {'status': 'ok', 'active_requests': 0, 'queued_requests': 0}
    if args.state == 'error':
        state['last_error'] = '请先登录微信并保留聊天主窗口。'
    app._show_health(app.controller, (state, state is not None))
    if args.state in ('result', 'error'):
        app._sample()
        app.file_detail.set('6.3 秒 · 随应用提供的合成示例音频')
    if args.state == 'result':
        app.result.configure(state='normal')
        app.result.insert('1.0', '你好，这是微信语音接口测试，今天阳光很好')
        app.result.configure(state='disabled')
        app.result_status.set('识别完成 · 示例结果')
    root.update_idletasks()
    root.update()
    root.update_idletasks()
    # Pillow's window capture calls PrintWindow for this application's own
    # window. It neither reads the desktop nor sends mouse/keyboard input.
    frame = ImageGrab.grab(window=root.winfo_id())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.save(args.output)
    print(f'Rendered {args.state}: {args.output} ({frame.width}x{frame.height})')
    root.destroy()


if __name__ == '__main__':
    main()
