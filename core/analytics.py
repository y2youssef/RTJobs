"""Compute v2 market statistics from SQLite; no LLM or network calls.

Times follow the database's local-time convention. Unknown facts stay unknown;
salary bounds are benchmarked separately without currency/period conversions.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
import sqlite3
import statistics

SCHEMA_VERSION = 'job_family_v2'
WINDOWS = {'15m': timedelta(minutes=15), '1h': timedelta(hours=1), '24h': timedelta(days=1),
           '7d': timedelta(days=7), '30d': timedelta(days=30)}


def distribution(counter, fields=('value',)):
    return [{**dict(zip(fields, key if isinstance(key, tuple) else (key,))), 'jobs': count}
            for key, count in sorted(counter.items(), key=lambda pair: (-pair[1], str(pair[0])))]


def percentiles(values):
    """Linear-interpolated percentiles of explicit advertised bounds only."""
    values = sorted(values)
    if not values:
        return {'n': 0, 'median': None, 'p10': None, 'p25': None, 'p75': None, 'p90': None}
    def p(fraction):
        index = (len(values)-1)*fraction
        low = int(index)
        return values[low] + (values[min(low+1,len(values)-1)]-values[low])*(index-low)
    return {'n':len(values), 'median':statistics.median(values), **{f'p{k}':p(k/100) for k in (10,25,75,90)}}


def aggregate(rows, now: datetime) -> dict:
    counters = {name: Counter() for name in ('job_family','specialization','employer_sector','family_sector',
        'seniority','seniority_trend','experience_min','experience_max','work_setup','governorate',
        'tools','certifications','languages','posting_entity_type','posting_weekday_hour')}
    windows = {key:{'raw_jobs':0,'enriched_jobs':0} for key in WINDOWS}
    employers = defaultdict(Counter)
    employer_labels = {}
    salaries = defaultdict(lambda: {'minimum':[], 'maximum':[]})
    disclosure = Counter()
    enriched = review = fallback = unknown_times = 0
    for row in rows:
        try:
            posted = datetime.fromisoformat(row.get('posted_at') or '')
            if posted.tzinfo: raise ValueError('DB timestamps must be local and naive')
        except ValueError:
            unknown_times += 1
            continue
        if not now - WINDOWS['30d'] <= posted <= now:
            continue
        active = [key for key, delta in WINDOWS.items() if now-delta <= posted]
        company = ' '.join((row.get('company') or '').split())
        employer = company.casefold()
        if employer: employer_labels.setdefault(employer, company)
        for key in active:
            windows[key]['raw_jobs'] += 1
            if employer: employers[employer][key] += 1
        counters['posting_weekday_hour'][(posted.strftime('%A'),posted.hour)] += 1
        if row.get('schema_version') != SCHEMA_VERSION or not row.get('result_json'):
            continue
        if row.get('state') == 'fallback':
            fallback += 1
            continue  # Provider failure is not evidence of missing source facts.
        if row.get('state') != 'ready': continue
        result = json.loads(row['result_json'])
        enriched += 1
        for key in active: windows[key]['enriched_jobs'] += 1
        classification = result['classification']; requirements = result['requirements']
        work = result['work_conditions']; compensation = result['compensation']
        family = classification['job_family']; sector = classification['employer_sector']
        counters['job_family'][family] += 1
        counters['specialization'][(family,classification['specialization'])] += 1
        counters['employer_sector'][sector] += 1
        counters['family_sector'][(family,sector)] += 1
        counters['seniority'][result['seniority']['level']] += 1
        counters['seniority_trend'][(posted.date().isoformat(), result['seniority']['level'])] += 1
        counters['experience_min'][requirements['experience_years_min']] += 1
        counters['experience_max'][requirements['experience_years_max']] += 1
        counters['work_setup'][work['work_setup']] += 1
        counters['governorate'][work['governorate']] += 1
        counters['posting_entity_type'][classification['posting_entity_type']] += 1
        review += bool(classification['needs_review'])
        for name, field in (('tools','tools_and_technologies'),('certifications','certifications')):
            counters[name].update({value.casefold() for value in requirements[field]})
        counters['languages'].update({(lang['language'].casefold(),lang['required'],lang['proficiency']) for lang in requirements['languages']})
        disclosed_salary = compensation['salary_min'] is not None or compensation['salary_max'] is not None
        disclosure['salary'] += disclosed_salary
        disclosure['work_setup'] += work['work_setup'] is not None
        disclosure['education'] += requirements['education_level'] is not None
        disclosure['experience'] += requirements['experience_years_min'] is not None or requirements['experience_years_max'] is not None
        if disclosed_salary and compensation['currency'] and compensation['period']:
            group = (compensation['currency'],compensation['period'],compensation['salary_basis'])
            for name,key in (('minimum','salary_min'),('maximum','salary_max')):
                if compensation[key] is not None: salaries[group][name].append(compensation[key])
    fields = {'specialization':('job_family','specialization'), 'family_sector':('job_family','employer_sector'),
              'seniority_trend':('date','level'), 'languages':('language','required','proficiency'),
              'posting_weekday_hour':('weekday','hour')}
    return {'generated_at':now.isoformat(sep=' ',timespec='seconds'),'schema_version':SCHEMA_VERSION,
            'window_days':30, 'windows':windows, 'enriched_jobs':enriched, 'fallback_jobs':fallback,
            'needs_review_jobs':review,'invalid_timestamps':unknown_times,
            'coverage_percent':100*enriched/windows['30d']['raw_jobs'] if windows['30d']['raw_jobs'] else 0,
            'disclosure_rates':{key:{'disclosed':disclosure[key], 'denominator':enriched,
                'percent':100*disclosure[key]/enriched if enriched else None} for key in ('salary','work_setup','education','experience')},
            'distributions':{name:distribution(counter, fields.get(name,('value',))) for name,counter in counters.items()},
            'unique_employers':{key:sum(counts[key]>0 for counts in employers.values()) for key in WINDOWS},
            'employer_hiring_velocity':[{'company':employer_labels[key], **{window:counts[window] for window in WINDOWS}}
                for key,counts in sorted(employers.items(),key=lambda pair:(-pair[1]['24h'],-pair[1]['30d'],pair[0]))],
            'salary_benchmarks':[{'currency':key[0],'period':key[1],'salary_basis':key[2],
                'advertised_minimum':percentiles(value['minimum']),'advertised_maximum':percentiles(value['maximum'])}
                for key,value in sorted(salaries.items(),key=lambda pair:str(pair[0]))],
            'notes':['Time windows use posted_at in database local time; future/unknown dates are excluded.',
                     'Raw counts include all jobs; extraction distributions use only current-schema ready results.',
                     'Rates use enriched jobs as denominator; provider-fallback outputs are excluded.',
                     'Salary bounds are separate; different currencies, periods and net/gross bases are never pooled.',
                     'Employer counts normalize case/whitespace only; staffing-client identities are not resolved.']}


def report_from_db(path: str, now: datetime | None = None) -> dict:
    """Open read-only and stream compact rows; never read raw descriptions."""
    from pathlib import Path
    connection = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory = sqlite3.Row
    now = now or datetime.now()
    try:
        rows = connection.execute('SELECT j.company,j.posted_at,e.state,e.schema_version,e.result_json FROM jobs j '
            'LEFT JOIN job_enrichments e ON e.job_id=j.id WHERE j.posted_at>=? AND j.posted_at<=?',
            ((now-WINDOWS['30d']).strftime('%Y-%m-%d %H:%M'),now.strftime('%Y-%m-%d %H:%M:%S')))
        return aggregate((dict(row) for row in rows),now)
    finally:
        connection.close()
