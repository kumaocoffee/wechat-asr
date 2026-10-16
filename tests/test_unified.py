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
from desktop_settings import save_settings, saved_concurrency, saved_auth_enabled, saved_api_key
from wetype_backend import WetypeBackend
import doubao_backend
import server


class UnifiedApiTests(unittest.TestCase):
    def setUp(self):
        server.configure_backend('all')
        self.addCleanup(server.configure_backend, 'wechat')
        self.client=TestClient(server.app)

    def test_model_routes_are_independent(self):
        expected=['wechat-voice','wetype-voice','doubao-voice']
        self.assertEqual([m['id'] for m in self.client.get('/v1/models').json()['data']],expected)
        for name,key in zip(expected,['wechat','wetype','doubao']):
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

    def test_removed_alias_and_missing_model_are_rejected(self):
        self.assertEqual(self.client.get('/v1/models/whisper-1').status_code,404)
        for fields in ({'model':'whisper-1'}, {}):
            response=self.client.post('/v1/audio/transcriptions',data=fields,files={'file':('x.wav',b'a')})
            self.assertEqual(response.status_code,400)
        schema=self.client.get('/openapi.json').json()['paths']['/v1/audio/transcriptions']['post']
        model=schema['requestBody']['content']['multipart/form-data']['schema']['properties']['model']
        self.assertNotIn('default',model)

    def test_arbitrary_authorization_is_ignored_when_auth_is_disabled(self):
        with patch.dict('os.environ', {'WECHAT_API_KEY':''}), patch.object(
                server.pools['wechat'].backend,'transcribe',return_value=Transcription('ok',1,0,1,1)):
            for header in (None,'Bearer anything','Basic arbitrary','', 'Bearer sk-client-placeholder'):
                headers={} if header is None else {'Authorization':header}
                self.assertEqual(self.client.get('/v1/models',headers=headers).status_code,200)
                result=self.client.post('/v1/audio/transcriptions',headers=headers,
                    data={'model':'wechat-voice'},files={'file':('x.wav',b'a')})
                self.assertEqual(result.json(),{'text':'ok'})

    def test_default_capacities_admit_twelve_requests_independently(self):
        self.assertEqual(validate_concurrency(),dict.fromkeys(('wechat','wetype','doubao'),4))
        self._exercise_limit(None, ['wechat-voice']*4+['wetype-voice']*4+['doubao-voice']*4,12,0)

    def test_custom_provider_capacity_above_40_has_no_shared_ceiling(self):
        self._exercise_limit({'wechat':42,'wetype':2,'doubao':1}, ['wechat-voice']*44+['doubao-voice'],43,2)

    def _exercise_limit(self, capacities, models, active, queued):
        server.configure_backend('all',capacities)
        release=threading.Event()
        def slow(data):
            if not release.wait(10):
                raise RuntimeError('offline release timeout')
            return Transcription('ok',1,0,1,1)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
                tasks=[asyncio.create_task(client.post('/v1/audio/transcriptions',data={'model':model},
                       files={'file':('x.wav',b'a')})) for model in models]
                try:
                    for _ in range(300):
                        data=(await client.get('/health')).json()
                        self.assertLessEqual(data['active_requests'],sum(p['max_concurrency'] for p in data['pools']))
                        if data['active_requests']==active and data['queued_requests']==queued:
                            break
                        await asyncio.sleep(.01)
                    else:self.fail('admission did not reach expected running and queued counts')
                    self.assertEqual(data['max_concurrency'],sum(p['max_concurrency'] for p in data['pools']))
                    for pool in data['pools']:
                        self.assertLessEqual(pool['active_requests'],pool['max_concurrency'])
                        self.assertEqual(len(pool['occupied_slots']),pool['active_requests'])
                finally:
                    release.set()
                    results=await asyncio.gather(*tasks)
                self.assertTrue(all(r.status_code==200 for r in results))
                data=(await client.get('/health')).json()
                self.assertEqual((data['active_requests'],data['queued_requests']),(0,0))
        with patch.object(server.pools['wechat'].backend,'transcribe',side_effect=slow), \
             patch.object(server.pools['wetype'].backend,'transcribe',side_effect=slow), \
             patch.object(server.pools['doubao'].backend,'transcribe',side_effect=slow):
            asyncio.run(run())

    def test_local_queue_cancellation_cleans_waiters_without_affecting_other_pools(self):
        from bridge_pool import BackendPool
        async def run():
            pools={key:BackendPool(key,MagicMock(),4) for key in ('wechat','wetype','doubao')}
            release=asyncio.Event()
            async def hold(pool):
                async with pool.admit():await release.wait()
            async def wait_for(predicate):
                for _ in range(300):
                    if predicate():return
                    await asyncio.sleep(.005)
                self.fail('pool wait timed out')
            tasks=[asyncio.create_task(hold(pools['wechat'])) for _ in range(5)]
            try:
                await wait_for(lambda:len(pools['wechat'].occupied)==4 and pools['wechat'].waiting==1)
                tasks.extend(asyncio.create_task(hold(pools[key])) for key in ('wetype','doubao') for _ in range(4))
                await wait_for(lambda:sum(len(p.occupied) for p in pools.values())==12)
                waiter=tasks[4]
                waiter.cancel()
                await asyncio.gather(waiter,return_exceptions=True)
                self.assertEqual(pools['wechat'].waiting,0)
                self.assertEqual(len(pools['wechat'].occupied),4)
                self.assertEqual(len(pools['wetype'].occupied),4)
                self.assertEqual(len(pools['doubao'].occupied),4)
            finally:
                release.set()
                await asyncio.gather(*tasks,return_exceptions=True)
            for pool in pools.values():
                self.assertEqual((pool.waiting,pool.occupied),(0,set()))
                self.assertEqual(pool.gate.value,4)
                async with pool.admit():self.assertEqual(len(pool.occupied),1)
        asyncio.run(run())

    def test_single_provider_uses_its_configured_capacity(self):
        for key in ('wechat','wetype','doubao'):
            server.configure_backend(key,dict.fromkeys(('wechat','wetype','doubao'),16))
            self.assertEqual(server.MAX_CONCURRENCY,16)
            self.assertEqual(self.client.get('/health').json()['max_concurrency'],16)


class SettingsTests(unittest.TestCase):
    def test_auth_switch_preserves_legacy_protection_and_allows_unused_key(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(saved_auth_enabled(directory))
            save_settings(18765,'legacy-key',directory)
            self.assertTrue(saved_auth_enabled(directory))
            save_settings(18765,'arbitrary-unused-key',directory,auth_enabled=False)
            self.assertFalse(saved_auth_enabled(directory))
            self.assertEqual(saved_api_key(directory),'arbitrary-unused-key')
            target=Path(directory)/'desktop-settings.json'
            before=target.read_bytes()
            with self.assertRaises(ValueError):
                save_settings(18765,'',directory,auth_enabled=True)
            self.assertEqual(target.read_bytes(),before)
            save_settings(18765,'real-key',directory,auth_enabled=True)
            self.assertTrue(saved_auth_enabled(directory))

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
