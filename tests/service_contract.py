"""Real FastCAS -> FastInsight SDK -> FastResearch service delivery contract."""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

workspace=Path(__file__).resolve().parents[2]
research=workspace/'FastResearch'/'server'/'index.mjs'
publisher=workspace/'FastInsight'/'scripts'/'publish_to_research.py'
issuer=os.environ['FASTCAS_CONTRACT_ISSUER']
origin=os.environ['FASTCAS_CONTRACT_RESEARCH_ORIGIN']
secret=os.environ['FASTCAS_CONTRACT_INSIGHT_SECRET']

with tempfile.TemporaryDirectory(prefix='fastinsight-contract-') as directory:
    root=Path(directory)
    base={**os.environ,'PORT':str(urlparse(origin).port),'HOST':'127.0.0.1',
          'FASTRESEARCH_DATA_DIR':directory,'FASTRESEARCH_ACCOUNT_DATABASE':str(root/'accounts.sqlite'),
          'ADMIN_USERNAME':'admin','ADMIN_PASSWORD':'local-admin-password',
          'FASTRESEARCH_PUBLIC_URL':origin,'FASTINSIGHT_INGEST_KEY':'legacy-ingest-key',
          'FASTRESEARCH_FASTCAS_ALLOW_LOOPBACK_HTTP':'true'}
    def start(extra=None):
        env={**base,**(extra or {})}
        child=subprocess.Popen(['node',str(research)],cwd=directory,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if child.poll() is not None:raise AssertionError('Research startup failed: '+child.stderr.read())
            if select.select([child.stdout],[],[],.2)[0] and 'listening on' in child.stdout.readline():return child
        child.kill();raise AssertionError('Research startup timeout')
    def stop(child):
        child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
    child=start()
    try:
        with httpx.Client(base_url=origin,follow_redirects=False) as app:
            admin=app.post('/api/admin/login',json={'username':'admin','password':'local-admin-password'}).json()['session']
            keys=[app.post('/api/admin/keys',json={'person':'Same Name'},headers={'Authorization':'Bearer '+admin}).json()['key'] for _ in range(2)]
            accounts=[app.post('/api/content/unlock',json={'key':key}).json() for key in keys]
            account_a,account_b=(item['accountId'] for item in accounts)
            assert account_a!=account_b
        stop(child);child=None
        child=start({'FASTRESEARCH_FASTCAS_INGEST_ISSUER':issuer,
                     'FASTRESEARCH_FASTCAS_INGEST_CLIENT_ID':'insight-service',
                     'FASTRESEARCH_FASTCAS_INGEST_ALLOWED_ACCOUNT_IDS':account_a})
        paper=root/'paper.json';paper.write_text(json.dumps({'title':'CAS scoped delivery','doi':'10.1234/cas-scoped'}),encoding='utf-8')
        client_env={**os.environ,'FASTINSIGHT_FASTCAS_ISSUER':issuer,
                    'FASTINSIGHT_FASTCAS_CLIENT_ID':'insight-service',
                    'FASTINSIGHT_FASTCAS_CLIENT_SECRET':secret,
                    'FASTINSIGHT_FASTCAS_ALLOW_LOOPBACK_HTTP':'true','FASTINSIGHT_INGEST_KEY':''}
        def publish(receiver):
            return subprocess.run([sys.executable,str(publisher),'--paper',str(paper),'--endpoint',origin,'--receiver-ref',receiver,'--trend-text','Isolated service authorization contract summary'],env=client_env,capture_output=True,text=True,timeout=20)
        result=publish(account_a)
        assert result.returncode==0,(result.stdout,result.stderr)
        with httpx.Client(base_url=origin,follow_redirects=False) as app:
            def inbox(key):
                session=app.post('/api/content/unlock',json={'key':key}).json()['session']
                return app.get('/api/content/me',headers={'Authorization':'Bearer '+session}).json()['insightItems']
            assert any(item['title']=='CAS scoped delivery' and item['publisherClientId']=='insight-service' for item in inbox(keys[0]))
            assert not any(item['title']=='CAS scoped delivery' for item in inbox(keys[1]))
            denied=publish(account_b)
            assert denied.returncode!=0 and not any(item['title']=='CAS scoped delivery' for item in inbox(keys[1]))
            no_auth=app.post('/api/insight/publish',json={'receiver_ref':account_a,'item':{'title':'No auth'}})
            assert no_auth.status_code==401
            mixed=app.post('/api/insight/publish',json={'receiver_ref':account_a,'item':{'title':'Mixed'}},headers={'Authorization':'Bearer bad','X-FastInsight-Key':'legacy-ingest-key'})
            assert mixed.status_code==400
            legacy=app.post('/api/insight/publish',json={'person':'Same Name','item':{'title':'Legacy delivery'}},headers={'X-FastInsight-Key':'legacy-ingest-key'})
            assert legacy.status_code==201
            assert any(item['title']=='Legacy delivery' for item in inbox(keys[0]))
            assert any(item['title']=='Legacy delivery' for item in inbox(keys[1]))
    finally:
        if child is not None:stop(child)
print('FastInsight service contract passed: scoped recipient, same-name isolation, denied receiver, no fallback and legacy path')
