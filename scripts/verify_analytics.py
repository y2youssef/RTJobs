"""Offline analytics checks: separate axes, denominators, periods and currencies."""
import copy,json
from datetime import datetime
from core.analytics import aggregate


def verify_analytics(client):
    first=client.fallback(1)
    first['classification'].update(job_family='accounting_finance_banking',specialization='general_accounting',routing_confidence='High',needs_review=False,employer_sector='technology_telecom')
    first['compensation'].update(salary_min=10000,salary_max=12000,currency='EGP',period='Per Month',salary_basis='Net')
    first['requirements'].update(tools_and_technologies=['Excel','Excel'],certifications=['CPA'])
    second=copy.deepcopy(first)
    second['job_id']=2
    second['classification'].update(job_family='software_engineering',specialization='backend',employer_sector='banking_finance_insurance')
    second['compensation'].update(salary_min=500,salary_max=1000,currency='USD',period='Per Year',salary_basis='Gross')
    second['requirements']['tools_and_technologies']=['Python']
    def row(result,state='ready',schema='job_family_v2',posted='2026-10-04 11:55:00'):
        return {'company':'Example','posted_at':posted,'state':state,'schema_version':schema,'result_json':json.dumps(result)}
    report=aggregate([row(first),row(second,posted='2026-10-03 12:30:00'),row(first,state='fallback'),row(first,schema='industry_v1')],datetime(2026,10,4,12))
    assert report['enriched_jobs']==2 and report['fallback_jobs']==1
    assert report['windows']['15m']=={'raw_jobs':3,'enriched_jobs':1}
    assert report['windows']['24h']=={'raw_jobs':4,'enriched_jobs':2}
    assert report['coverage_percent']==50
    matrix=report['distributions']['family_sector']
    assert {(r['job_family'],r['employer_sector']) for r in matrix}=={('accounting_finance_banking','technology_telecom'),('software_engineering','banking_finance_insurance')}
    assert len(report['salary_benchmarks'])==2
    assert report['salary_benchmarks'][0]['advertised_minimum']['median']==10000
    assert report['distributions']['tools']==[{'value':'excel','jobs':1},{'value':'python','jobs':1}]
    assert report['unique_employers']['30d']==1
    assert report['disclosure_rates']['salary']=={'disclosed':2,'denominator':2,'percent':100}
    assert report['disclosure_rates']['work_setup']['percent']==0
    empty=aggregate([],datetime(2026,10,4,12))
    assert empty['coverage_percent']==0 and empty['disclosure_rates']['salary']['percent'] is None
    print('PASS SQLite analytics: independent axes, windows, coverage, distinct tools and comparable salary bounds')
