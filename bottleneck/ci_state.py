"""GitHub Actions: consistent compressed SQLite checkpoints and failed-push recovery."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import urllib.error
import urllib.request
import zipfile

NAMES=('radar.sqlite3','usage.sqlite3')
FORMAT=1

def order(manifest):
    return int(manifest.get('run_id',0)),int(manifest.get('run_attempt',0))

def manifest_at(state):
    file=state/'manifest.json'
    if not file.exists():
        if any(state.glob('*.gz')):raise RuntimeError('State manifest missing')
        return None
    value=json.loads(file.read_text())
    if value.get('format')!=FORMAT:raise RuntimeError('Unsupported state format')
    for name in NAMES:
        blob=state/(name+'.gz')
        if not blob.exists() or hashlib.sha256(blob.read_bytes()).hexdigest()!=value['sha256'].get(name):
            raise RuntimeError('State checkpoint missing or checksum mismatch')
    return value

def restore(state,data):
    manifest=manifest_at(state)
    data.mkdir(parents=True,exist_ok=True)
    if not manifest:
        print('First radar run: no prior radar checkpoint.');return
    # Validate both databases before replacing either file.
    with tempfile.TemporaryDirectory(dir=data) as temp:
        for name in NAMES:
            dest=Path(temp)/name
            with gzip.open(state/(name+'.gz'),'rb') as src,dest.open('wb') as out:
                import shutil
                shutil.copyfileobj(src,out)
            with sqlite3.connect(dest) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('SQLite checkpoint corrupt')
            db.close()
        for name in NAMES:os.replace(Path(temp)/name,data/name)
    print('Radar memory and token ledger restored.')

def snapshot(data,state):
    present=[(data/n).exists() for n in NAMES]
    if not any(present):print('No runtime state to checkpoint.');return
    if not all(present):raise RuntimeError('Incomplete runtime state; refusing to overwrite checkpoint')
    state.mkdir(parents=True,exist_ok=True)
    manifest=dict(format=FORMAT,run_id=os.getenv('GITHUB_RUN_ID','0'),
                  run_attempt=os.getenv('GITHUB_RUN_ATTEMPT','0'),sha256={})
    with tempfile.TemporaryDirectory(dir=state.parent) as temp:
        temp=Path(temp)
        for name in NAMES:
            with sqlite3.connect(data/name) as src,sqlite3.connect(temp/name) as dest:
                src.backup(dest)
                dest.execute('PRAGMA journal_mode=DELETE')
                if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('Snapshot integrity failure')
            src.close();dest.close()
            packed=gzip.compress((temp/name).read_bytes(),compresslevel=6,mtime=0)
            (temp/(name+'.gz')).write_bytes(packed)
            manifest['sha256'][name]=hashlib.sha256(packed).hexdigest()
        (temp/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        for name in [n+'.gz' for n in NAMES]+['manifest.json']:os.replace(temp/name,state/name)
    print('SQLite backups prepared, including committed WAL records.')

def api(path,binary=False):
    repo=os.environ['GITHUB_REPOSITORY']
    request=urllib.request.Request('https://api.github.com/repos/'+repo+'/'+path,
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
                 'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
    # Do not forward GitHub credentials to an artifact-storage redirect.
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            r=super().redirect_request(req,fp,code,msg,headers,newurl)
            if r:r.remove_header('Authorization')
            return r
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request,timeout=60) as r:
            raw=r.read()
        return raw if binary else json.loads(raw)
    except (OSError,ValueError):raise RuntimeError('GitHub state recovery API failed; bot execution blocked') from None

def recover(state):
    current=manifest_at(state)
    artifacts=api('actions/artifacts?name=industry-radar-state&per_page=100').get('artifacts',[])
    branch=os.environ['TARGET_BRANCH']
    for a in sorted(artifacts,key=lambda x:x['id'],reverse=True):
        run=a.get('workflow_run',{})
        if run.get('head_branch')!=branch:continue
        details=api('actions/runs/'+str(run['id']))
        if details.get('path','').split('@')[0]!='.github/workflows/bottleneck.yml':continue
        if details.get('event') not in ('schedule','workflow_dispatch'):continue
        if a.get('expired'):
            if not current or int(run['id'])>order(current)[0]:
                raise RuntimeError('Newer recovery checkpoint expired; refusing silent memory reset')
            return
        blob=api('actions/artifacts/'+str(a['id'])+'/zip',binary=True)
        with tempfile.TemporaryDirectory() as temp,zipfile.ZipFile(io.BytesIO(blob)) as z:
            target=Path(temp)
            # Read only known filenames; never extract arbitrary artifact paths.
            for name in [n+'.gz' for n in NAMES]+['manifest.json']:
                matches=[p for p in z.namelist() if p==name or p.endswith('/'+name)]
                if len(matches)!=1:raise RuntimeError('Invalid recovery archive')
                (target/name).write_bytes(z.read(matches[0]))
            incoming=manifest_at(target)
            if int(incoming['run_id'])!=int(run['id']):raise RuntimeError('Recovery manifest run mismatch')
            if not current or order(incoming)>order(current):
                state.mkdir(parents=True,exist_ok=True)
                for file in target.iterdir():os.replace(file,state/file.name)
                print('Restored newer artifact checkpoint before generation or sending.')
            else:print('Repository checkpoint is current.')
        return
    print('No newer matching recovery artifact.')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['recover','restore','snapshot'])
    parser.add_argument('--state',default='state')
    parser.add_argument('--data',default=os.getenv('RADAR_DATA_DIR','data'))
    a=parser.parse_args()
    try:
        state=Path(a.state);data=Path(a.data)
        if a.action=='recover':recover(state)
        elif a.action=='restore':restore(state,data)
        else:snapshot(data,state)
    except Exception as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'State operation failed')
        raise SystemExit(1)
