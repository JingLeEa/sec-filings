"""Cloud launch uses one validated secret and shares it with child workers only."""
import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sec_disclosure import cloud_secrets as cloud


SETTINGS = {'SEC_USER_AGENT': 'Example team contact@example.test', 'SEC_API_KEY': 'sec-test-value',
            'SOCLAAS_BASE_URL': 'https://example.test/v1', 'SOCLAAS_API_KEY': 'llm-test-${LITERAL}',
            'SOCLAAS_MODEL': 'example-model'}
REFERENCE = cloud.SecretReference('example-project', 'sec-filings-env', 'latest')


def payload(settings=None):
    return '\n'.join(name + '=' + json.dumps(value) for name, value in (settings or SETTINGS).items()).encode()


def client_response(raw=None):
    import google_crc32c
    raw = payload() if raw is None else raw
    return SimpleNamespace(payload=SimpleNamespace(data=raw, data_crc32c=google_crc32c.value(raw)))


def test_dotenv_bytes_are_literal_and_accept_windows_newlines_and_bom(monkeypatch):
    monkeypatch.setenv('LITERAL', 'do-not-substitute')
    assert cloud.parse_settings(b'\xef\xbb\xbf' + payload().replace(b'\n', b'\r\n')) == SETTINGS
    assert cloud.parse_settings(payload({**SETTINGS, 'POSTGRES_PASSWORD': 'local-db-password'}))['POSTGRES_PASSWORD'] == 'local-db-password'


@pytest.mark.parametrize('raw', [b'\xff', b'x' * 65537, payload() + b'\nSEC_API_KEY=duplicate',
    payload() + b'\nPYTHONPATH=arbitrary', payload() + b'\nPOSTGRES_PASSWORD',
    payload() + b'\nPOSTGRES_PASSWORD="unterminated', payload() + b'\nPOSTGRES_PASSWORD="null\x00value"',
    payload({'SEC_API_KEY': 'only-one-setting'})])
def test_invalid_payload_fails_without_revealing_values(raw):
    with pytest.raises(cloud.CloudSecretsError) as error:
        cloud.parse_settings(raw)
    assert SETTINGS['SEC_API_KEY'] not in str(error.value)
    assert SETTINGS['SOCLAAS_API_KEY'] not in str(error.value)


@pytest.mark.parametrize('url', ['http://example.test/v1', 'https://llm-test-${LITERAL}@example.test',
                               'https://example.test?key=llm-test-${LITERAL}', 'https://[invalid',
                               'https://example.test:invalid', 'https://example.test/white space'])
def test_invalid_endpoint_reports_setting_name_without_value(url):
    with pytest.raises(cloud.CloudSecretsError, match='SOCLAAS_BASE_URL') as error:
        cloud.parse_settings(payload({**SETTINGS, 'SOCLAAS_BASE_URL': url}))
    assert url not in str(error.value)


def test_remote_payload_checksum_is_verified_before_parsing(monkeypatch):
    from google.cloud import secretmanager
    response = client_response()
    with patch.object(secretmanager, 'SecretManagerServiceClient') as factory:
        client = factory.return_value.__enter__.return_value
        client.access_secret_version.return_value = response
        assert cloud.read_secret(REFERENCE) == SETTINGS
        client.access_secret_version.assert_called_once_with(request={'name': REFERENCE.name}, timeout=30, retry=None)
        response.payload.data += b'corrupted'
        with pytest.raises(cloud.CloudSecretsError, match='checksum'):
            cloud.read_secret(REFERENCE)


@pytest.mark.parametrize('kind,expected', [('auth', 'authentication'), ('denied', 'denied'),
    ('missing', 'not found'), ('disabled', 'enabled'), ('network', 'connection')])
def test_cloud_errors_hide_raw_provider_details(kind, expected):
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth
    from google.cloud import secretmanager
    error_type = {'auth': auth.DefaultCredentialsError, 'denied': api.PermissionDenied,
                  'missing': api.NotFound, 'disabled': api.FailedPrecondition, 'network': api.ServiceUnavailable}[kind]
    with patch.object(secretmanager, 'SecretManagerServiceClient') as factory:
        factory.return_value.__enter__.return_value.access_secret_version.side_effect = error_type('private provider details')
        with pytest.raises(cloud.CloudSecretsError, match=expected) as error:
            cloud.read_secret(REFERENCE)
    assert 'private provider details' not in str(error.value)


def test_reference_defaults_and_command_line_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    (tmp_path / 'gcp-secrets.json').write_text(json.dumps({'project': 'example-project',
        'secret': 'shared-api', 'version': '2'}), encoding='utf-8')
    assert cloud.secret_reference(argparse.Namespace(project=None, secret=None, version=None)) == cloud.SecretReference('example-project', 'shared-api', '2')
    assert cloud.secret_reference(argparse.Namespace(project='other-project', secret='other', version='latest')) == cloud.SecretReference('other-project', 'other', 'latest')
    (tmp_path / 'gcp-secrets.json').write_text('not json', encoding='utf-8')
    with pytest.raises(cloud.CloudSecretsError, match='gcp-secrets.json'):
        cloud.secret_reference(argparse.Namespace(project=None, secret=None, version=None))


@pytest.mark.parametrize('project,secret,version', [('bad/project', 'valid', 'latest'),
    ('example-project', '../bad', 'latest'), ('example-project', 'valid', '1/other'),
    (None, 'valid', 'latest')])
def test_invalid_reference_cannot_change_the_resource_path(project, secret, version):
    with pytest.raises(cloud.CloudSecretsError):
        cloud.SecretReference(project, secret, version)


@pytest.mark.parametrize('command', [[], ['--', 'scripts/run_full_pipeline.py', '--ticker', 'MS',
                                        '--years', '2023', '2024', '2025', '--until', 'preprocessing']])
def test_launch_overrides_stale_keys_and_inherits_local_database_without_writing_files(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    monkeypatch.setenv('SEC_API_KEY', 'old-export')
    monkeypatch.setenv('SEC_UI_PIPELINE_ENV_FILE', '/missing/local.env')
    monkeypatch.setenv('POSTGRES_PORT', '6543')
    with patch.object(cloud, 'read_secret', return_value=SETTINGS), \
         patch.object(cloud.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
        assert cloud.main(['--project', 'example-project', *command]) == 0
    argv = command[1:] or ['-m', 'streamlit', 'run', 'app/Home.py']
    assert run.call_args.args[0] == [sys.executable, *argv]
    kwargs = run.call_args.kwargs
    assert kwargs['env']['SEC_API_KEY'] == SETTINGS['SEC_API_KEY']
    assert kwargs['env']['POSTGRES_PORT'] == '6543'
    assert 'SEC_UI_PIPELINE_ENV_FILE' not in kwargs['env']
    assert kwargs['cwd'] == tmp_path and not kwargs['check'] and 'shell' not in kwargs
    assert os.environ['SEC_API_KEY'] == 'old-export'
    assert list(tmp_path.iterdir()) == []
    log = capsys.readouterr()
    assert SETTINGS['SEC_API_KEY'] not in log.out + log.err
    assert SETTINGS['SOCLAAS_API_KEY'] not in log.out + log.err


def test_check_mode_and_access_failure_never_launch_application(capsys):
    with patch.object(cloud, 'read_secret', return_value=SETTINGS), patch.object(cloud.subprocess, 'run') as run:
        assert cloud.main(['--project', 'example-project', '--check']) == 0
        run.assert_not_called()
    assert 'Required settings are present' in capsys.readouterr().out
    with patch.object(cloud, 'read_secret', side_effect=cloud.CloudSecretsError('Google denied secret access.')), \
         patch.object(cloud.subprocess, 'run') as run:
        assert cloud.main(['--project', 'example-project']) == 1
        run.assert_not_called()
    assert 'Google denied secret access.' in capsys.readouterr().err


def test_real_child_and_its_background_process_receive_settings(monkeypatch, capsys):
    # Exercise actual process inheritance and the existing LLM/chatbot readers;
    # no external provider, database, or Streamlit server is started.
    probe = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from sec_disclosure.llm.config import load_config
from sec_disclosure.chatbot.config import load_chat_config, load_embedding_config
from sec_disclosure.storage.disclosures import connection_arguments
assert load_config().api_key == os.environ['SOCLAAS_API_KEY']
assert load_chat_config()[0].api_key == os.environ['SOCLAAS_API_KEY']
assert load_embedding_config().api_key == os.environ['SOCLAAS_API_KEY']
assert str(connection_arguments()['port']) == '6543'
subprocess.run([sys.executable, '-c', 'import os; assert os.environ["SEC_API_KEY"] == "sec-test-value"; assert "SEC_UI_PIPELINE_ENV_FILE" not in os.environ'], check=True)
"""
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('POSTGRES_PORT', '6543')
    with patch.object(cloud, 'read_secret', return_value=SETTINGS):
        assert cloud.main(['--project', 'example-project', '--', '-c', probe, str(cloud.ROOT / 'src')]) == 0
    assert SETTINGS['SOCLAAS_API_KEY'] not in capsys.readouterr().out


def test_child_exit_status_is_preserved():
    with patch.object(cloud, 'read_secret', return_value=SETTINGS), \
         patch.object(cloud.subprocess, 'run', return_value=SimpleNamespace(returncode=7)):
        assert cloud.main(['--project', 'example-project', '--', 'scripts/example.py']) == 7


def test_help_works_from_checkout_without_google_login():
    result = subprocess.run([sys.executable, str(cloud.ROOT / 'scripts/run_with_gcp_secrets.py'), '--help'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and '--check' in result.stdout
