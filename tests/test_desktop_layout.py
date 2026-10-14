"""Native Tk geometry regression checks for the clipped SDK/docs button row."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop
from desktop_service import ServiceController
from desktop_verify import inspect_layout


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows Tk layout')
class DesktopLayoutTests(unittest.TestCase):
    def test_controls_fit_at_minimum_size_with_selected_audio_and_scaled_fonts(self):
        import tkinter as tk
        for dpi in (96, 120, 144, 192):
            with self.subTest(dpi=dpi), tempfile.TemporaryDirectory() as directory:
                runtime=Path(directory)
                root=tk.Tk()
                root.tk.call('tk','scaling',dpi/72)
                try:
                    with patch.object(desktop,'runtime_dir',return_value=runtime), \
                            patch.object(desktop,'ServiceController',side_effect=lambda port:ServiceController(port,runtime=runtime)), \
                            patch.object(desktop,'health',return_value=None):
                        app=desktop.DesktopApplication(root)
                    # Includes a path, as in the user's screenshot; the extra
                    # filename lines must not consume the SDK/docs button row.
                    app._sample()
                    app.detail.set('已连接现有服务 · 4/4 路识别\n此窗口不会停止其他程序启动的服务。')
                    app._update_buttons()
                    root.update_idletasks()
                    width,height=root.minsize()
                    left=app.settings_button.master.master
                    link_row=next(child for child in left.winfo_children()
                                  if any(isinstance(button,app.ttk.Button) and button.cget('text')=='复制 SDK 地址'
                                         for button in child.winfo_children()))
                    for window_width,window_height in ((width,height),(max(width,1080),height+300)):
                        root.geometry(f'{window_width}x{window_height}')
                        root.update_idletasks()
                        inspect_layout(app)
                        gap=app.settings_button.winfo_rooty()-(link_row.winfo_rooty()+link_row.winfo_height())
                        self.assertGreaterEqual(gap,0)
                        self.assertLessEqual(gap,24,'The lower two button rows spread apart when the window grows')
                    app.closed=True
                finally:
                    for timer in root.tk.call('after','info'):
                        root.after_cancel(timer)
                    root.destroy()


if __name__=='__main__':unittest.main(verbosity=2)
