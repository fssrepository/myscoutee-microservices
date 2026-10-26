#!/usr/bin/env python3
"""Read the existing runtime manifest for Python and shell lab entry points.

Project identity lives in deploy/runtime.json, never in mutable host state.
This module reads configuration only; it does not create/adopt lab resources.
"""
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

BACKEND = Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', Path.cwd())).resolve()
SHELL = BACKEND / 'config/microservices'
BUILD = BACKEND / 'server/build/microservices'
SUPPORT = Path(os.environ.get('MYSCOUTEE_MICROSERVICES_SOURCE',
                             Path(__file__).resolve().parents[1])).resolve()
# Product fixtures remain explicit project inputs to the existing runtime tools.
sys.path.append(str(BACKEND / 'tests/e2e/microservices'))


def validate(config):
    rollout_timeout = config.get('rolloutTimeoutSeconds', 90)
    if type(rollout_timeout) is not int or not 30 <= rollout_timeout <= 1800:
        raise ValueError('rolloutTimeoutSeconds must be an integer between 30 and 1800')
    lab = config['lab']
    state = PurePosixPath(lab['stateDirectory'])
    if state.is_absolute() or not state.parts or any(
            not re.fullmatch(r'[a-zA-Z0-9_-]+', part) for part in state.parts):
        raise ValueError('Lab stateDirectory must be a safe relative project path')
    for key in ('vmName', 'monitoringProject'):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', lab[key]):
            raise ValueError('Invalid lab ' + key)
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,14}', lab['bridge']):
        raise ValueError('Invalid lab bridge')
    if not re.fullmatch(r'52:54:00(:[0-9a-f]{2}){3}', lab['macAddress']):
        raise ValueError('Invalid lab MAC address')
    network = ipaddress.ip_network(lab['networkCidr'])
    private_ranges = [ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]  # NOSONAR - python:S1313: RFC1918 validation ranges, not remote endpoints.
    if network.version != 4 or network.prefixlen != 24 or not any(network.subnet_of(value) for value in private_ranges):
        raise ValueError('Lab network must be a private IPv4 /24')
    if type(lab['memoryMiB']) is not int or lab['memoryMiB'] <= 0:
        raise ValueError('Lab memory must be a positive integer in MiB')
    ports = [config['localPort'], lab['grafanaPort'], lab['prometheusPort']]
    if len(set(ports)) != len(ports) or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise ValueError('Lab listeners require distinct unprivileged ports')
    for value in config['databases'].values():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', value):
            raise ValueError('Invalid lab database/replica-set name')
    if len({config['databases'][key] for key in ('real', 'demo', 'bootstrap')}) != 3:
        raise ValueError('REAL, DEMO and bootstrap databases must be distinct')
    images = config['images']
    if not re.fullmatch(r'[a-z][a-z0-9-]*-', images['prefix']):
        raise ValueError('Invalid lab image prefix')
    for key in ('buildTag', 'immutableVersion'):
        if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,100}', images[key]):
            raise ValueError('Invalid lab image tag')
    return config


CONFIG = validate(json.loads((SHELL / 'deploy/runtime.json').read_text()))
LAB = CONFIG['lab']
STATE = Path(os.environ.get('XDG_STATE_HOME', str(BACKEND / '.local/state'))) / LAB['stateDirectory']


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: runtime_config.py state|support-root|<dotted runtime key>')
    roots = {'state': STATE, 'support-root': SUPPORT}
    value = str(roots[sys.argv[1]]) if sys.argv[1] in roots else CONFIG
    if sys.argv[1] not in roots:
        for key in sys.argv[1].split('.'):
            value = value[key]
    print(json.dumps(value) if isinstance(value, (dict, list, bool)) else value)
