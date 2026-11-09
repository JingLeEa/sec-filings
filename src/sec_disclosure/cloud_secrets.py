"""Load a private dotenv payload into a child process without writing it to disk."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from dotenv.parser import parse_stream


ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ('SEC_USER_AGENT', 'SEC_API_KEY', 'SOCLAAS_BASE_URL', 'SOCLAAS_API_KEY', 'SOCLAAS_MODEL')
ALLOWED = {*REQUIRED, 'SOCLAAS_CHATBOT_MODEL', 'DATABASE_URL', 'POSTGRES_HOST',
           'POSTGRES_PORT', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB'}


class CloudSecretsError(ValueError):
    """A safe-to-display error that never contains a secret payload."""


@dataclass(frozen=True)
class SecretReference:
    project: str
    secret: str
    version: str

    def __post_init__(self):
        for value, pattern in ((self.project, r'(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]+)'),
                               (self.secret, r'[A-Za-z0-9_-]{1,255}'),
                               (self.version, r'(?:[1-9][0-9]*|[A-Za-z][A-Za-z0-9_-]{0,62})')):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise CloudSecretsError('Invalid Google Cloud project, secret name, or version.')

    @property
    def name(self):
        return f'projects/{self.project}/secrets/{self.secret}/versions/{self.version}'


def secret_reference(args):
    path = ROOT / 'gcp-secrets.json'
    try:
        defaults = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if not isinstance(defaults, dict):
            raise ValueError
    except (OSError, ValueError):
        raise CloudSecretsError('Could not read gcp-secrets.json. It must contain project, secret, and version identifiers.') from None
    return SecretReference(args.project or defaults.get('project'),
                           args.secret or defaults.get('secret', 'bt4103-api-key'),
                           args.version or defaults.get('version', 'latest'))


def parse_settings(payload: bytes):
    """Accept known settings literally; reject duplicates, malformed and incomplete files."""
    try:
        if len(payload) > 65536:
            raise CloudSecretsError('The secret exceeds the 64 KiB dotenv payload limit.')
        text = payload.decode('utf-8-sig')
    except UnicodeError:
        raise CloudSecretsError('The secret must contain a UTF-8 .env file.') from None
    values = {}
    for binding in parse_stream(StringIO(text)):
        if binding.error:
            raise CloudSecretsError(f'The secret contains invalid .env syntax at line {binding.original.line}.')
        if binding.key is None:
            continue
        if binding.key not in ALLOWED:
            raise CloudSecretsError('The secret contains an unsupported setting. Use the API and optional PostgreSQL settings from .env.example.')
        if binding.key in values:
            raise CloudSecretsError(f'The secret defines {binding.key} more than once.')
        if binding.value is None or '\x00' in binding.value:
            raise CloudSecretsError(f'The secret has an invalid value for {binding.key}.')
        values[binding.key] = binding.value
    missing = [name for name in REQUIRED if not values.get(name, '').strip()]
    if missing:
        raise CloudSecretsError('The secret is missing required settings: ' + ', '.join(missing))
    for name in REQUIRED:
        values[name] = values[name].strip()
    for name in ('SEC_API_KEY', 'SOCLAAS_API_KEY'):
        if any(char.isspace() for char in values[name]):
            raise CloudSecretsError(f'{name} in the secret must not contain whitespace.')
    try:
        url = urlsplit(values['SOCLAAS_BASE_URL'])
        url.port
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(char.isspace() for char in values['SOCLAAS_BASE_URL'])):
            raise ValueError
    except ValueError:
        raise CloudSecretsError('SOCLAAS_BASE_URL in the secret must be an HTTPS API base URL without credentials, query, or fragment.') from None
    return values


def read_secret(reference):
    try:
        from google.cloud import secretmanager
        import google_crc32c
        from google.api_core import exceptions as api_errors
        from google.auth import exceptions as auth_errors
    except ImportError:
        raise CloudSecretsError('Google Secret Manager dependencies are missing. Install them with python -m pip install -r app/requirements.txt.') from None
    try:
        with secretmanager.SecretManagerServiceClient() as client:
            response = client.access_secret_version(request={'name': reference.name}, timeout=30, retry=None)
    except auth_errors.GoogleAuthError:
        raise CloudSecretsError('Google authentication is missing or expired. Run gcloud auth application-default login, then retry.') from None
    except api_errors.PermissionDenied:
        raise CloudSecretsError('Google denied secret access. Enable the Secret Manager API and grant your Google account roles/secretmanager.secretAccessor on this secret. If quota-project permissions are requested, check your ADC quota project.') from None
    except api_errors.NotFound:
        raise CloudSecretsError('The Google secret or version was not found. Check gcp-secrets.json and upload your .env file to that project and secret.') from None
    except api_errors.FailedPrecondition:
        raise CloudSecretsError('The Google secret version is unavailable. Ensure the selected version is enabled.') from None
    except (api_errors.GoogleAPICallError, OSError):
        raise CloudSecretsError('Could not reach Google Secret Manager. Check your connection and try again.') from None
    payload = bytes(response.payload.data)
    if google_crc32c.value(payload) != response.payload.data_crc32c:
        raise CloudSecretsError('Google secret checksum validation failed. No application was started.')
    return parse_settings(payload)


def child_environment(settings):
    environment = dict(os.environ)
    # Each launch uses one cloud version. A stale exported key must not override it.
    environment.update(settings)
    # Manual-review workers inherit these settings; they need no downloaded env file.
    environment.pop('SEC_UI_PIPELINE_ENV_FILE', None)
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', help='Google Cloud project ID; defaults to gcp-secrets.json')
    parser.add_argument('--secret', help='Secret name; defaults to gcp-secrets.json or bt4103-api-key')
    parser.add_argument('--version', help='Version number or alias; defaults to latest')
    parser.add_argument('--check', action='store_true', help='Verify cloud access and required settings without starting the app or making provider calls')
    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='Optional Python arguments after --; defaults to -m streamlit run app/Home.py')
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.check and command:
        parser.error('--check does not accept an application command')
    try:
        reference = secret_reference(args)
        settings = read_secret(reference)
    except CloudSecretsError as error:
        print(f'Cloud settings error: {error}', file=sys.stderr)
        return 1
    # Report identifiers only. Keys and secret contents never enter logs or caches.
    print(f'Cloud API settings verified: {reference.name}.', flush=True)
    if args.check:
        print('Required settings are present. No application or API-provider request was started.')
        return 0
    try:
        return subprocess.run([sys.executable, *(command or ['-m', 'streamlit', 'run', 'app/Home.py'])],
                              cwd=ROOT, env=child_environment(settings), check=False).returncode
    except OSError:
        print('Could not start the application with the project Python interpreter.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
