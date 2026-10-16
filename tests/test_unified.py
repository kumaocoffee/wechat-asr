"""Unified routing, independent pools and persisted settings without live ASR."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from backend import BackendError, Transcription
from bridge_config import validate_concurrency
from desktop_settings import save_settings, saved_concurrency
from wetype_backend import WetypeBackend
import doubao_backend
import server


class UnifiedApiTests(unittest.TestCase):
    def setUp(self):
        server.configure_backend('all')
        self.addCleanup(server.configure_backend, 'wechat')
        self.client=TestClient(server.app)

    def test_model_routes_are_independent_and_alias_is_wechat(self):
        expected=['wechat-voice','wetype-voice','doubao-voice','whisper-1']
        self.assertEqual([m['id'] for m in self.client.get('/v1/models').json()['data']],expected)
        for name,key in zip(expected,['wechat','wetype','doubao','wechat']):
            with self.subTest(model=name), patch.object(server.pools[key].backend,'transcribe',return_value=Transcription(key,1,0,1,1)) as call:
                response=self.client.post('/v1/audio/transcriptions',data={'model':name},files={'file':('test.wav',b'audio')})
                self.assertEqual(response.json(),{'text':key})
                call.assert_called_once_with(b'audio')
        states=self.client.get('/health').json()
        self.assertEqual(states['backend'],'asr-bridge')
        self.assertEqual(len(states['pools']),3)
        self.assertEqual(states['active_requests'],0)
        self.assertEqual([s['occupied_slots'] for s in states['pools']],[[],[],[]])

    def test_backend_failure_does_not_disable_another_route(self):
        with patch.object(server.pools['wechat'].backend,'transcribe',side_effect=BackendError('登录微信')), \
             patch.object(server.pools['doubao'].backend,'transcribe',return_value=Transcription('豆包',1,0,1,1)):
            post=lambda model:self.client.post('/v1/audio/transcriptions',data={'model':model},files={'file':('x.wav',b'a')})
            self.assertEqual(post('wechat-voice').status_code,503)
            self.assertEqual(post('doubao-voice').json(),{'text':'豆包'})
        self.assertEqual(self.client.get('/health').json()['active_requests'],0)

    def test_bundled_documentation_is_served(self):
        result=self.client.get('/docs')
        self.assertEqual(result.status_code,200)
        for value in ('response_format','doubao-voice','temperature','Authorization'):
            self.assertIn(value,result.text)
        self.assertIn('text/html',result.headers['content-type'])

    def test_pool_above_40_has_no_shared_worker_ceiling_and_isolates_queue(self):
        server.configure_backend('all',{'wechat':42,'wetype':2,'doubao':1})
        release=threading.Event()
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
                async def post(model):
                    return await client.post('/v1/audio/transcriptions',data={'model':model},files={'file':('x.wav',b'a')})
                tasks=[asyncio.create_task(post('wechat-voice')) for _ in range(44)]
                try:
                    for _ in range(300):
                        data=(await client.get('/health')).json()
                        wx=data['pools'][0]
                        if wx['active_requests']==42 and wx['queued_requests']==2:
                            break
                        await asyncio.sleep(.01)
                    else:
                        self.fail('42 native workers did not enter concurrently')
                    self.assertEqual(wx['occupied_slots'],list(range(42)))
                    response=await asyncio.wait_for(post('doubao-voice'),3)
                    self.assertEqual(response.json(),{'text':'doubao'})
                    self.assertEqual(data['pools'][1]['queued_requests'],0)
                finally:
                    release.set()
                    results=await asyncio.gather(*tasks)
                self.assertTrue(all(r.status_code==200 for r in results))
                states=(await client.get('/health')).json()
                self.assertEqual(states['active_requests'],0)
                self.assertEqual(states['queued_requests'],0)
        def slow(data):
            if not release.wait(10):
                raise RuntimeError('offline release timeout')
            return Transcription('wechat',1,0,1,1)
        with patch.object(server.pools['wechat'].backend,'transcribe',side_effect=slow), \
             patch.object(server.pools['doubao'].backend,'transcribe',return_value=Transcription('doubao',1,0,1,1)):
            asyncio.run(run())


class SettingsTests(unittest.TestCase):
    def test_large_capacity_persists_and_invalid_change_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            capacities={'wechat':108,'wetype':64,'doubao':33}
            save_settings(18765,runtime=directory,concurrency=capacities)
            self.assertEqual(saved_concurrency(directory),capacities)
            target=Path(directory)/'desktop-settings.json'
            before=target.read_bytes()
            for invalid in (0,-1,True,1.5):
                with self.assertRaises(ValueError):
                    save_settings(18766,runtime=directory,concurrency={**capacities,'wechat':invalid})
                self.assertEqual(target.read_bytes(),before)
            save_settings(18766,runtime=directory)
            self.assertEqual(saved_concurrency(directory),capacities)


class WetypeParallelTests(unittest.TestCase):
    def test_parallel_success_cannot_clear_another_owners_cleanup_failure(self):
        bridge=WetypeBackend()
        bridge._session,bridge._script,bridge._pid=MagicMock(),MagicMock(),123
        bridge._session.is_detached=False
        barrier=threading.Barrier(2)
        failed=threading.Event()
        owners={}
        def recognize(script,pcm,duration):
            owners[bridge._request_context.owner]=pcm
            barrier.wait(3)
            return pcm.decode(),1,10,'server_end_flag'
        def finalize(owner):
            if owners[owner]==b'A':
                failed.set()
                return {'cleaned':False}
            self.assertTrue(failed.wait(3))
            return {'cleaned':True}
        bridge._script.exports_sync.finalize.side_effect=finalize
        with patch('wetype_backend.normalize_audio',side_effect=lambda data:(data,1)), \
             patch.object(bridge,'_recognize',side_effect=recognize):
            with ThreadPoolExecutor(2) as executor:
                a,b=[executor.submit(bridge.transcribe,data) for data in (b'A',b'B')]
                with self.assertRaises(BackendError):a.result(timeout=5)
                self.assertEqual(b.result(timeout=5).text,'B')
        self.assertFalse(bridge.healthy)
        self.assertEqual(bridge.last_error,'cleanup_failed')

    def test_two_recognitions_overlap_and_finalize_only_their_owner(self):
        bridge=WetypeBackend()
        bridge._session,bridge._script,bridge._pid=MagicMock(),MagicMock(),123
        bridge._session.is_detached=False
        bridge._script.exports_sync.finalize.return_value={'cleaned':True}
        barrier=threading.Barrier(2)
        owners=set()
        def recognize(script,pcm,duration):
            owners.add(bridge._request_context.owner)
            barrier.wait(3)
            return pcm.decode(),1,10,'server_end_flag'
        with patch('wetype_backend.normalize_audio',side_effect=lambda data:(data,1)), \
             patch.object(bridge,'_recognize',side_effect=recognize):
            with ThreadPoolExecutor(2) as executor:
                results=list(executor.map(bridge.transcribe,[b'A',b'B']))
        self.assertEqual([r.text for r in results],['A','B'])
        self.assertEqual(len(owners),2)
        self.assertEqual({call.args[0] for call in bridge._script.exports_sync.finalize.call_args_list},owners)
        self.assertEqual(bridge._active_requests,0)


class DoubaoAdapterTests(unittest.TestCase):
    def test_normalized_audio_stays_in_memory_and_errors_release_ownership(self):
        bridge=doubao_backend.DoubaoBackend()
        result=MagicMock(text='测试')
        with patch.object(doubao_backend,'normalize_audio',return_value=(b'\0'*640,.02)), \
             patch.object(doubao_backend,'transcribe_pcm',return_value=result) as call:
            self.assertEqual(bridge.transcribe(b'compressed audio').text,'测试')
            call.assert_called_once_with(b'\0'*640,duration=.02)
            call.side_effect=doubao_backend.ASRError('等待 ASREnded 超时')
            with self.assertRaises(BackendError) as error:
                bridge.transcribe(b'audio')
            self.assertEqual(error.exception.status,504)
        self.assertEqual(bridge.active,0)
        bridge.close()


if __name__=='__main__':unittest.main()
