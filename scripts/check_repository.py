"""Check files eligible for Git for deployment IDs, credentials and artifacts.

This is a focused local hygiene check, not a substitute for credential rotation
or a full repository-history secret audit. It never prints matched values.
"""
from pathlib import Path
import re
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'production channel ID': re.compile(r'-100\d{9,}'),
    'Telegram bot token': re.compile(r'\b\d{7,}:[A-Za-z0-9_-]{25,}\b'),
    'OpenRouter key': re.compile(r'sk-or-v1-[A-Za-z0-9]{30,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}
PRIVATE_KEYS = ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_TEST_ID',
                'TELEGRAM_FAILURE_CHAT_ID', 'LINKEDIN_EMAIL', 'LINKEDIN_PASSWORD',
                'INDEED_EMAIL', 'OPENROUTER_API_KEY', 'OPENROUTER_API')


def main():
    candidates = subprocess.check_output(
        ['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=ROOT
    ).decode().strip('\0').split('\0')
    environment = dotenv_values(ROOT / '.env')
    private_values = {key: environment.get(key) for key in PRIVATE_KEYS if environment.get(key)}
    failures = []
    for name in sorted(set(candidates)):
        path = ROOT / name
        if not path.is_file():
            continue
        if (path.name.startswith('.env') and path.name != '.env.example'
                or path.suffix in ('.db', '.pyc', '.log', '.pem', '.key')
                or 'snapshots' in path.parts):
            failures.append((name, 'private/generated artifact'))
        if path.name.startswith(('codex-session-', 'claude-session-')) or path.name.endswith('.transcript.md'):
            failures.append((name, 'agent session transcript'))
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                failures.append((name, label))
        for key, value in private_values.items():
            if value in content:
                failures.append((name, 'configured ' + key))
    for name, label in failures:
        print(f'FAIL {name}: {label}')
    print(f'Checked {len(set(candidates))} repository files; {len(failures)} findings. No secret values printed.')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
