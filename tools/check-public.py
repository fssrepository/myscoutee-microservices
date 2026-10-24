#!/usr/bin/env python3
"""Read-only public ingress and authorization smoke checks for the owned lab."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import workloads as w


def get(path, headers=None):
    request = urllib.request.Request('http://127.0.0.1:' + str(w.CONFIG['localPort']) + path,
                                     headers={'Accept': 'application/json', **(headers or {})})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), response.read(4 * 1024 * 1024)


def main():
    w.namespace_owned()
    config = json.loads((w.SHELL / 'checks/public.json').read_text())
    cases = config['cases']
    results = []
    for path, expected in cases:
        try:
            status, headers, body = get(path)
            valid = status == expected
            if expected == 200:
                valid = valid and 'application/json' in headers.get('Content-Type', '')
                payload = json.loads(body)
                valid = valid and isinstance(payload, (dict, list))
            results.append({'path': path, 'expectedStatus': expected, 'actualStatus': status, 'passed': valid})
            print(('PASS' if valid else 'FAIL') + ': ' + path + ' HTTP ' + str(status), flush=True)
        except (OSError, ValueError) as error:
            results.append({'path': path, 'expectedStatus': expected, 'passed': False, 'error': type(error).__name__})
            print('FAIL: ' + path + ' ' + type(error).__name__, flush=True)
    # A 200 with an empty catalog is not a working landing page. Check the
    # canonical production rows and the image bytes served by this frontend.
    if config['landingCatalog']:
        source = Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', w.BACKEND))
        catalog = json.loads((source / config['landingCatalog']).read_text())
        images = set()
        for group in sorted({row.get('workspaceGroupId') or '' for row in catalog}):
            path = '/api/landing/content?' + urllib.parse.urlencode({'lang': 'en', 'groupId': group})
            status, _, body = get(path)
            slides = json.loads(body).get('slides', []) if status == 200 else []
            expected = [row for row in catalog if (row.get('workspaceGroupId') or '') == group]
            fields = ('id', 'imageUrl', 'titleKey', 'messageKey', 'sliceX', 'sliceY')
            project = lambda rows: sorted([tuple(row.get(key) for key in fields) for row in rows])
            valid = bool(expected) and project(slides) == project(expected)
            results.append({'path': path, 'passed': valid, 'slides': len(slides), 'expectedSlides': len(expected)})
            print(('PASS' if valid else 'FAIL') + ': canonical landing catalog ' + (group or 'dating'), flush=True)
            images.update(row['imageUrl'] for row in expected)
        for asset in sorted(images):
            if asset.startswith('/') or '..' in asset.split('/') or urllib.parse.urlsplit(asset).scheme:
                raise RuntimeError('Unexpected canonical landing asset path')
            status, headers, body = get('/' + asset)
            png = asset.endswith('.png') and headers.get('Content-Type', '').startswith('image/png') and body.startswith(b'\x89PNG\r\n\x1a\n')
            webp = asset.endswith('.webp') and headers.get('Content-Type', '').startswith('image/webp') and body[:4] == b'RIFF' and body[8:12] == b'WEBP'
            valid = status == 200 and (png or webp)
            results.append({'path': '/' + asset, 'status': status, 'passed': valid})
            print(('PASS' if valid else 'FAIL') + ': landing image ' + asset, flush=True)
    path = w.BUILD / 'reports/public-smoke.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2) + '\n')
    return 0 if all(item['passed'] for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
