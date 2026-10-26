"""Reuse the installed, lock-verified provider without registry discovery."""
import json
import os
from pathlib import Path
import re
import shutil
import sys


def configure(support, state):
    lock=(support/'infra/terraform/.terraform.lock.hcl').read_text()
    version=re.search(r'provider "registry\.terraform\.io/dmacvicar/libvirt"\s*\{\s*version\s*=\s*"([0-9.]+)"',lock).group(1)
    relative=Path('registry.terraform.io/dmacvicar/libvirt')/version/'linux_amd64'
    installed=state/'terraform/providers/providers'/relative
    mirror=state/'tools/provider-mirror'
    target=mirror/relative
    if not target.exists() and installed.is_dir():
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(installed,target)
    if not target.is_dir() and os.environ.get('MYSCOUTEE_INFRA_OFFLINE')=='true':
        raise RuntimeError('Pinned Terraform provider is missing. Run the optional manual infrastructure preparation job.')
    config=(support/'infra/terraform.tfrc').read_text()
    if target.is_dir():
        # Terraform still checks the committed h1 checksum. Do not use a dev
        # override, which would bypass provider identity verification.
        config+='\nprovider_installation {\n  filesystem_mirror {\n    path = '+json.dumps(str(mirror))+'\n    include = ["registry.terraform.io/dmacvicar/libvirt"]\n  }\n  direct {\n    exclude = ["registry.terraform.io/dmacvicar/libvirt"]\n  }\n}\n'
    output=state/'terraform/cli.tfrc'
    output.write_text(config)
    return output


if __name__=='__main__':
    print(configure(Path(sys.argv[1]),Path(sys.argv[2])))
