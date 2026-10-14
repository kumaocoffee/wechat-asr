"""Build the Windows portable folder, ZIP, notices and SHA-256 checksums."""
import hashlib
import importlib.metadata
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_service import VERSION


def icon():
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 244, 244), radius=52, fill='#18364A')
    draw.rounded_rectangle((48, 48, 208, 182), radius=24, fill='#137D78')
    draw.polygon([(68, 176), (68, 215), (116, 176)], fill='#137D78')
    for x, top, bottom in ((80, 92, 140), (118, 69, 163), (156, 83, 149)):
        draw.rounded_rectangle((x, top, x + 18, bottom), radius=9, fill='white')
    target = ROOT / 'assets' / 'app.ico'
    target.parent.mkdir(exist_ok=True)
    image.save(target, sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])


def notices(folder):
    target = folder / 'licenses'
    target.mkdir(exist_ok=True)
    lines = ['Bundled dependencies retain their own licenses.',
             'No Weixin binaries or account data are included.', '']
    packages = ['frida', 'fastapi', 'starlette', 'uvicorn', 'anyio', 'imageio-ffmpeg',
                'httpx', 'httpcore', 'h11', 'pydantic', 'pydantic-core', 'python-multipart',
                'certifi', 'idna', 'typing-extensions', 'openai', 'pyinstaller']
    for name in packages:
        distribution = importlib.metadata.distribution(name)
        lines.append(f'{distribution.metadata["Name"]} {distribution.version}')
        for file in distribution.files or []:
            if ('license' in str(file).lower() or Path(file).name.lower().startswith(('copying', 'notice'))):
                source = Path(distribution.locate_file(file))
                if source.is_file() and source.suffix.lower() in ('', '.txt', '.md', '.rst'):
                    destination = target / name / Path(file).name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
    # The binary from imageio-ffmpeg is a separate FFmpeg distribution.
    ffmpeg_notice = ('The bundled ffmpeg executable is obtained from imageio-ffmpeg.\n'
                     'FFmpeg license details and corresponding source: https://ffmpeg.org/legal.html\n'
                     'Bundled binary repository: https://github.com/imageio/imageio-binaries/tree/master/ffmpeg\n'
                     'Windows binary provider: https://www.gyan.dev/ffmpeg/builds/\n'
                     'FFmpeg 7.1 source: https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz\n'
                     'Run the executable with -L and -version for this build\'s license and configuration.\n')
    (target / 'ffmpeg.txt').write_text(ffmpeg_notice, encoding='utf-8')
    shutil.copyfile(ROOT / 'packaging' / 'licenses' / 'FFmpeg-GPLv3.txt', target / 'FFmpeg-GPLv3.txt')
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([ffmpeg, '-version'], capture_output=True, text=True, check=True)
    (target / 'ffmpeg-build-configuration.txt').write_text(result.stdout, encoding='utf-8')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        python_license = ROOT / 'packaging' / 'licenses' / 'Python-LICENSE.txt'
    shutil.copyfile(python_license, target / 'Python-LICENSE.txt')
    shutil.copyfile(ROOT / 'packaging' / 'licenses' / 'Tcl-license.terms', target / 'Tcl-license.terms')
    for component in ('tcl8.6', 'tk8.6'):
        source = Path(sys.base_prefix) / 'Library' / 'lib' / component / 'license.terms'
        if source.is_file():
            shutil.copyfile(source, target / f'{component}-license.terms')
    (folder / 'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(lines), encoding='utf-8')


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build this package on Windows x64.')
    build, dist = ROOT / 'build', ROOT / 'dist'
    for directory in (build, dist):
        # PyInstaller may replace its output folders. Never pass an output path
        # resolving outside this checkout, including through a junction/symlink.
        if not directory.resolve().is_relative_to(ROOT.resolve()):
            raise SystemExit('Unsafe build output path.')
    icon()
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(dist), '--workpath', str(build),
                    str(ROOT / 'packaging' / 'portable.spec')], check=True, cwd=ROOT)
    folder = dist / 'WeChatASRBridge'
    shutil.copyfile(ROOT / 'docs' / 'PORTABLE.md', folder / '使用说明.md')
    shutil.copyfile(ROOT / 'LICENSE', folder / 'LICENSE.txt')
    notices(folder)
    name = f'WeChatASRBridge-{VERSION}-windows-x64-portable.zip'
    archive = dist / name
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(dist))
    with archive.open('rb') as input_file:
        digest = hashlib.file_digest(input_file, 'sha256').hexdigest()
    (dist / 'SHA256SUMS.txt').write_text(f'{digest}  {name}\n', encoding='utf-8')
    print(f'Portable ZIP: {archive}', flush=True)
    print(f'SHA-256: {digest}', flush=True)


if __name__ == '__main__':
    main()
