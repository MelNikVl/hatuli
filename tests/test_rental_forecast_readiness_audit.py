"""The audit can read aggregates only and never creates application pools."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.audit_rental_forecast_readiness import QUERIES, artifact_audit, audit, conclusions


class Transaction:
    async def __aenter__(self):
        pass

    async def __aexit__(self,*args):
        pass


class Connection:
    def __init__(self,readonly):
        self.readonly=readonly
        self.closed=False
        self.reads=[]

    def transaction(self,**options):
        self.options=options
        return Transaction()

    async def fetchval(self,sql):
        return self.readonly if sql.startswith('SHOW') else '2026-10-16T06:00:00Z'

    async def fetch(self,sql):
        self.reads.append(sql)
        return []

    async def close(self):
        self.closed=True


@pytest.mark.asyncio
@pytest.mark.parametrize('readonly',['on','off'])
async def test_readonly_connection_and_transaction_are_mandatory(monkeypatch,tmp_path,readonly):
    conn=Connection(readonly)
    options={}

    async def connect(dsn,**kwargs):
        assert dsn=='postgresql://unused:unused@127.0.0.1:1/placeholder'
        options.update(kwargs)
        return conn

    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    if readonly=='off':
        with pytest.raises(RuntimeError,match='read-only'):
            await audit('postgresql://unused:unused@127.0.0.1:1/placeholder',tmp_path)
        assert conn.reads==[]
    else:
        result=await audit('postgresql://unused:unused@127.0.0.1:1/placeholder',tmp_path)
        assert result['database_writes']==result['source_requests']==0
        assert result['readiness']['source_confirmed_monthly_removals']==0
        assert result['readiness']['monthly_episodes_spanning_7_days']==0
        assert 'missing_validated_rental_artifact' in result['readiness']['reason_codes']
        assert all(sql.strip().startswith(('SELECT','WITH')) for sql in conn.reads)
    assert options['server_settings']['default_transaction_read_only']=='on'
    assert conn.options=={'readonly':True,'isolation':'repeatable_read'}
    assert conn.closed


def test_artifact_metadata_never_exports_per_offer_private_values(tmp_path):
    (tmp_path/'var').mkdir()
    (tmp_path/'var/rental-forecasts.json').write_text(json.dumps({
        'schema_version':1,'reports':[{'target':'future_asking_rent','status':'not_ready',
                                     'private_features':['PRIVATE-ADDRESS']}],
        'offers':{'PRIVATE-ID':{'address':'PRIVATE-ADDRESS','seller_phone':'PRIVATE-PHONE'}},
    }),encoding='utf-8')
    result=artifact_audit(tmp_path)
    assert result['published_offers']==1
    assert 'PRIVATE' not in json.dumps(result)


def test_legacy_changes_and_disappearance_are_never_contract_ground_truth():
    report={key:[] for key in QUERIES}
    report.update(artifact={'present':False},source_observations=[{'rent_period':'monthly','observed_days':1}],
                  monthly_history_spans=[{'span7_episodes':0}],
                  monthly_removal_cohort=[{'source_confirmed_removals':0}])
    result=conclusions(report)
    assert result['legacy_price_history_is_period_verified'] is False
    assert result['asking_price_target']=='future_asking_rent_conditional_on_remaining_listed'
    assert result['removal_target'].endswith('_not_letting')
    assert result['model_validation_recomputed_by_this_audit'] is False
    assert 'no_matured_7_day_asking_price_outcomes' in result['reason_codes']
