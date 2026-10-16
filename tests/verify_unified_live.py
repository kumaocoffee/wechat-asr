"""Opt-in real ASR of repository-synthesized audio, with an owned service."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sys
import time
import unicodedata
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from desktop_service import ServiceController, health


def letters(text):
    return ''.join(c for c in text if unicodedata.category(c)[0] in ('L','N'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path)
    parser.add_argument('--models',nargs='+',default=['wetype-voice','doubao-voice'])
    parser.add_argument('--output',type=Path,default=ROOT/'.runtime'/'unified-live.json')
    args=parser.parse_args()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    case=ROOT/'.runtime'/('unified-live-'+uuid.uuid4().hex)
    previous=os.environ.get('LOCALAPPDATA')
    if args.executable:
        os.environ['LOCALAPPDATA']=str(case)
        runtime=case/'WeChatASRBridge'/'runtime'
    else:
        runtime=case
    controller=ServiceController(port,runtime=runtime,executable=args.executable)
    report={'passed':False,'frozen_service':bool(args.executable),'requests':[],
            'peak_active':{'wechat':0,'wetype':0,'doubao':0},'scope':'repository synthesized test.wav only'}
    started=False
    try:
        controller.start(api_key='unified-live-test-key',concurrency={'wechat':4,'wetype':2,'doubao':2})
        started=True
        with httpx.Client(timeout=90,trust_env=False,headers={'Authorization':'Bearer unified-live-test-key'}) as client:
            url=f'http://127.0.0.1:{port}'
            report['models']=[m['id'] for m in client.get(url+'/v1/models').json()['data']]
            docs=client.get(url+'/docs')
            report['documentation']=docs.status_code==200 and 'response_format' in docs.text
            fixture=ROOT/'tests'/'fixtures'/'test.wav'
            def request(model):
                before=time.monotonic()
                with fixture.open('rb') as audio:
                    response=client.post(url+'/v1/audio/transcriptions',data={'model':model},files={'file':('test.wav',audio,'audio/wav')})
                data=response.json()
                text=data.get('text','')
                return {'model':model,'status':response.status_code,'text':text,
                        'elapsed':round(time.monotonic()-before,2),
                        'backend':response.headers.get('x-asr-backend'),
                        'completion':response.headers.get('x-asr-completion'),
                        'matches_fixture':letters(text)==letters('你好，这是微信语音接口测试，今天阳光很好'),
                        'error':data.get('error')}
            with ThreadPoolExecutor(max_workers=len(args.models)*2) as executor:
                futures=[executor.submit(request,model) for model in args.models for _ in range(2)]
                while not all(future.done() for future in futures):
                    state=health(port)
                    if state:
                        for pool in state['pools']:
                            report['peak_active'][pool['provider']]=max(report['peak_active'][pool['provider']],pool['active_requests'])
                    time.sleep(.1)
                report['requests']=[future.result() for future in futures]
            report['idle_after']=all(p['active_requests']==0 and p['queued_requests']==0 for p in health(port)['pools'])
            report['passed']=report['documentation'] and report['idle_after'] and all(r['status']==200 and r['matches_fixture'] for r in report['requests'])
    finally:
        if started:
            controller.stop()
            report['service_stopped']=health(port) is None
        if previous is None:
            os.environ.pop('LOCALAPPDATA',None)
        else:
            os.environ['LOCALAPPDATA']=previous
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] and report.get('service_stopped') else 1


if __name__=='__main__':raise SystemExit(main())
