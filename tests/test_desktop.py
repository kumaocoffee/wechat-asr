"""Portable resource paths, service ownership and desktop API behavior."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import app_paths
import desktop_service
from desktop_service import ServiceController, transcribe_file
import server


class PortablePathsTests(unittest.TestCase):
    def test_frozen_assets_and_logs_use_different_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app_paths.sys, 'frozen', True, create=True), patch.object(app_paths.sys, '_MEIPASS', str(root / 'bundle'), create=True), patch.dict(app_paths.os.environ, {'LOCALAPPDATA': str(root / 'user')}):
                self.assertEqual(app_paths.resource_dir(), root / 'bundle')
                self.assertEqual(app_paths.runtime_dir(), root / 'user' / 'WeChatASRBridge' / 'runtime')

    def test_only_port_is_persisted_and_bad_settings_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(desktop_service.saved_port(root), 8765)
            desktop_service.save_port(8768, root)
            self.assertEqual(desktop_service.saved_port(root), 8768)
            self.assertEqual(json.loads((root / 'desktop-settings.json').read_text()), {'port': 8768})
            (root / 'desktop-settings.json').write_text('{bad json')
            self.assertEqual(desktop_service.saved_port(root), 8765)

    def test_server_rejects_shutdown_signal_outside_its_runtime(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server.uvicorn, 'Server') as launch:
            root = Path(directory)
            with patch.object(server, 'runtime_dir', return_value=root / 'runtime'):
                with self.assertRaises(SystemExit) as error:
                    server.main(['--shutdown-file', str(root / 'outside')])
            self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()


class ServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = ServiceController(18765, runtime=self.root)

    def metadata(self, **changes):
        value = {'pid': 123, 'port': 18765, 'shutdown_file': str(self.root / 'desktop-stop-18765-test'),
                 'executable': str(Path(desktop_service.sys.executable).resolve())}
        value.update(changes)
        self.controller.metadata_path.write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_existing_service_is_reused_without_launching_a_child(self):
        current = {'backend': desktop_service.APP_BACKEND, 'status': 'ok'}
        with patch.object(desktop_service, 'health', return_value=current), patch.object(desktop_service.subprocess, 'Popen') as launch:
            self.assertEqual(self.controller.start(), current)
            launch.assert_not_called()
        self.assertFalse(self.controller.owns_service())

    def test_unrelated_listener_is_never_replaced(self):
        with patch.object(desktop_service, 'health', return_value=None), patch.object(desktop_service, 'port_in_use', return_value=True), patch.object(desktop_service.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '端口'):
                self.controller.start()
            launch.assert_not_called()

    def test_forged_pid_or_path_cannot_signal_another_program(self):
        data = self.metadata()
        info = {'ExecutablePath': data['executable'], 'CommandLine': 'unrelated program'}
        with patch.object(desktop_service, 'process_info', return_value=info):
            with self.assertRaisesRegex(RuntimeError, '其他程序'):
                self.controller.stop()
        self.assertFalse(Path(data['shutdown_file']).exists())
        self.metadata(shutdown_file=str(self.root.parent / 'outside'))
        self.assertIsNone(self.controller._metadata())
        self.metadata(executable='relative.exe')
        self.assertIsNone(self.controller._metadata())

    def test_verified_child_gets_only_its_graceful_signal(self):
        data = self.metadata()
        info = {'ExecutablePath': data['executable'],
                'CommandLine': f'"{data["executable"]}" --serve --shutdown-file "{data["shutdown_file"]}"'}
        with patch.object(desktop_service, 'process_info', side_effect=[info, None]):
            self.controller.stop()
        self.assertTrue(Path(data['shutdown_file']).is_file())


class DesktopTranscriptionTests(unittest.TestCase):
    def test_audio_response_and_authentication_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'sample.wav'
            audio.write_bytes(b'synthetic')
            def respond(request):
                self.assertEqual(request.url.path, '/v1/audio/transcriptions')
                self.assertEqual(request.headers['Authorization'], 'Bearer local-test')
                self.assertIn(b'synthetic', request.read())
                return httpx.Response(200, json={'text': '测试文字'})
            client = httpx.Client(transport=httpx.MockTransport(respond))
            with patch.object(httpx, 'Client', return_value=client):
                self.assertEqual(transcribe_file(audio, 18765, 'local-test'), '测试文字')
            client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={'error': {'message': 'bad key'}})))
            with patch.object(httpx, 'Client', return_value=client):
                with self.assertRaisesRegex(RuntimeError, '密钥'):
                    transcribe_file(audio)

    def test_oversized_audio_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'large.wav'
            with audio.open('wb') as output:
                output.truncate(desktop_service.MAX_UPLOAD + 1)
            with patch.object(httpx, 'Client') as client:
                with self.assertRaisesRegex(ValueError, '25 MiB'):
                    transcribe_file(audio)
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
