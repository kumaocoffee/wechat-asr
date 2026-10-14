# Build an onedir distribution: no large self-extracting temp directory on launch.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import imageio_ffmpeg

root = Path(SPECPATH).parent
ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
datas = [(str(root / 'agent.js'), '.'),
         (str(root / 'assets' / 'app.ico'), 'assets'),
         (str(root / 'tests' / 'fixtures' / 'test.wav'), 'assets'),
         (str(root / 'LICENSE'), '.'),
         (str(root / 'docs' / 'PORTABLE.md'), '.')]
datas += collect_data_files('frida')
a = Analysis([str(root / 'desktop.py')], pathex=[str(root)],
             binaries=[(str(ffmpeg), 'imageio_ffmpeg/binaries')], datas=datas,
             hiddenimports=collect_submodules('uvicorn') + ['pystray._win32'],
             excludes=['pytest', 'unittest', 'IPython', 'numpy', 'matplotlib',
                       'pystray._darwin', 'pystray._gtk', 'pystray._appindicator', 'pystray._xorg'],
             noarchive=False)
# A conda base environment on PATH can supply older Tcl/Tk DLLs even when
# the hook correctly gathers scripts from the active prefix. Prefer the DLLs
# from the interpreter's own prefix; official Python builds keep hook defaults.
for index, (name, source, kind) in enumerate(a.binaries):
    if Path(name).name.lower() in ('tcl86t.dll', 'tk86t.dll'):
        candidate = Path(sys.base_prefix) / 'Library' / 'bin' / Path(name).name
        if candidate.is_file():
            a.binaries[index] = (name, str(candidate), kind)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WeChatASRBridge',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(root / 'assets' / 'app.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WeChatASRBridge')
