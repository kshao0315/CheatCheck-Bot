"""Stable numbered account slots and shared performance settings."""
import os
import re
from dataclasses import dataclass


def account_config_keys(environ=None):
    """Append numbered slots; blank future placeholders don't activate a session."""
    env=os.environ if environ is None else environ
    candidates=sorted({int(match.group(1)) for key in env
                       if (match:=re.fullmatch(r'ACCOUNT_(?:JSON|SESSION)_(\d+)',key))})
    indexes=[]
    for index in candidates:
        if index<2:
            raise ValueError('Additional account indexes must start at 2')
        values=[env.get(f'ACCOUNT_{kind}_{index}','').strip() for kind in ('JSON','SESSION')]
        if not any(values):
            continue
        if not all(values):
            raise ValueError(f'Account {index} needs both JSON and SESSION settings')
        indexes.append(index)
    if indexes!=list(range(2,len(indexes)+2)):
        raise ValueError('Account indexes must be consecutive; keep existing indexes stable')
    return [('ACCOUNT_JSON','ACCOUNT_SESSION')]+[(f'ACCOUNT_JSON_{n}',f'ACCOUNT_SESSION_{n}') for n in indexes]


@dataclass(frozen=True)
class RuntimeSettings:
    rpc_concurrency:int=4
    member_concurrency:int=2
    workers_per_account:int=4
    bulk_worker_limit:int=32
    source_worker_limit:int=32
    partial_cache_seconds:int=20
    query_cache_entries:int=4096

    @classmethod
    def from_env(cls,environ=None):
        env=os.environ if environ is None else environ
        fields={
            'rpc_concurrency':('ACCOUNT_RPC_CONCURRENCY',4,1,64),
            'member_concurrency':('ACCOUNT_MEMBER_CONCURRENCY',2,1,64),
            'workers_per_account':('BULK_WORKERS_PER_ACCOUNT',4,1,64),
            'bulk_worker_limit':('BULK_WORKER_LIMIT',32,1,256),
            'source_worker_limit':('SOURCE_WORKER_LIMIT',32,1,256),
            'partial_cache_seconds':('QUERY_PARTIAL_CACHE_SECONDS',20,1,300),
            'query_cache_entries':('QUERY_CACHE_ENTRIES',4096,1,100000),
        }
        result={}
        for field,(key,default,minimum,maximum) in fields.items():
            try:value=int(env.get(key,'') or default)
            except (TypeError,ValueError):raise ValueError(f'{key} must be an integer') from None
            if not minimum<=value<=maximum:
                raise ValueError(f'{key} must be between {minimum} and {maximum}')
            result[field]=value
        return cls(**result)

    def bulk_workers(self,accounts):
        return min(self.bulk_worker_limit,max(8,accounts*self.workers_per_account))

    def source_workers(self,accounts):
        return min(self.source_worker_limit,max(6,accounts*self.member_concurrency))
