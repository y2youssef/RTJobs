"""Offline v2 classifier integration checks, called by verify_offline.py."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch


def verify_classifier(client, channels):
    from core import db, telegram
    from core import classify
    from core.enrichment_worker import process_job
    from scripts.check_channels import audit_channels

    def rejected(result):
        try:
            client.validate(result, 99)
        except ValueError:
            return
        raise AssertionError('Invalid result accepted')

    result = client.fallback(99)
    assert result['classification']['needs_review'] and result['classification']['routing_confidence'] == 'Low'
    assert result['compensation']['commission_explicit'] is False
    assert result['work_conditions']['travel_required'] is None
    result['classification'].update(job_family='accounting_finance_banking', specialization='general_accounting',
                                    routing_confidence='High', needs_review=False, employer_sector='technology_telecom')
    result['seniority']['level'] = 'Senior'
    result['requirements'].update(experience_years_min=2, experience_years_max=4)
    before = copy.deepcopy(result)
    client.validate(result, 99)
    assert result == before and result['seniority']['level'] == 'Senior'
    assert telegram.destination_for({'job_family': 'accounting_finance_banking', 'employer_sector': 'technology_telecom'}, channels) == channels['accounting_finance_banking']
    result['requirements'].update(experience_years_min=0.5, experience_years_max=1.5)
    client.validate(result, 99)
    for change in ('specialization', 'confidence', 'review', 'skills', 'salary', 'age', 'extra'):
        invalid = copy.deepcopy(result)
        if change == 'specialization': invalid['classification']['specialization'] = 'backend'
        elif change == 'confidence': invalid['classification'].update(job_family='other', specialization='general', routing_confidence='High')
        elif change == 'review': invalid['classification'].update(routing_confidence='Low', needs_review=False)
        elif change == 'skills': invalid['requirements']['tools_and_technologies'] = ['negotiation']
        elif change == 'salary': invalid['compensation'].update(salary_min=200, salary_max=100)
        elif change == 'age': invalid['explicit_candidate_constraints'].update(age_min=40, age_max=30)
        elif change == 'extra': invalid['requirements']['hard_skills'] = []
        rejected(invalid)
    assert client.validate_batch({'jobs': [result]}, [99]) == [result]
    for batch in ({'jobs': []}, {'jobs': [result, result]}, {'jobs': [dict(result, job_id=98)]}):
        try: client.validate_batch(batch, [99])
        except ValueError: pass
        else: raise AssertionError('Batch identity failure accepted')
    job = {'id':99, 'source':'indeed', 'title':'Senior Accountant', 'company':'Example', 'description':'2-4 years',
           'extra':json.dumps({'snippet':'SAP required', 'location':'Maadi', 'hiring_manager_name':'Ignored'})}
    data, digest = client.prepare(job)
    assert data['extra']['snippet'] == 'SAP required' and 'hiring_manager_name' not in data['extra']
    payload = client.payload(data)
    assert json.loads(payload['messages'][1]['content']) == {'jobs':[data]}
    assert client.prepare(dict(job, id=100))[1] == digest
    assert client.prepare(dict(job, extra={'snippet':'Excel required'}))[1] != digest
    old_version = client.version
    client.version = 'different-contract'
    assert client.prepare(job)[1] != digest
    client.version = old_version
    # Exercise the real HTTP response decoder (transport only is mocked).
    response = SimpleNamespace(ok=True, json=lambda: {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'jobs':[result]})}}], 'usage':{'cost':0.001}})
    with patch.object(client.session, 'post', return_value=response):
        parsed, usage = client.classify(data)
        assert parsed['job_id'] == 99 and usage['cost'] == 0.001

    # A disabled delivery switch prevents even a transport call or acknowledgement.
    pending = db.get_unnotified(limit=100)
    with patch.object(telegram, 'CLASSIFIED_DELIVERY_ENABLED', False), patch.object(telegram, '_send') as send:
        assert telegram.notify_jobs(pending) == 0 and not send.called

    # Simulate a historical industry result on the existing schema and migrate
    # again: raw jobs survive, stale results remain auditable and undeliverable.
    raw = {'source':'test','external_id':'legacy-industry','title':'Old', 'description':'Original raw evidence', 'extra':{}}
    db.save_jobs([raw], enqueue=True)
    with db.get_db() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE source='test' AND external_id='legacy-industry'").fetchone()
        old_id = row[0]
        conn.execute("UPDATE job_enrichments SET state='ready',result_json=?,schema_version='' WHERE job_id=?", (json.dumps({'classification':{'industry':'technology_it_telecommunications'}}),old_id))
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute('SELECT state FROM job_enrichments WHERE job_id=?',(old_id,)).fetchone()[0] == 'obsolete'
        assert conn.execute('SELECT description FROM jobs WHERE id=?',(old_id,)).fetchone()[0] == 'Original raw evidence'
    assert all(row['id'] != old_id for row in db.get_unnotified(limit=100))

    families = {'software_engineering':'Software Engineering','other':'Other / Unclassified'}
    destinations = {'software_engineering':'-1001','other':'-1002'}
    def api(method,payload):
        if method == 'getMe':return {'ok':True,'result':{'id':1,'username':'test'}}
        if method == 'getChat':return {'ok':True,'result':{'id':int(payload['chat_id']),'type':'channel',
            'title':'RTJobs - '+('Software Engineering' if payload['chat_id']=='-1001' else 'Other / Unclassified')+' 🇪🇬'}}
        if method == 'getChatMember':return {'ok':True,'result':{'status':'administrator','can_post_messages':True}}
        raise AssertionError('Channel checker attempted a non-read-only method')
    assert audit_channels(destinations,families,api)['ready']
    assert not audit_channels({'software_engineering':'-1002','other':'-1001'},families,api)['ready']
    assert not audit_channels({'software_engineering':'-1001'},families,api)['ready']
    def no_post(method,payload):
        if method == 'getChatMember':return {'ok':True,'result':{'status':'administrator','can_post_messages':False}}
        return api(method,payload)
    assert not audit_channels(destinations,families,no_post)['ready']
    # Delivery startup must reject incomplete or duplicated destinations, even
    # if an operator skips the standalone Telegram audit.
    assert classify.load_channels(require_complete=True) == channels
    invalid_maps = [dict(channels), {}]
    invalid_maps[0].pop('general_management')
    duplicate_map = dict(channels, general_management=channels['other'])
    invalid_maps.append(duplicate_map)
    for invalid_map in invalid_maps:
        with patch.object(classify, 'TELEGRAM_CHANNELS_JSON', json.dumps(invalid_map)):
            try: classify.load_channels(require_complete=True)
            except ValueError: pass
            else: raise AssertionError('Unsafe channel map accepted for delivery')
    print('PASS v2 family/sector independence, seniority, sparse fields, API batches, cache isolation, migration, delivery-off and channel identity')
