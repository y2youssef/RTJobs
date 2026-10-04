"""Run opt-in paid v2 classifier checks in scratch SQLite, never Telegram.

.venv/bin/python scripts/evaluate_classifier.py --live --output /tmp/classifier-eval.json
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='Authorize paid OpenRouter calls for these fixtures')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--case', action='append', dest='case_names', help='Run only selected fixture names')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rtjobs-classifier-eval-') as directory:
        os.environ.update(DATA_DIR=directory, MARKUP_DIR=str(ROOT/'markup'), ENRICHMENT_ENABLED='false',
                          CLASSIFIED_DELIVERY_ENABLED='false', LOG_FILE='')
        from core import db, telegram
        from core.classify import Enricher
        from core.enrichment_worker import process_batch
        def forbidden(*a,**kw):raise AssertionError('Telegram forbidden in classifier evaluation')
        telegram._send = telegram._api = forbidden
        db.init_db()
        client = Enricher()
        cases = json.loads((ROOT/'markup/enrichment/evaluation.json').read_text())
        if args.case_names:
            known = {case['name'] for case in cases}
            if set(args.case_names) - known:
                parser.error('Unknown fixture name')
            cases = [case for case in cases if case['name'] in args.case_names]
        if args.limit: cases = cases[:args.limit]
        report = {'version':client.version, 'schema_version':client.taxonomy['schema_version'], 'cases':[], 'cost_usd':0}
        try:
            batch = process_batch(client, [case['job'] for case in cases], preview=True)
            report.update(usage=batch['usage'], api_calls=batch['api_calls'], cost_usd=batch['usage'].get('cost') or 0)
            for case, row in zip(cases, batch['jobs'], strict=True):
                failures = []
                for path, expected in case['expected'].items():
                    actual = row['result']
                    for key in path.split('.'):
                        actual = actual.get(key) if isinstance(actual, dict) else None
                    if actual != expected:failures.append({'path':path,'expected':expected,'actual':actual})
                if row['error']:failures.append({'error':row['error']})
                report['cases'].append({'name':case['name'], 'passed':not failures, 'failures':failures, **row})
                print('PASS' if not failures else 'FAIL',case['name'],json.dumps(failures),flush=True)
                args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        finally:
            client.close()
        report['passed'] = sum(row['passed'] for row in report['cases'])
        report['total'] = len(report['cases'])
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:report[k] for k in ('passed','total','cost_usd')}),flush=True)
        return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
