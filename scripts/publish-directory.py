#!/usr/bin/env python3
"""Build the public directory from public observations, never an operator's cache."""

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def public_domain(value):
    value=value.strip()
    parsed=urlsplit(value if '://' in value else 'https://'+value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Use a public domain without a path, credentials, query, or port.')
    host=parsed.hostname.encode('idna').decode('ascii').lower()
    if '.' not in host or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host):
        raise ValueError('Use a public domain name.')
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Private or reserved network targets are excluded from public checks.')
    return host


def main():
    source=ROOT/'directory'
    targets=json.loads((source/'targets.json').read_text())
    if not isinstance(targets,list) or len(targets)>100 or not all(isinstance(item,str) for item in targets):
        raise ValueError('The public target list must contain at most100 domains.')
    requested=os.environ.get('CHECK_DOMAIN','').strip()
    learned=json.loads((source/'sitemaps.json').read_text())
    if not isinstance(learned,dict):raise ValueError('Invalid public sitemap history.')
    if requested:
        host=public_domain(requested)
        if host not in targets:
            if len(targets)>=100:raise ValueError('The public target list is limited to100 domains.')
            targets.append(host)
        selected=[host]
    elif os.environ.get('RECHECK_DIRECTORY')=='true':
        selected=targets
    else:
        selected=[]
    with tempfile.TemporaryDirectory(prefix='public-directory-') as state:
        # Only committed public records are imported; desktop caches are never read.
        os.environ['XDG_DATA_HOME']=state+'/data'
        os.environ['XDG_CACHE_HOME']=state+'/cache'
        from thd_sources.store import data_root
        from thd_sources.directory import export,observe
        from thd_sources.sitemap import discover
        from thd_sources.store import now
        records=data_root()/'records';records.mkdir(parents=True,mode=0o700)
        for path in (source/'records').glob('*.json'):
            if re.fullmatch(r'[a-f0-9]{64}\.json',path.name):shutil.copyfile(path,records/path.name)
        for target in selected:
            try:
                host=public_domain(target)
                prior=learned.get(host,{})
                urls=prior.get('urls',[]) if isinstance(prior,dict) else []
                if not isinstance(urls,list):urls=[]
                result=discover('https://'+host,[url for url in urls if isinstance(url,str)])
                hidden=bool(prior.get('unrecheckable',False)) if isinstance(prior,dict) else False
                if hidden and result['missing_sitemap']:
                    result['missing_sitemap']=False
                    result['warnings'].append('A previously readable query-bearing sitemap cannot be rechecked from the sanitized public history.')
                result.update(checked_at=now(),cached=False)
                observe(result)
                safe=set(urls)
                for url in result['sitemaps']:
                    parsed=urlsplit(url)
                    if parsed.query or parsed.fragment or parsed.username or parsed.password:
                        hidden=True
                    else:
                        safe.add(url)
                learned[host]={'urls':sorted(safe)[:16],'unrecheckable':hidden}
            except (ValueError,OSError):
                print('A requested domain could not be classified; no failure rating was added.')
        result=export()
        destination=ROOT/'site';destination.mkdir(exist_ok=True)
        shutil.copyfile(result['html'],destination/'index.html')
        shutil.copyfile(result['json'],destination/'index.json')
        (source/'records').mkdir(exist_ok=True)
        for path in records.glob('*.json'):shutil.copyfile(path,source/'records'/path.name)
    (source/'targets.json').write_text(json.dumps(sorted(set(targets)),indent=2)+'\n')
    (source/'sitemaps.json').write_text(json.dumps(learned,indent=2)+'\n')
    domain=os.environ.get('PAGES_DOMAIN','').strip()
    if domain:
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*[a-z0-9]',domain):raise ValueError('Invalid Pages hostname.')
        (ROOT/'site/CNAME').write_text(domain+'\n')
    print('Built public directory from dated domain observations.')


if __name__=='__main__':
    main()
