"""Rental cards only accept source-proven, personally bound offline projections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core.rental_forecast_runtime import (
    METHOD_VERSION, PRICE_TARGET, REMOVAL_TARGET, _read,
    offer_signature, rental_forecast_for_offer,
)

NOW=datetime(2026,10,16,6,tzinfo=timezone.utc)


def listing():
    return {'id':'unit-test-offer','city':'astana','rent_period':'monthly','price':250000,
            'rooms':2,'area':61.0,'district':'Есильский','complex_id':None,
            'rental_episode_started_at':NOW-timedelta(days=20),'last_seen':NOW,
            'is_active':True,'archived_at':None,'is_duplicate':False,'dup_needs_review':False,
            'duplicate_of':None,'rent_period_conflict':False}


def folds():
    return [{'train_end':'2026-08-01T00:00:00+00:00',
             'calibration_start':'2026-08-09T00:00:00+00:00','calibration_end':'2026-08-12T00:00:00+00:00',
             'test_start':'2026-08-20T00:00:00+00:00','test_end':'2026-08-24T00:00:00+00:00',
             'disjoint_property_groups':True,'future_labels_purged':True,'features_known_at_anchor':True},
            {'train_end':'2026-08-24T00:00:00+00:00',
             'calibration_start':'2026-09-01T00:00:00+00:00','calibration_end':'2026-09-04T00:00:00+00:00',
             'test_start':'2026-09-12T00:00:00+00:00','test_end':'2026-09-16T00:00:00+00:00',
             'disjoint_property_groups':True,'future_labels_purged':True,'features_known_at_anchor':True}]


def artifact():
    common={'status':'ready','folds':folds(),'validation':{
        'independent_properties':250,'sample_size':300,'coverage_pct':81,
        'interval_nominal_coverage_pct':80,'improvement_ci95':[.01,.02]}}
    asking={**deepcopy(common),'target':PRICE_TARGET,'horizon_days':7,'baseline':'unchanged_asking_rent',
            'model_version':'mock-price-model'}
    asking['validation'].update(changed_properties=60,rms_log_improvement_pct=20,
        median_interval_width_pct=20,rms_log_error=.08,baseline_rms_log_error=.1,
        mape_pct=5,baseline_mape_pct=7)
    removal={**deepcopy(common),'target':REMOVAL_TARGET,'baseline':'kaplan_meier_segment',
             'model_version':'mock-removal-model','right_censoring_accounted':True,
             'interval_censoring_accounted':True,'complete_at_risk_population':True,
             'prediction_time_origin':'remaining_from_anchor'}
    removal['validation'].update(integrated_brier_score=.08,baseline_integrated_brier_score=.1,
                                 confirmed_removal_properties=120)
    return {'schema_version':1,'method_version':METHOD_VERSION,'generated_at':NOW.isoformat(),
            'source_history_verified':True,'independent_property_groups_verified':True,
            'reports':[asking,removal],
            'offers':{'unit-test-offer':{'signature':offer_signature(listing()),'as_of':NOW.isoformat(),
                'forecasts':[{'status':'ready','target':PRICE_TARGET,'model_version':'mock-price-model',
                              'horizon_days':7,'estimate':245000,'low':230000,'high':270000,
                              'target_date':'2026-10-23','historical_analog_count':25},
                             {'status':'ready','target':REMOVAL_TARGET,'model_version':'mock-removal-model',
                              'median_days':12,'low_days':4,'high_days':26}]}}}


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    async def reject(*args,**kwargs):
        raise AssertionError('Rental forecast runtime must never access PostgreSQL')
    monkeypatch.setattr('asyncpg.connect',reject)
    monkeypatch.setattr('asyncpg.create_pool',reject)
    _read.cache_clear()
    yield
    _read.cache_clear()


def publish(tmp_path,data):
    path=tmp_path/'rental-forecasts.json'
    path.write_text(json.dumps(data),encoding='utf-8')
    return path


def test_valid_personal_projection_has_only_ready_public_fields(tmp_path):
    result=rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,artifact()))
    assert result['asking_price']=={'horizon_days':7,'estimate':245000,'low':230000,'high':270000,
                                   'as_of_label':'16.10.2026','target_date_label':'23.10.2026'}
    assert result['removal']=={'median_days':12,'low_days':4,'high_days':26,'as_of_label':'16.10.2026'}
    assert 'signature' not in json.dumps(result)
    assert 'mock-price-model' not in json.dumps(result)


def test_missing_model_returns_empty_mapping_and_no_median_fallback(tmp_path):
    assert rental_forecast_for_offer(listing(),now=NOW,path=tmp_path/'absent.json')=={}


@pytest.mark.parametrize('update',[
    {'rent_period':'daily'},{'rent_period':'unknown'},{'rent_period_conflict':True},
    {'is_duplicate':True},{'dup_needs_review':True},{'duplicate_of':'other'},
    {'is_active':False},{'archived_at':NOW},{'city':'almaty'},
    {'price':250001},{'area':62.0},{'rooms':3},{'district':'Сарыаркинский'},
    {'rental_episode_started_at':NOW-timedelta(days=1)},{'complex_id':123},
    {'last_seen':NOW-timedelta(hours=73)},{'last_seen':NOW+timedelta(seconds=1)},
    {'rental_episode_started_at':None},{'area':float('nan')},
])
def test_current_offer_and_episode_must_match_projection(tmp_path,update):
    assert rental_forecast_for_offer({**listing(),**update},now=NOW,path=publish(tmp_path,artifact()))=={}


@pytest.mark.parametrize('update',[
    {'method_version':'sales-historical-neighbours-v2'},{'source_history_verified':False},
    {'independent_property_groups_verified':False},{'schema_version':999},
    {'generated_at':(NOW-timedelta(hours=49)).isoformat()},
    {'generated_at':(NOW+timedelta(seconds=1)).isoformat()},
    {'generated_at':'2026-10-16T06:00:00'},
])
def test_invalid_publication_never_produces_forecast(tmp_path,update):
    assert rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,{**artifact(),**update}))=={}


@pytest.mark.parametrize('target_index,metric,value',[
    (0,'independent_properties',199),(0,'changed_properties',49),
    (0,'coverage_pct',74),(0,'coverage_pct',96),(0,'improvement_ci95',[-.01,.02]),
    (0,'rms_log_improvement_pct',4),(0,'rms_log_error',.2),
    (0,'median_interval_width_pct',41),(0,'mape_pct',float('nan')),
    (1,'confirmed_removal_properties',99),(1,'integrated_brier_score',.099),
    (1,'integrated_brier_score',float('nan')),(1,'sample_size',0),
])
def test_failed_numeric_quality_gate_hides_only_affected_block(tmp_path,target_index,metric,value):
    data=artifact()
    data['reports'][target_index]['validation'][metric]=value
    result=rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,data))
    assert ('asking_price' if target_index==0 else 'removal') not in result
    assert ('removal' if target_index==0 else 'asking_price') in result


@pytest.mark.parametrize('field,value',[
    ('right_censoring_accounted',False),('complete_at_risk_population',False),
    ('interval_censoring_accounted',False),('prediction_time_origin','since_first_seen'),
    ('baseline','mean_removed_duration'),('model_version',None),
])
def test_uncensored_or_total_exposure_estimate_cannot_be_residual_prediction(tmp_path,field,value):
    data=artifact()
    data['reports'][1][field]=value
    assert 'removal' not in rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,data))


@pytest.mark.parametrize('mutation',[
    'one_fold','random_split','label_leak','feature_leak','overlapping_test','future_validation',
])
def test_temporal_group_validation_is_mandatory(tmp_path,mutation):
    data=artifact()
    folds=data['reports'][0]['folds']
    if mutation=='one_fold':
        folds.pop()
    elif mutation=='overlapping_test':
        folds[1].update(train_end='2026-07-01T00:00:00Z',calibration_start='2026-07-03T00:00:00Z',
                        calibration_end='2026-07-04T00:00:00Z',test_start='2026-08-22T00:00:00Z')
    elif mutation=='future_validation':
        folds[1]['test_end']=(NOW+timedelta(days=1)).isoformat()
    else:
        key={'random_split':'disjoint_property_groups','label_leak':'future_labels_purged',
             'feature_leak':'features_known_at_anchor'}[mutation]
        folds[0][key]=False
    assert 'asking_price' not in rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,data))


def test_removal_prediction_counts_remaining_days_from_today(tmp_path):
    result=rental_forecast_for_offer({**listing(),'last_seen':NOW+timedelta(days=1)},
        now=NOW+timedelta(days=1),path=publish(tmp_path,artifact()))
    assert result['removal']['median_days']==11
    assert result['removal']['low_days']==3


@pytest.mark.parametrize('index,field,value',[
    (0,'target_date','2026-10-24'),(0,'low',260000),(0,'historical_analog_count',19),
    (0,'estimate',True),(0,'high',float('inf')),
    (1,'median_days',-1),(1,'high_days',8),(1,'low_days',False),
])
def test_malformed_predictions_are_omitted(tmp_path,index,field,value):
    data=artifact()
    data['offers']['unit-test-offer']['forecasts'][index][field]=value
    result=rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,data))
    assert ('asking_price' if index==0 else 'removal') not in result


@pytest.mark.parametrize('data',[None,[],{}, {'offers':[]}, {'schema_version':1,'reports':[]}])
def test_malformed_artifact_shapes_are_safe(tmp_path,data):
    assert rental_forecast_for_offer(listing(),now=NOW,path=publish(tmp_path,data))=={}
