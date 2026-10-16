"""Offline checks for the optional Doubao experiment; never contact the service."""
import io
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch
import wave

from experiments.doubao_asr import client
from experiments.doubao_asr.protocol import ProtocolError, decode_response, encode_request, varint


class ProtocolTests(unittest.TestCase):
    def test_observed_wire_fields_and_unknown_fields(self):
        # Fixed wire bytes independent of the encoder under test.
        wire = b'\x0a\x04task\x22\x0bTaskStarted\x28\x80\xda\xc4\x09'
        self.assertEqual(decode_response(wire), {
            'task_id': 'task', 'event': 'TaskStarted', 'status_code': 20000000})
        unknown = b'\x61' + b'\x00' * 8 + b'\x6d' + b'\x00' * 4 + b'\x72\x03abc'
        self.assertEqual(decode_response(wire + unknown), decode_response(wire))
        self.assertEqual(varint(300), b'\xac\x02')
        self.assertIn(b'\x2a\x09StartTask', encode_request('StartTask', 'app'))
        self.assertIn(b'\x3a\x02ab\x48\x02', encode_request(
            'TaskRequest', 'app', audio=b'ab', sequence=2))

    def test_malformed_frames_are_rejected(self):
        for wire in (b'\x22\x08abc', b'\x28\x80', b'\x00', b'\x27',
                     b'\x28' + b'\xff' * 10, b'\x22\x01\xff', b'\x20\x01'):
            with self.subTest(wire=wire), self.assertRaises(ProtocolError):
                decode_response(wire)
        for value in (-1, 2**64):
            with self.assertRaises(ProtocolError):
                varint(value)
        for value in ('[1]', 'not json'):
            with self.assertRaises(ProtocolError):
                decode_response(value)


class VisitorTests(unittest.TestCase):
    def response(self, layout):
        router = {'loaderData': {'chat_layout': {'chat_layout': layout}}}
        return io.BytesIO(('window._ROUTER_DATA = ' + json.dumps(router)).encode())

    @patch.object(client.urllib.request, 'build_opener')
    def test_fresh_homepage_identity_and_no_persisted_cookie(self, build):
        build.return_value.open.return_value = self.response({
            'aid': 497858, 'pc_version': 'test-version',
            'launchCore': {'web_id': '123456789', 'ttwid': 'test-only-cookie'}})
        context = client.bootstrap_guest()
        self.assertEqual(context['device_id'], '123456789')
        self.assertEqual(context['pc_version'], 'test-version')
        self.assertNotIn('ttwid', context)
        self.assertNotIn('cookie', context)
        self.assertEqual(len(build.call_args.args[0].cookiejar), 0)
        request = build.return_value.open.call_args.args[0]
        self.assertFalse(request.has_header('Cookie'))

    @patch.object(client.urllib.request, 'build_opener')
    def test_changed_homepage_or_invalid_identity_fails_closed(self, build):
        for layout in ({}, {'aid': 497858, 'pc_version': 'x',
                           'launchCore': {'web_id': '0'}}):
            build.return_value.open.return_value = self.response(layout)
            with self.assertRaises(client.ASRError):
                client.bootstrap_guest()


class FakeSocket:
    def __init__(self, scenario='success'):
        self.responses = queue.Queue()
        self.sent = []
        self.closed = False
        self.scenario = scenario

    def send_binary(self, packet):
        for event in ('StartTask', 'StartSession', 'TaskRequest', 'EndASR',
                      'FinishSession', 'CancelSession'):
            marker = b'\x2a' + bytes([len(event)]) + event.encode()
            if marker in packet:
                break
        else:
            raise AssertionError('Unexpected request envelope')
        self.sent.append(event)
        if event == 'TaskRequest':
            return
        mapping = {'StartTask': 'TaskStarted', 'StartSession': 'SessionStarted',
                   'EndASR': 'ASREnded', 'FinishSession': 'SessionFinished',
                   'CancelSession': 'SessionCanceled'}
        if event == 'EndASR' and self.scenario == 'success':
            self.responses.put(json.dumps({'event': 'ASRResponse',
                'status_code': 20000000, 'payload': json.dumps({'results': [
                    {'text': '未完成', 'is_interim': True}]})}))
            self.responses.put(json.dumps({'event': 'ASRResponse',
                'status_code': 20000000, 'payload': json.dumps({'results': [
                    {'text': '最终文字', 'is_interim': False}]})}))
        if event == 'StartSession' and self.scenario == 'denied':
            self.responses.put(json.dumps({'event': 'SessionFailed',
                'status_code': 40200001, 'status_text': 'denied'}))
        elif event == 'StartSession' and self.scenario == 'timeout':
            return
        else:
            self.responses.put(json.dumps({'event': mapping[event], 'task_id': 'test-task',
                                            'status_code': 20000000}))

    def recv(self):
        import websocket
        try:
            return self.responses.get(timeout=0.02)
        except queue.Empty:
            if self.closed:
                return ''
            raise websocket.WebSocketTimeoutException()

    def close(self, timeout=1):
        self.closed = True


@unittest.skipUnless(importlib.util.find_spec('websocket'), '需要可选的 websocket-client 依赖')
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name) / 'sample.wav'
        with wave.open(str(self.audio), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0' * 32)

    def run_session(self, scenario):
        socket = FakeSocket(scenario)
        context = {'device_id': 'test-only-device'}
        with patch.object(client, 'bootstrap_guest', return_value=context), \
             patch('websocket.create_connection', return_value=socket) as connect:
            try:
                result = client.transcribe(self.audio, timeout=0.1)
            finally:
                self.assertTrue(socket.closed)
                self.assertNotIn('cookie', connect.call_args.kwargs)
                self.assertNotIn('Cookie', connect.call_args.kwargs.get('header', {}))
        return result, socket

    def test_final_result_and_explicit_session_finish(self):
        result, socket = self.run_session('success')
        self.assertEqual(result.text, '最终文字')
        self.assertEqual(socket.sent, ['StartTask', 'StartSession', 'TaskRequest',
                                       'EndASR', 'FinishSession'])

    def test_rejection_timeout_and_missing_final_do_not_report_success(self):
        for scenario in ('denied', 'timeout', 'no-final'):
            with self.subTest(scenario=scenario), self.assertRaises(client.ASRError):
                self.run_session(scenario)

    @patch.object(client, 'bootstrap_guest')
    def test_invalid_audio_does_not_create_a_visitor(self, bootstrap):
        with wave.open(str(self.audio), 'wb') as audio:
            audio.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0' * 32)
        with self.assertRaises(client.ASRError):
            client.transcribe(self.audio)
        bootstrap.assert_not_called()


if __name__ == '__main__':
    unittest.main()
