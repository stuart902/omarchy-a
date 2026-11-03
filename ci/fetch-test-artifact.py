#!/usr/bin/env python3
"""Fetch one exact successful test artifact; no release or device operations."""
import argparse, hashlib, json, os, pathlib, subprocess, tempfile, urllib.request, zipfile
p = argparse.ArgumentParser()
for name in ('repo', 'run', 'commit', 'artifact', 'sha256', 'output'):
    p.add_argument('--' + name, required=True)
a = p.parse_args()
api = 'https://api.github.com/repos/' + a.repo
headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'}
def get(path):
    with urllib.request.urlopen(urllib.request.Request(api + path, headers=headers)) as r:
        return json.load(r)
run = get('/actions/runs/' + a.run)
assert run['head_sha'] == a.commit and run['conclusion'] == 'success'
meta = get('/actions/artifacts/' + a.artifact)
assert not meta['expired'] and meta['workflow_run']['id'] == int(a.run)
assert meta['digest'] == 'sha256:' + a.sha256
out = pathlib.Path(a.output)
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    archive = pathlib.Path(tmp) / 'artifact.zip'
    subprocess.run(['curl', '--fail', '--location', '--retry', '2', '--silent', '--show-error',
        '-H', 'Authorization: Bearer ' + os.environ['GH_TOKEN'],
        api + '/actions/artifacts/' + a.artifact + '/zip', '-o', str(archive)], check=True)
    assert hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() == a.sha256
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            path = pathlib.PurePosixPath(item.filename)
            assert not path.is_absolute() and '..' not in path.parts
            assert (item.external_attr >> 16) & 0o170000 != 0o120000
        z.extractall(out)
print('Verified exact artifact', a.repo, a.run, a.artifact)
