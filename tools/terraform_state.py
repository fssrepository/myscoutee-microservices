"""Select the existing local state or an explicitly configured GitLab HTTP backend.

Remote ownership is always read from the backend, never from a stale local copy.
Credentials are supplied through Terraform's HTTP environment variables.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request


def remote():
    mode=os.environ.get('MYSCOUTEE_TERRAFORM_BACKEND','local')
    if mode not in ('local','gitlab'): raise ValueError('Unknown Terraform backend')
    if mode=='gitlab':
        for key in ('TF_HTTP_ADDRESS','TF_HTTP_USERNAME','TF_HTTP_PASSWORD'):
            if not os.environ.get(key): raise ValueError('Missing '+key)
    return mode=='gitlab'


def read_state(local):
    if not remote():
        return json.loads(local.read_text()) if local.exists() else {}
    if local.exists():
        raise RuntimeError('Local Terraform state exists; migrate it explicitly before selecting GitLab')
    authorization=base64.b64encode((os.environ['TF_HTTP_USERNAME']+':'+os.environ['TF_HTTP_PASSWORD']).encode()).decode()
    request=urllib.request.Request(os.environ['TF_HTTP_ADDRESS'],headers={'Authorization':'Basic '+authorization})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            # GitLab creates the state record on the first lock, before any
            # version is written. Its read endpoint then returns 204, not 404.
            if response.status == 204: return {}
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code==404: return {}
        raise RuntimeError('Cannot read authoritative Terraform state: HTTP '+str(error.code)) from None


def module(support,state):
    source=Path(os.environ.get('MYSCOUTEE_TERRAFORM_MODULE_ROOT',support/'infra'))
    original=source/'terraform'
    if not remote(): return original
    read_state(state/'vm/terraform.tfstate')
    # Preserve relative guest/versions inputs in a private generated copy. The
    # checked-in module and local operators' provider metadata stay untouched.
    generated=state/'terraform/http-module'
    if generated.is_symlink(): raise RuntimeError('Symlinked Terraform module')
    generated.mkdir(parents=True,exist_ok=True,mode=0o700)
    for child in source.iterdir():
        if child.name not in ('terraform','guest','versions.json'): continue
        target=generated/child.name
        if child.is_dir(): shutil.copytree(child,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('.terraform','*.tfstate*'))
        else: shutil.copyfile(child,target)
    target=generated/'terraform'
    (target/'backend_override.tf').write_text('terraform {\n  backend "http" {}\n}\n')
    return target


if __name__=='__main__':
    from runtime_config import SUPPORT,STATE
    if sys.argv[1:]!=['module']: raise SystemExit('Usage: terraform_state.py module')
    print(module(SUPPORT,STATE))
