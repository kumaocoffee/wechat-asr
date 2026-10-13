"""Contract and audio-validation checks; no live Weixin injection."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import wave

from fastapi.testclient import TestClient
from backend import BackendError, Transcription, normalize_audio, WechatBackend
import backend
import server


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.audio = Path(__file__).with_name('fixtures').joinpath('test.wav').read_bytes()

    def post(self, fields=None, audio=None):
        return self.client.post('/v1/audio/transcriptions',
            data=fields or {'model': 'whisper-1'},
            files={'file': ('audio.wav', self.audio if audio is None else audio, 'audio/wav')})

    def test_sdk_contract_and_formats(self):
        value = Transcription('你好，接口测试', 6.28, 123, 314, 10136)
        with patch.object(server.backend, 'transcribe', return_value=value):
            r = self.post()
            self.assertEqual(r.json(), {'text': value.text})
            self.assertEqual(r.headers['x-wechat-session-cleaned'], 'true')
            self.assertTrue(r.headers['x-request-id'].startswith('req_'))
            text = self.post({'model': 'wechat-voice', 'response_format': 'text'})
            self.assertEqual(text.text, value.text)
            self.assertIn('text/plain', text.headers['content-type'])

    def test_unsupported_features_are_explicit_errors(self):
        with patch.object(server.backend, 'transcribe') as call:
            for extra in ({'response_format': 'srt'}, {'stream': 'true'}, {'language': 'en'},
                          {'prompt': 'test'}, {'temperature': '1'}, {'include[]': 'logprobs'}):
                r = self.post({'model': 'whisper-1', **extra})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()['error']['type'], 'invalid_request_error')
            call.assert_not_called()

    def test_authentication_and_backend_unavailable(self):
        with patch.dict(os.environ, {'WECHAT_API_KEY': 'test-only-key'}):
            self.assertEqual(self.client.get('/v1/models').status_code, 401)
            r = self.client.get('/v1/models', headers={'Authorization': 'Bearer test-only-key'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual([x['id'] for x in r.json()['data']], ['wechat-voice', 'whisper-1'])
        with patch.object(server.backend, 'transcribe', side_effect=BackendError('请登录微信')):
            r = self.post()
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()['error']['code'], 'wechat_unavailable')

    def test_missing_and_invalid_input(self):
        self.assertEqual(self.client.post('/v1/audio/transcriptions', json={}).status_code, 400)
        self.assertEqual(self.post({'model': 'unsupported'}).status_code, 400)
        self.assertEqual(self.client.get('/v1/models/missing').status_code, 404)

    def test_audio_duration_is_not_silently_truncated(self):
        out = io.BytesIO()
        with wave.open(out, 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0' * 16 * 32000)
        with self.assertRaises(BackendError) as error:
            normalize_audio(out.getvalue())
        self.assertEqual(error.exception.code, 'audio_too_long')
        with self.assertRaises(BackendError):
            normalize_audio(b'not an audio file')
        pcm, seconds = normalize_audio(self.audio)
        self.assertAlmostEqual(seconds, 6.28)
        self.assertEqual(len(pcm) % 640, 0)

    def test_wrong_dll_is_rejected_before_native_hooks_load(self):
        bridge = WechatBackend()
        session = MagicMock()
        metadata = MagicMock()
        session.create_script.return_value = metadata
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / 'Weixin.dll'
            dll.write_bytes(b'unsupported-build')
            metadata.exports_sync.info.return_value = {'path': str(dll)}
            with patch.object(backend, 'discover_pid', return_value=123), patch.object(backend.frida, 'attach', return_value=session):
                with self.assertRaises(BackendError) as error:
                    bridge.transcribe(self.audio)
        self.assertIn('指纹', str(error.exception))
        self.assertEqual(session.create_script.call_count, 1)
        metadata.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_cleanup_failure_stops_future_native_calls(self):
        bridge = WechatBackend()
        session, metadata, agent = MagicMock(), MagicMock(), MagicMock()
        session.create_script.side_effect = [metadata, agent]
        metadata.exports_sync.info.return_value = {'path': 'test.dll'}
        agent.exports_sync.recognize.return_value = {
            'done': True, 'events': [{'event': 'final_result', 'text': '测试'}],
            'frames': 1, 'bytes': 20,
        }
        agent.exports_sync.finalize.return_value = {'cleaned': False}
        with patch.object(backend, 'discover_pid', return_value=123), patch.object(backend.frida, 'attach', return_value=session) as attach, patch.object(bridge, 'verify_dll'):
            with self.assertRaises(BackendError) as error:
                bridge.transcribe(self.audio)
            self.assertEqual(error.exception.code, 'cleanup_failed')
            self.assertFalse(bridge.healthy)
            with self.assertRaises(BackendError):
                bridge.transcribe(self.audio)
            attach.assert_called_once()
        session.detach.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
