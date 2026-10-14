"""Windows tray callbacks are marshalled by the application to Tk's queue."""
import threading

from app_paths import resource_dir


class TrayIcon:
    def __init__(self, show, quit):
        import pystray
        from PIL import Image, ImageDraw
        icon_path = resource_dir() / 'assets' / 'app.ico'
        if icon_path.is_file():
            with Image.open(icon_path) as source:
                image = source.convert('RGBA')
        else:
            image = Image.new('RGBA', (64, 64))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill='#137D78')
            for x, y in ((18, 24), (29, 15), (40, 20)):
                draw.rectangle((x, y, x + 5, 49 - y + 15), fill='white')
        self.icon = pystray.Icon('WeChatASRBridge', image, '微信语音接口 · WeChat ASR Bridge',
                                pystray.Menu(pystray.MenuItem('显示窗口', lambda *_: show(), default=True),
                                             pystray.MenuItem('退出', lambda *_: quit())))
        self.thread = None

    def start(self, ready, failed):
        def setup(icon):
            try:
                icon.visible = True
                ready()
            except Exception as error:
                failed(str(error))
                icon.stop()

        def run():
            try:
                self.icon.run(setup=setup)
            except Exception as error:
                failed(str(error))

        self.thread = threading.Thread(target=run, name='WeChatASRBridge-tray', daemon=True)
        self.thread.start()

    def stop(self):
        if self.icon.visible:
            self.icon.visible = False
        self.icon.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('系统托盘仍在退出，请稍后重试。')
