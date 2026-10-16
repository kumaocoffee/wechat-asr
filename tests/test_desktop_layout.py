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
                    app._sample()
                    providers=('wechat','wetype','doubao')
                    state={'status':'ok','active_requests':12,'queued_requests':1,
                           'pools':[{'provider':key,'max_concurrency':4,'occupied_slots':list(range(4))} for key in providers]}
                    app._show_health(app.controller,(state,True))
                    self.assertIn('运行 12',app.pool_summary.get())
                    self.assertNotIn('/',app.pool_summary.get())
                    for view in app.pool_views.values():
                        canvas=view['canvas']
                        self.assertEqual(sum(canvas.itemcget(item,'fill')=='#22954E' for item in canvas.find_all()),4)
                    app.concurrency['wechat']=108
                    app._show_health(app.controller, (None, False))
                    self.assertEqual(len(app.pool_views['wechat']['canvas'].find_all()),108)
                    self.assertEqual([app.notebook.tab(tab,'text') for tab in app.notebook.tabs()],['首页','手动转写'])
                    with patch.object(desktop.webbrowser,'open') as browser:
                        app._open_docs()
                        self.assertTrue(browser.call_args.args[0].startswith('file:///'))
                        self.assertIn('api.html?port=8765',browser.call_args.args[0])
                        app.connected=True
                        app._open_docs()
                        self.assertEqual(browser.call_args.args[0],'http://127.0.0.1:8765/docs?port=8765')
                        app.connected=False
                    app.detail.set('已连接现有服务，此窗口不会停止它。')
                    app._update_buttons()
                    root.update_idletasks()
                    width,height=root.minsize()
                    for window_width,window_height in ((width,height),(max(width,1080),height+300)):
                        root.geometry(f'{window_width}x{window_height}')
                        root.update_idletasks()
                        inspect_layout(app)
                        app.notebook.select(0)
                        root.update()
                        self.assertFalse(app.result.winfo_ismapped())
                        self.assertTrue(app.pool_views['wechat']['canvas'].winfo_ismapped())
                    app.closed=True
                finally:
                    for timer in root.tk.call('after','info'):
                        root.after_cancel(timer)
                    root.destroy()


if __name__=='__main__':unittest.main(verbosity=2)
