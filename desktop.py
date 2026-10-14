"""Portable Windows desktop launcher; --serve runs its hidden API child."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from app_paths import resource_dir, runtime_dir
from desktop_service import ServiceController, VERSION, health, saved_port, save_port, transcribe_file


class DesktopApplication:
    def __init__(self, root, port=8765):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.runtime = runtime_dir()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.controller = ServiceController(port)
        self.events = queue.Queue()
        self.operation = None
        self.polling = False
        self.connected = False
        self.owned = False
        self.audio_path = None
        self.started = None
        self.port = tk.StringVar(value=str(port))
        self.api_key = tk.StringVar(value=os.getenv('WECHAT_API_KEY', ''))
        self.status = tk.StringVar(value='正在检查服务…')
        self.detail = tk.StringVar(value='请保持微信登录。')
        self.file_name = tk.StringVar(value='还没有选择音频')
        self.file_detail = tk.StringVar(value='选择 WAV、MP3 等音频，单文件最大 25 MiB。')
        self.result_status = tk.StringVar(value='选择音频后，点击开始识别。')
        self.endpoint = tk.StringVar(value=f'http://127.0.0.1:{port}')
        self._build()
        self.root.after(100, self._drain_events)
        self.root.after(150, self._poll)
        self.root.protocol('WM_DELETE_WINDOW', self._close)

    def _build(self):
        tk, ttk, root = self.tk, self.ttk, self.root
        self.colors = {'bg': '#EEF4F8', 'paper': '#FFFFFF', 'ink': '#18364A',
                       'muted': '#607585', 'accent': '#137D78', 'line': '#DAE5EC'}
        c = self.colors
        root.title('微信语音接口 · WeChat ASR Bridge')
        root.geometry('1000x720')
        root.minsize(930, 680)
        root.configure(bg=c['bg'])
        icon = resource_dir() / 'assets' / 'app.ico'
        if icon.exists():
            root.iconbitmap(str(icon))
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 9),
                        background='#F1F6F9', foreground=c['ink'], borderwidth=0)
        style.map('TButton', background=[('active', '#E3EDF3')], foreground=[('disabled', '#8B9BA7')])
        style.configure('Accent.TButton', background=c['accent'], foreground='white')
        style.map('Accent.TButton', background=[('active', '#106C67'), ('disabled', '#D4E3E8')],
                  foreground=[('disabled', '#728591')])
        style.configure('TEntry', padding=6, fieldbackground='white')
        style.configure('TSpinbox', padding=6, fieldbackground='white')
        style.configure('TProgressbar', background=c['accent'], troughcolor=c['bg'], borderwidth=0)
        outer = tk.Frame(root, bg=c['bg'])
        outer.pack(fill='both', expand=True, padx=28, pady=24)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        header = tk.Frame(outer, bg=c['bg'])
        header.grid(row=0, column=0, sticky='ew', pady=(0, 20))
        mark = tk.Canvas(header, width=44, height=44, bg=c['bg'], highlightthickness=0)
        mark.pack(side='left', padx=(0, 12))
        mark.create_rectangle(3, 3, 41, 36, fill=c['accent'], outline=c['accent'])
        mark.create_polygon(12, 35, 12, 43, 23, 35, fill=c['accent'], outline=c['accent'])
        for x, h in ((13, 12), (21, 22), (29, 16)):
            mark.create_rectangle(x, 20-h/2, x+3, 20+h/2, fill='white', outline='white')
        titles = tk.Frame(header, bg=c['bg'])
        titles.pack(side='left')
        tk.Label(titles, text='微信语音接口', font=('Microsoft YaHei UI', 21, 'bold'),
                 bg=c['bg'], fg=c['ink']).pack(anchor='w')
        tk.Label(titles, text='音频转文字，也能供 Screenpipe 和其他应用调用。',
                 bg=c['bg'], fg=c['muted'], font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(4, 0))
        tk.Label(header, text=f'WeChat ASR Bridge\nv{VERSION} · Windows', justify='right',
                 font=('Segoe UI', 9), bg=c['bg'], fg=c['muted']).pack(side='right')
        body = tk.Frame(outer, bg=c['bg'])
        body.grid(row=1, column=0, sticky='nsew')
        body.columnconfigure(0, minsize=310)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=c['paper'], padx=22, pady=18)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 18))
        right = tk.Frame(body, bg=c['paper'], padx=24, pady=22)
        right.grid(row=0, column=1, sticky='nsew')
        self._label(left, '本机服务', 14, bold=True).pack(anchor='w')
        self.state_label = self._label(left, '', 11, bold=True)
        self.state_label.configure(textvariable=self.status)
        self.state_label.pack(anchor='w', pady=(12, 4))
        self._label(left, '', 9, variable=self.detail, wrap=270).pack(anchor='w')
        slots = tk.Frame(left, bg=c['paper'])
        slots.pack(fill='x', pady=(12, 6))
        self.slots = []
        for _ in range(4):
            slot = tk.Label(slots, text='空闲', bg='#EAF2F6', fg=c['muted'],
                            font=('Microsoft YaHei UI', 9), padx=8, pady=8)
            slot.pack(side='left', expand=True, fill='x', padx=(0, 4))
            self.slots.append(slot)
        buttons = tk.Frame(left, bg=c['paper'])
        buttons.pack(fill='x', pady=(12, 12))
        self.start_button = ttk.Button(buttons, text='启动服务', command=self._start, style='Accent.TButton')
        self.start_button.pack(side='left', expand=True, fill='x', padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text='停止服务', command=self._stop)
        self.stop_button.pack(side='left', expand=True, fill='x')
        tk.Frame(left, bg=c['line'], height=1).pack(fill='x', pady=(0, 12))
        self._label(left, '接口地址', 10, bold=True).pack(anchor='w')
        self._label(left, '', 10, variable=self.endpoint, mono=True).pack(anchor='w', pady=(8, 10))
        links = tk.Frame(left, bg=c['paper'])
        links.pack(fill='x')
        ttk.Button(links, text='复制 SDK 地址', command=self._copy_endpoint).pack(side='left', padx=(0, 8))
        ttk.Button(links, text='接口文档', command=self._open_docs).pack(side='left')
        self.port_box = None
        self.settings_window = None
        bottom = tk.Frame(left, bg=c['paper'])
        bottom.pack(side='bottom', before=left.winfo_children()[0], fill='x', pady=(12, 0))
        self.settings_button = ttk.Button(bottom, text='连接设置', command=self._settings)
        self.settings_button.pack(side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(bottom, text='运行日志', command=self._open_logs).pack(side='left', fill='x', expand=True)
        self._label(right, '音频识别', 14, bold=True).pack(anchor='w')
        file_area = tk.Frame(right, bg='#F1F6F9', padx=16, pady=16)
        file_area.pack(fill='x', pady=(16, 14))
        self._label(file_area, '', 10, bold=True, variable=self.file_name, bg='#F1F6F9', wrap=450).pack(anchor='w')
        self._label(file_area, '', 9, variable=self.file_detail, bg='#F1F6F9', wrap=450).pack(anchor='w', pady=(6, 0))
        file_buttons = tk.Frame(right, bg=c['paper'])
        file_buttons.pack(fill='x')
        self.choose_button = ttk.Button(file_buttons, text='选择音频', command=self._choose_file)
        self.choose_button.pack(side='left', padx=(0, 8))
        self.sample_button = ttk.Button(file_buttons, text='使用示例音频', command=self._sample)
        self.sample_button.pack(side='left')
        self.recognize_button = ttk.Button(file_buttons, text='开始识别', command=self._recognize, style='Accent.TButton')
        self.recognize_button.pack(side='right')
        progress_row = tk.Frame(right, bg=c['paper'])
        progress_row.pack(fill='x', pady=(18, 8))
        self._label(progress_row, '', 9, variable=self.result_status, wrap=490).pack(anchor='w')
        self.progress = ttk.Progressbar(right, mode='determinate', value=0)
        self.progress.pack(fill='x', pady=(0, 12))
        text_frame = tk.Frame(right, bg=c['line'], padx=1, pady=1)
        text_frame.pack(fill='both', expand=True)
        self.result = tk.Text(text_frame, wrap='word', state='disabled', bg='white', fg=c['ink'],
                              font=('Microsoft YaHei UI', 11), padx=14, pady=14,
                              relief='flat', borderwidth=0, height=8, width=30, undo=False)
        scrollbar = ttk.Scrollbar(text_frame, command=self.result.yview)
        scrollbar.pack(side='right', fill='y')
        self.result.configure(yscrollcommand=scrollbar.set)
        self.result.pack(fill='both', expand=True)
        actions = tk.Frame(right, bg=c['paper'])
        actions.pack(side='bottom', before=text_frame, fill='x', pady=(12, 0))
        ttk.Button(actions, text='复制文字', command=self._copy_result).pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='另存为 TXT', command=self._save_result).pack(side='left')
        footer = tk.Frame(outer, bg=c['bg'])
        footer.grid(row=2, column=0, sticky='ew', pady=(16, 0))
        self._label(footer, '需登录微信 4.1.13.65；音频通过微信发送到微信服务端。', 9, bg=c['bg']).pack(anchor='w')
        self._label(footer, '关闭窗口后，已启动的服务继续在后台运行。', 9, bg=c['bg']).pack(anchor='w', pady=(4, 0))
        self._update_buttons()

    def _label(self, parent, text, size, *, bold=False, variable=None, wrap=None, mono=False, bg=None):
        label = self.tk.Label(parent, text=text, textvariable=variable,
                              font=('Consolas' if mono else 'Microsoft YaHei UI', size, 'bold' if bold else 'normal'),
                              bg=bg or self.colors['paper'], fg=self.colors['ink'] if bold else self.colors['muted'],
                              justify='left', anchor='w')
        if wrap:
            label.configure(wraplength=wrap)
        return label

    def _work(self, action, success, failure=None):
        def run():
            try:
                result = action()
                self.events.put(lambda value=result: success(value))
            except Exception as error:
                self.events.put(lambda message=str(error): (failure or self._show_error)(message))
        threading.Thread(target=run, daemon=True).start()

    def _drain_events(self):
        try:
            while True:
                self.events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _apply_port(self, event=None):
        if self.operation:
            return False
        try:
            value = int(self.port.get())
            if value == self.controller.port:
                return True
            self.controller = ServiceController(value)
            save_port(value, self.runtime)
            self.connected = self.owned = False
            self.endpoint.set(f'http://127.0.0.1:{value}')
            self.status.set('正在检查服务…')
            self._update_buttons()
            return True
        except (ValueError, OSError) as error:
            self.port.set(str(self.controller.port))
            self._show_error(str(error))
            return False

    def _poll(self):
        if not self.polling and self.operation not in ('starting', 'stopping'):
            self.polling = True
            controller = self.controller
            def check():
                current = health(controller.port)
                return current, controller.owns_service() if current else False
            self._work(check, lambda result: self._show_health(controller, result), self._poll_error)
        self.root.after(2500, self._poll)

    def _poll_error(self, message):
        self.polling = False

    def _show_health(self, controller, result):
        self.polling = False
        if controller is not self.controller or self.operation in ('starting', 'stopping'):
            return
        current, self.owned = result
        self.connected = current is not None
        failed = False
        if current is None:
            self.status.set('服务未启动')
            self.detail.set('登录微信后，点击启动服务。')
            active = 0
        else:
            active = int(current.get('active_requests', int(current.get('busy', False))))
            queued = int(current.get('queued_requests', 0))
            failed = bool(current.get('last_error') or current.get('status') != 'ok')
            self.status.set('最近识别失败' if failed else '服务运行中')
            if failed:
                error = str(current.get('last_error') or '服务状态异常，请查看日志。')
                friendly = {'cleanup_failed': '会话清理未完成，请退出微信后重新登录。',
                            'recognition_timeout': '上次识别超时，可重试或查看日志。',
                            'recognition_failed': '微信识别未成功，可重试或查看日志。'}
                message = friendly.get(error, error)
                self.detail.set(message if len(message) < 100 else '上次识别未成功，请确认微信已登录，或查看日志。')
            elif self.owned:
                self.detail.set(f'{active}/4 路识别 · {queued} 个请求排队')
            else:
                self.detail.set(f'已连接现有服务 · {active}/4 路识别\n此窗口不会停止其他程序启动的服务。')
        self.state_label.configure(fg='#A03F39' if failed else self.colors['accent'] if self.connected else self.colors['ink'])
        for index, slot in enumerate(self.slots):
            occupied = index < active
            slot.configure(text='识别中' if occupied else '空闲',
                           bg=self.colors['accent'] if occupied else '#EAF2F6',
                           fg='white' if occupied else self.colors['muted'])
        self._update_buttons()

    def _update_buttons(self):
        idle = self.operation is None
        for button, enabled in ((self.start_button, idle and not self.connected),
                                 (self.stop_button, idle and self.connected and self.owned),
                                 (self.recognize_button, idle and self.connected and self.audio_path is not None),
                                 (self.settings_button, idle),
                                 (self.choose_button, idle), (self.sample_button, idle)):
            button.configure(state='normal' if enabled else 'disabled')
        if self.port_box is not None and self.port_box.winfo_exists():
            self.port_box.configure(state='normal' if idle else 'disabled')

    def _settings(self):
        tk, ttk = self.tk, self.ttk
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title('连接设置')
        window.transient(self.root)
        window.resizable(False, False)
        panel = ttk.Frame(window, padding=24)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='本机接口端口').pack(anchor='w')
        self.port_box = ttk.Spinbox(panel, from_=1024, to=65535, textvariable=self.port, width=28)
        self.port_box.pack(fill='x', pady=(8, 16))
        ttk.Label(panel, text='API Key（现有服务启用鉴权时填写）').pack(anchor='w')
        ttk.Entry(panel, textvariable=self.api_key, show='•', width=28).pack(fill='x', pady=(8, 16))
        ttk.Label(panel, text='密钥仅用于本次连接，不会保存到文件。').pack(anchor='w', pady=(0, 16))
        def apply():
            if self._apply_port():
                window.destroy()
        ttk.Button(panel, text='应用设置', style='Accent.TButton', command=apply).pack(fill='x')

    def _start(self):
        if not self._apply_port():
            return
        self.operation = 'starting'
        self.status.set('正在启动服务…')
        self.detail.set('首次启动可能需要几秒钟。')
        self._update_buttons()
        controller = self.controller
        def done(result):
            self.operation = None
            self._show_health(controller, result)
        self._work(lambda: (controller.start(), controller.owns_service()), done, self._operation_error)

    def _stop(self):
        self.operation = 'stopping'
        self.status.set('正在停止服务…')
        self.detail.set('等待已提交的识别与会话清理完成。')
        self._update_buttons()
        def done(_):
            self.operation = None
            self._show_health(self.controller, (None, False))
        self._work(self.controller.stop, done, self._operation_error)

    def _operation_error(self, message):
        self.operation = None
        self.status.set('操作未完成')
        self.detail.set(message)
        self._update_buttons()
        self._show_error(message)

    def _choose_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title='选择要识别的音频', filetypes=[
            ('音频文件', '*.wav *.mp3 *.flac *.ogg *.m4a *.webm *.aac'), ('所有文件', '*.*')])
        if path:
            self._set_file(Path(path))

    def _set_file(self, path):
        if not path.is_file():
            self._show_error('音频文件不存在。')
            return
        self.audio_path = path
        self.file_name.set(path.name)
        self.file_detail.set(f'{path.stat().st_size / 1024:.1f} KiB · {path}')
        self.result_status.set('音频已选好，点击开始识别。')
        self._update_buttons()

    def _sample(self):
        path = resource_dir() / 'assets' / 'test.wav'
        if not path.exists():
            path = resource_dir() / 'tests' / 'fixtures' / 'test.wav'
        self._set_file(path)

    def _recognize(self):
        port, api_key, path = self.controller.port, self.api_key.get(), self.audio_path
        if path is None:
            return
        self.operation = 'recognizing'
        self.started = time.monotonic()
        self.result_status.set('正在识别，音频通过微信发送…')
        self.progress.configure(mode='indeterminate')
        self.progress.start(14)
        self._update_buttons()
        def done(text):
            self.operation = None
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)
            self.result.configure(state='normal')
            self.result.delete('1.0', 'end')
            self.result.insert('1.0', text)
            self.result.configure(state='disabled')
            self.result_status.set(f'识别完成 · {time.monotonic() - self.started:.1f} 秒')
            self._update_buttons()
        def failed(message):
            self.operation = None
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)
            self.result_status.set('识别失败：' + message)
            self._update_buttons()
        self._work(lambda: transcribe_file(path, port, api_key), done, failed)

    def _copy(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _copy_endpoint(self):
        self._copy(self.endpoint.get() + '/v1')

    def _copy_result(self):
        self._copy(self.result.get('1.0', 'end-1c'))

    def _save_result(self):
        from tkinter import filedialog
        text = self.result.get('1.0', 'end-1c')
        if not text:
            return
        path = filedialog.asksaveasfilename(title='保存识别文字', defaultextension='.txt',
                                          filetypes=[('文本文件', '*.txt')])
        if path:
            try:
                Path(path).write_text(text, encoding='utf-8')
                self.result_status.set('文字已保存。')
            except OSError as error:
                self._show_error(str(error))

    def _open_docs(self):
        webbrowser.open(self.endpoint.get() + '/docs')

    def _open_logs(self):
        os.startfile(str(self.runtime))

    def _show_error(self, message):
        from tkinter import messagebox
        messagebox.showerror('微信语音接口', message, parent=self.root)

    def _close(self):
        if self.operation == 'recognizing':
            from tkinter import messagebox
            if not messagebox.askyesno('关闭窗口', '识别正在进行，关闭后无法在此窗口查看结果。继续关闭？', parent=self.root):
                return
        self.root.destroy()


def self_test(output):
    import tkinter as tk
    import frida
    import imageio_ffmpeg
    report = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
              'assets': (resource_dir() / 'agent.js').is_file(), 'frida': frida.__version__}
    root = tk.Tk()
    root.withdraw()
    report['tk'] = root.tk.call('info', 'patchlevel')
    app = DesktopApplication(root)
    app._sample()
    report['gui'] = app.start_button.winfo_exists() and app.result.winfo_exists()
    report['sample_audio'] = app.audio_path is not None and app.audio_path.is_file()
    root.destroy()
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], capture_output=True,
                            timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    report['ffmpeg'] = result.returncode == 0
    report['passed'] = report['assets'] and report['ffmpeg'] and report['gui'] and report['sample_audio']
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


def main():
    args = sys.argv[1:]
    if args and args[0] == '--serve':
        # Windowed frozen apps have no standard streams. Uvicorn requires real
        # streams even when the process is launched invisibly by the desktop.
        runtime = runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        port = args[args.index('--port') + 1] if '--port' in args else '8765'
        with (runtime / f'desktop-service-{int(port)}.log').open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            from server import main as serve
            serve(args[1:])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args(args)
    if options.self_test:
        if not options.output:
            parser.error('--self-test requires --output')
        return self_test(options.output)
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk
    root = tk.Tk()
    DesktopApplication(root, options.port if options.port is not None else saved_port())
    root.mainloop()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        runtime = runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / 'desktop-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        if '--self-test' in sys.argv and '--output' in sys.argv:
            Path(sys.argv[sys.argv.index('--output') + 1]).write_text(
                json.dumps({'passed': False, 'error': traceback.format_exc()}, ensure_ascii=False, indent=2), encoding='utf-8')
        if '--serve' not in sys.argv and '--self-test' not in sys.argv:
            import tkinter.messagebox
            try:
                tkinter.messagebox.showerror('应用启动失败', f'请查看错误日志：\n{runtime / "desktop-error.log"}')
            except Exception:
                pass
        raise SystemExit(1)
