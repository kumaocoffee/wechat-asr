"""Concurrency, request isolation and agent lifecycle without live injection."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

import anyio
import httpx

import backend
from backend import BackendError, Transcription, WechatBackend
import server


def native_result(text):
    return {'done': True, 'cleaned': True, 'events': [{'event': 'final_result', 'text': text}],
            'frames': 1, 'bytes': 20}


class BackendConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.bridge = WechatBackend()
        self.session, self.agent = MagicMock(), MagicMock()
        self.session.is_detached = False
        self.bridge._session = self.session
        self.bridge._script = self.agent
        self.bridge._pid = 123
        self.bridge._generation = 1
        self.agent.exports_sync.finalize.return_value = {'cleaned': True}
        self.agent.exports_sync.dispose.return_value = {'cleaned': True}
        self.discovery = patch.object(backend, 'discover_pid', return_value=123)
        self.normalize = patch.object(backend, 'normalize_audio', side_effect=lambda data: (data, 1))
        self.discovery.start()
        self.normalize.start()
        self.addCleanup(self.discovery.stop)
        self.addCleanup(self.normalize.stop)

    def test_rpc_calls_overlap_and_finalize_only_their_own_ids(self):
        barrier = threading.Barrier(2)
        seen = {}
        def recognize(pcm, request_id, *args):
            seen[request_id] = chr(pcm[0])
            barrier.wait(timeout=5)
            return native_result(chr(pcm[0]))
        self.agent.exports_sync.recognize.side_effect = recognize
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.bridge.transcribe, (b'A', b'B')))
        self.assertEqual([result.text for result in results], ['A', 'B'])
        self.assertEqual(len(seen), 2)
        self.assertEqual({call.args[0] for call in self.agent.exports_sync.finalize.call_args_list}, set(seen))
        self.agent.unload.assert_not_called()
        self.session.detach.assert_not_called()
        self.bridge.close()
        self.agent.exports_sync.dispose.assert_called_once()

    def test_attached_agent_survives_picture_viewer_window_title(self):
        with patch.object(backend, 'discover_pid', side_effect=BackendError('窗口标题为图片和视频')) as discovery:
            self.agent.exports_sync.recognize.return_value = native_result('A')
            self.assertEqual(self.bridge.transcribe(b'A').text, 'A')
            discovery.assert_not_called()
        self.assertTrue(self.bridge.agent_connected)

    def test_unexpected_disconnect_quarantines_the_live_process(self):
        self.session.is_detached = True
        self.bridge._agent_detached(self.session, 123, 1, 'connection-terminated')
        with self.assertRaises(BackendError) as error:
            self.bridge.transcribe(b'A')
        self.assertEqual(error.exception.code, 'agent_disconnected')
        self.assertFalse(self.bridge.healthy)
        self.assertEqual(self.bridge._unsafe_pid, 123)
        self.agent.exports_sync.recognize.assert_not_called()

    def test_unexpected_disconnect_during_rpc_is_not_treated_as_process_exit(self):
        def disconnect(*args):
            self.session.is_detached = True
            self.bridge._agent_detached(self.session, 123, 1, 'connection-terminated')
            raise RuntimeError('connection terminated')
        self.agent.exports_sync.recognize.side_effect = disconnect
        with self.assertRaises(BackendError):
            self.bridge.transcribe(b'A')
        self.assertIsNone(self.bridge._session)
        self.assertFalse(self.bridge.healthy)
        with self.assertRaises(BackendError) as error:
            self.bridge.transcribe(b'B')
        self.assertEqual(error.exception.code, 'agent_disconnected')
        self.assertEqual(self.agent.exports_sync.recognize.call_count, 1)

    def test_process_exit_and_expected_detach_do_not_quarantine(self):
        self.bridge._agent_detached(self.session, 123, 1, 'process-terminated')
        self.assertIsNone(self.bridge._unsafe_pid)
        self.bridge._expected_detach = True
        self.bridge._agent_detached(self.session, 123, 1, 'application-requested')
        self.assertIsNone(self.bridge._unsafe_pid)

    def test_detach_signal_does_not_wait_for_the_shutdown_ownership_lock(self):
        self.bridge._expected_detach = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.bridge.lock:
                callback=executor.submit(self.bridge._agent_detached,self.session,123,1,'application-requested')
                callback.result(timeout=2)
        self.assertIsNone(self.bridge._unsafe_pid)

    def test_quarantine_recovers_only_with_a_new_wechat_process(self):
        self.session.is_detached = True
        self.bridge._agent_detached(self.session,123,1,'connection-terminated')
        self.bridge._close_agent()
        session,metadata,agent=MagicMock(),MagicMock(),MagicMock()
        session.is_detached=False
        session.create_script.side_effect=[metadata,agent]
        metadata.exports_sync.info.return_value={'path':'verified.dll'}
        agent.exports_sync.recognize.return_value=native_result('B')
        agent.exports_sync.finalize.return_value={'cleaned':True}
        agent.exports_sync.dispose.return_value={'cleaned':True}
        with patch.object(backend,'discover_pid',return_value=456), patch.object(backend.frida,'attach',return_value=session), patch.object(self.bridge,'verify_dll'):
            result=self.bridge.transcribe(b'B')
            self.assertEqual(result.pid,456)
            self.assertTrue(self.bridge.healthy)
            self.assertIsNone(self.bridge._unsafe_pid)
            self.bridge.close()
    def test_lifecycle_logs_do_not_record_transcription_text(self):
        with self.assertLogs('backend', level='INFO') as logs:
            self.bridge._agent_message(123, 1, {'type':'send','payload':{
                'event':'native_drained','id':'A','references':1,'elapsed_ms':50,'text':'private transcript'}}, None)
            self.bridge._agent_message(123, 1, {'type':'send','payload':{
                'event':'final_result','text':'private transcript'}}, None)
        self.assertIn('native_drained', logs.output[0])
        self.assertNotIn('private transcript', '\n'.join(logs.output))

    def test_close_waits_for_every_in_flight_request(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        def recognize(pcm, request_id, *args):
            barrier.wait(timeout=5)
            self.assertTrue(release.wait(timeout=5))
            return native_result(chr(pcm[0]))
        self.agent.exports_sync.recognize.side_effect = recognize
        with ThreadPoolExecutor(max_workers=3) as executor:
            requests = [executor.submit(self.bridge.transcribe, data) for data in (b'A', b'B')]
            try:
                barrier.wait(timeout=5)
                closing = executor.submit(self.bridge.close)
                with self.bridge.lock:
                    self.bridge.lock.wait_for(lambda: self.bridge._closing, timeout=5)
                self.assertTrue(self.bridge._closing)
                self.assertFalse(closing.done())
                self.agent.exports_sync.dispose.assert_not_called()
                with self.assertRaises(BackendError) as error:
                    self.bridge.transcribe(b'C')
                self.assertEqual(error.exception.code, 'server_stopping')
            finally:
                release.set()
            self.assertEqual([request.result(timeout=5).text for request in requests], ['A', 'B'])
            closing.result(timeout=5)
        self.agent.exports_sync.dispose.assert_called_once()
        self.assertEqual(self.bridge._active_requests, 0)

    def test_parallel_success_cannot_hide_uncertain_cleanup(self):
        barrier = threading.Barrier(2)
        release_success = threading.Event()
        broken_ids = set()
        def recognize(pcm, request_id, *args):
            if pcm[0] == ord('A'):
                broken_ids.add(request_id)
            barrier.wait(timeout=5)
            return native_result(chr(pcm[0]))
        def finalize(request_id):
            if request_id in broken_ids:
                return {'cleaned': False}
            self.assertTrue(release_success.wait(timeout=5))
            return {'cleaned': True}
        self.agent.exports_sync.recognize.side_effect = recognize
        self.agent.exports_sync.finalize.side_effect = finalize
        with ThreadPoolExecutor(max_workers=2) as executor:
            failed = executor.submit(self.bridge.transcribe, b'A')
            successful = executor.submit(self.bridge.transcribe, b'B')
            try:
                with self.assertRaises(BackendError) as error:
                    failed.result(timeout=5)
                self.assertEqual(error.exception.code, 'cleanup_failed')
            finally:
                release_success.set()
            self.assertEqual(successful.result(timeout=5).text, 'B')
        self.assertFalse(self.bridge.healthy)
        self.assertEqual(self.bridge.last_error, 'cleanup_failed')
        with self.assertRaises(BackendError):
            self.bridge.transcribe(b'C')
        self.assertEqual(self.agent.exports_sync.recognize.call_count, 2)
        self.agent.unload.assert_not_called()
        self.session.detach.assert_not_called()


class AdmissionTests(unittest.TestCase):
    def test_eight_requests_queue_at_four_slots_without_429(self):
        async def run():
            release = threading.Event()
            counters = {'active': 0, 'peak': 0}
            lock = threading.Lock()
            def transcribe(data):
                with lock:
                    counters['active'] += 1
                    counters['peak'] = max(counters['peak'], counters['active'])
                try:
                    if not release.wait(timeout=10):
                        raise RuntimeError('Test release timeout')
                    return Transcription(data.decode(), 1, 123, 1, 20)
                finally:
                    with lock:
                        counters['active'] -= 1
            with patch.object(server, 'gate', anyio.Semaphore(4)), patch.object(server.backend, 'transcribe', side_effect=transcribe):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
                    requests = [asyncio.create_task(client.post('/v1/audio/transcriptions',
                                data={'model': 'whisper-1'}, files={'file': ('test.wav', str(i).encode())})) for i in range(8)]
                    try:
                        for _ in range(200):
                            health = (await client.get('/health')).json()
                            if health['queued_requests'] == 4:
                                break
                            await asyncio.sleep(0.01)
                        else:
                            self.fail('Did not observe four queued requests')
                        self.assertEqual(health['active_requests'], 4)
                        self.assertEqual(health['max_concurrency'], 4)
                        self.assertTrue(health['busy'])
                    finally:
                        release.set()
                    responses = await asyncio.gather(*requests)
                    self.assertEqual([response.status_code for response in responses], [200] * 8)
                    self.assertEqual([response.json()['text'] for response in responses], list(map(str, range(8))))
                    self.assertEqual(counters['peak'], 4)
                    health = (await client.get('/health')).json()
                    self.assertEqual(health['active_requests'], 0)
                    self.assertEqual(health['queued_requests'], 0)
                    self.assertFalse(health['busy'])
        asyncio.run(run())

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for the isolated agent lifecycle tests')
    def test_agent_scheduler_and_callback_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([shutil.which('node'), str(root / 'tests' / 'verify_agent.cjs')],
                       cwd=root, check=True, timeout=15)


if __name__ == '__main__':
    unittest.main(verbosity=2)
