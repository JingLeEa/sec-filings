"""Bootstrap tests require no downloads, real sign-in, or package installation."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

from sec_disclosure import app_setup as setup


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    (tmp_path / 'gcp-secrets.json').write_text(json.dumps({'project': 'example-project'}), encoding='utf-8')
    return tmp_path


@pytest.mark.parametrize('system,machine,suffix', [('Darwin','arm64','darwin-arm.tar.gz'),
    ('Darwin','aarch64','darwin-arm.tar.gz'), ('Darwin','x86_64','darwin-x86_64.tar.gz'),
    ('Windows','AMD64','windows-x86_64.zip'), ('Windows','x86','windows-x86.zip'),
    ('Windows','i686','windows-x86.zip')])
def test_pinned_downloads_match_supported_platforms(system, machine, suffix):
    url, checksum = setup.archive_spec(system, machine)
    assert url == f'https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-{setup.SDK_VERSION}-{suffix}'
    assert len(checksum) == 64 and all(c in '0123456789abcdef' for c in checksum)


def test_unsupported_architecture_has_actionable_error():
    with pytest.raises(setup.SetupError, match='manually'):
        setup.archive_spec('Windows', 'unknown')


def test_download_is_verified_and_never_promoted_on_checksum_failure(tmp_path):
    data = b'example archive'
    target = tmp_path / 'download'
    with patch.object(setup, 'urlopen', return_value=BytesIO(data)) as fetch:
        setup.download_archive('https://storage.googleapis.com/example', target, hashlib.sha256(data).hexdigest())
    fetch.assert_called_once_with('https://storage.googleapis.com/example', timeout=45)
    assert target.read_bytes() == data
    target.unlink()
    with patch.object(setup, 'urlopen', return_value=BytesIO(data)):
        with pytest.raises(setup.SetupError, match='SHA256') as error:
            setup.download_archive('https://storage.googleapis.com/example', target, '0' * 64)
    assert 'example' in str(error.value)
    assert '0' * 64 in str(error.value)
    assert hashlib.sha256(data).hexdigest() in str(error.value)
    assert str(len(data)) + ' bytes' in str(error.value)


@pytest.mark.skipif(not os.environ.get('SEC_GCLOUD_ARCHIVE_TEST_DIR'),
                    reason='Optional: provide downloaded official versioned Google archives')
@pytest.mark.parametrize('system,machine', list(setup.ARCHIVES))
def test_pinned_checksums_match_official_versioned_archive_bytes(system, machine):
    # This caught mismatches between Google's rapid Mac builds and the
    # versioned archive bucket. Fixtures are real downloads, never generated
    # from the expected digest. Ordinary tests perform no network requests.
    url, expected = setup.archive_spec(system, machine)
    path = Path(os.environ['SEC_GCLOUD_ARCHIVE_TEST_DIR']) / url.rsplit('/', 1)[-1]
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    assert digest.hexdigest() == expected
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            version = archive.read('google-cloud-sdk/VERSION')
    else:
        with tarfile.open(path, 'r:gz') as archive:
            version = archive.extractfile('google-cloud-sdk/VERSION').read()
    assert version.decode('utf-8').strip() == setup.SDK_VERSION


def make_archive(path, kind, entries):
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in entries:
                archive.writestr(name, data)
    else:
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, BytesIO(data))


@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_both_archive_formats_preserve_the_sdk_python_entrypoint(tmp_path, kind):
    path = tmp_path / 'download'
    make_archive(path, kind, [('google-cloud-sdk/lib/gcloud.py', b'print("sdk")'),
                              ('google-cloud-sdk/bin/gcloud', b'#!/bin/sh\n')])
    setup.unpack_archive(path, tmp_path)
    assert (tmp_path / 'google-cloud-sdk/lib/gcloud.py').read_bytes() == b'print("sdk")'
    if kind == 'tar' and os.name != 'nt':
        assert (tmp_path / 'google-cloud-sdk/bin/gcloud').stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize('kind', ['zip', 'tar'])
@pytest.mark.parametrize('name', ['../outside', '/absolute', 'google-cloud-sdk/../../outside',
                                  'google-cloud-sdk\\..\\outside', 'C:/outside', 'other-root/file'])
def test_archive_cannot_write_outside_sdk_root(tmp_path, kind, name):
    path = tmp_path / 'download'
    make_archive(path, kind, [(name, b'unsafe')])
    with pytest.raises(setup.SetupError, match='invalid path'):
        setup.unpack_archive(path, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['download']


def test_relative_documentation_links_are_copied_without_symlink_privileges(tmp_path):
    path = tmp_path / 'download'
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo('google-cloud-sdk/docs/README')
        member.size = 5
        archive.addfile(member, BytesIO(b'notes'))
        link = tarfile.TarInfo('google-cloud-sdk/docs/subdir/readme')
        link.type, link.linkname = tarfile.SYMTYPE, '../README'
        archive.addfile(link)
    setup.unpack_archive(path, tmp_path)
    result = tmp_path / 'google-cloud-sdk/docs/subdir/readme'
    assert result.read_bytes() == b'notes' and not result.is_symlink()


def test_escaping_archive_link_is_rejected(tmp_path):
    path = tmp_path / 'download'
    with tarfile.open(path, 'w:gz') as archive:
        link = tarfile.TarInfo('google-cloud-sdk/docs/link')
        link.type, link.linkname = tarfile.SYMTYPE, '../../../outside'
        archive.addfile(link)
    with pytest.raises(setup.SetupError, match='invalid path'):
        setup.unpack_archive(path, tmp_path)


def test_omitted_vendor_fixture_does_not_break_sdk_installation(tmp_path):
    path = tmp_path / 'download'
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo('google-cloud-sdk/lib/gcloud.py')
        member.size = 4
        archive.addfile(member, BytesIO(b'pass'))
        link = tarfile.TarInfo('google-cloud-sdk/vendor/tests/link')
        link.type, link.linkname = tarfile.SYMTYPE, '../omitted-fixture'
        archive.addfile(link)
    setup.unpack_archive(path, tmp_path)
    assert (tmp_path / 'google-cloud-sdk/lib/gcloud.py').read_bytes() == b'pass'
    assert not (tmp_path / 'google-cloud-sdk/vendor/tests/link').exists()


def test_directory_fixture_links_are_copied_without_recursion(tmp_path):
    path = tmp_path / 'download'
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo('google-cloud-sdk/fixtures/real/file')
        member.size = 4
        archive.addfile(member, BytesIO(b'data'))
        link = tarfile.TarInfo('google-cloud-sdk/fixtures/alias')
        link.type, link.linkname = tarfile.SYMTYPE, 'real'
        archive.addfile(link)
    setup.unpack_archive(path, tmp_path)
    assert (tmp_path / 'google-cloud-sdk/fixtures/alias/file').read_bytes() == b'data'


def test_existing_environment_is_preserved_byte_for_byte(root):
    path = root / '.env'
    original = b'# existing config\r\nPOSTGRES_PASSWORD="my-password"\r\n'
    path.write_bytes(original)
    setup.prepare_database_settings()
    assert path.read_bytes() == original


def test_new_environment_only_contains_local_database_settings(root):
    setup.prepare_database_settings()
    content = (root / '.env').read_text(encoding='utf-8')
    assert 'POSTGRES_DB="sec_disclosure"' in content
    assert 'API_KEY' not in content
    if os.name != 'nt':
        assert (root / '.env').stat().st_mode & 0o777 == 0o600


def test_foreign_venv_is_not_overwritten(root):
    (root / '.venv').mkdir()
    (root / '.venv/other-platform').write_text('keep', encoding='utf-8')
    with pytest.raises(setup.SetupError, match='another platform'):
        setup.prepare_python()
    assert (root / '.venv/other-platform').read_text() == 'keep'


def test_dependency_install_targets_the_venv_and_complete_app_requirements(root):
    executable = root / '.venv/bin/python'
    with patch.object(setup, 'run') as run:
        setup.install_dependencies(executable)
    calls = [call.args[0] for call in run.call_args_list]
    assert all(command[0] == executable for command in calls)
    assert [executable, '-m', 'pip', 'install', '-r', 'app/requirements.txt', '-e', '.'] in calls
    assert calls[-1] == [executable, '-m', 'pip', 'check']


def test_sdk_download_promotes_only_complete_verified_installation(root, monkeypatch):
    monkeypatch.setattr(setup, 'existing_sdk', lambda: None)
    archive_source = root / 'source.zip'
    make_archive(archive_source, 'zip', [('google-cloud-sdk/lib/gcloud.py', b'print("ok")')])
    data = archive_source.read_bytes()
    monkeypatch.setattr(setup, 'archive_spec', lambda: ('https://storage.googleapis.com/example', hashlib.sha256(data).hexdigest()))
    with patch.object(setup, 'urlopen', return_value=BytesIO(data)), patch.object(setup, 'run'):
        command, environment = setup.prepare_gcloud(Path(sys.executable))
    sdk = root / '.tools/google-cloud-sdk'
    assert command == [Path(sys.executable), '-S', sdk / 'lib/gcloud.py']
    assert (sdk / 'lib/gcloud.py').is_file()
    assert environment['CLOUDSDK_PYTHON'] == sys.executable
    assert not list((root / '.tools').glob('.gcloud-setup-*'))


def test_failed_sdk_download_does_not_leave_a_partial_install(root, monkeypatch):
    monkeypatch.setattr(setup, 'existing_sdk', lambda: None)
    monkeypatch.setattr(setup, 'archive_spec', lambda: ('https://storage.googleapis.com/example', '0' * 64))
    with patch.object(setup, 'urlopen', return_value=BytesIO(b'incorrect')), patch.object(setup, 'run') as run:
        with pytest.raises(setup.SetupError, match='SHA256'):
            setup.prepare_gcloud(Path(sys.executable))
    run.assert_not_called()
    assert not (root / '.tools/google-cloud-sdk').exists()
    assert not list((root / '.tools').iterdir())


def test_existing_local_sdk_avoids_download_and_shell_commands(root, monkeypatch):
    sdk = root / '.tools/google-cloud-sdk'
    (sdk / 'lib').mkdir(parents=True)
    (sdk / 'lib/gcloud.py').write_text('# existing sdk', encoding='utf-8')
    with patch.object(setup, 'download_archive') as download, patch.object(setup, 'run') as run:
        command, environment = setup.prepare_gcloud(Path(sys.executable))
    download.assert_not_called()
    assert command == [Path(sys.executable), '-S', sdk / 'lib/gcloud.py']
    assert environment['CLOUDSDK_CORE_DISABLE_USAGE_REPORTING'] == 'true'
    assert run.call_args.args[0] == [*command, '--version']


def test_successful_existing_signin_does_not_open_browser(root):
    result = SimpleNamespace(returncode=0, stdout='Settings verified.', stderr='')
    with patch.object(setup, 'run', return_value=result) as run:
        setup.verify_cloud('python', ['python','gcloud.py'], {}, 'example-project')
    assert run.call_count == 1 and run.call_args.args[0][-1] == '--check'


def test_first_run_signin_uses_selected_project_then_rechecks_access(root):
    failure = SimpleNamespace(returncode=1, stdout='', stderr='Missing Google credentials.')
    success = SimpleNamespace(returncode=0, stdout='Settings verified.', stderr='')
    with patch.object(setup, 'run', side_effect=[failure, success, success]) as run:
        setup.verify_cloud('python', ['python','gcloud.py'], {'CONFIG':'unchanged'}, 'example-project')
    login = run.call_args_list[1]
    assert login.args[0] == ['python','gcloud.py','auth','application-default','login','--project=example-project']
    assert login.kwargs['environment'] == {'CONFIG':'unchanged'}
    assert run.call_args_list[2].args[0][-1] == '--check'


def test_skip_login_fails_without_starting_interactive_auth(root, capsys):
    with patch.object(setup, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='Secret access denied.')) as run:
        with pytest.raises(setup.SetupError, match='Secret Accessor'):
            setup.verify_cloud('python', ['python','gcloud.py'], {}, 'example-project', skip_login=True)
    assert run.call_count == 1
    assert 'Secret access denied.' in capsys.readouterr().err


def test_setup_handles_spaces_and_finishes_without_starting_docker_or_ui(root, monkeypatch, capsys):
    original = root
    root = root / 'Project with spaces'
    root.mkdir()
    (root / 'gcp-secrets.json').write_bytes((original / 'gcp-secrets.json').read_bytes())
    monkeypatch.setattr(setup, 'ROOT', root)
    executable = root / '.venv/bin/python'
    with patch.object(setup, 'prepare_python', return_value=executable), \
         patch.object(setup, 'install_dependencies') as install, \
         patch.object(setup, 'prepare_gcloud', return_value=(['gcloud'], {})), \
         patch.object(setup, 'verify_cloud') as verify:
        assert setup.main(['--skip-login']) == 0
    install.assert_called_once_with(executable)
    assert verify.call_args.args[-1] == 'example-project'
    assert verify.call_args.kwargs == {'skip_login': True}
    output = capsys.readouterr().out
    assert 'docker compose up -d --wait' in output and 'scripts/run_with_gcp_secrets.py' in output


def test_process_arguments_with_spaces_are_passed_without_a_shell(root):
    command = [root / 'with spaces/python', '-S', root / 'sdk path/lib/gcloud.py', '--version']
    with patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
        setup.run(command, stage='test', environment={'SAMPLE':'value'})
    assert run.call_args.args[0] == [str(part) for part in command]
    assert 'shell' not in run.call_args.kwargs
    assert run.call_args.kwargs['env'] == {'SAMPLE':'value'}


def test_invalid_project_stops_before_installing_anything(root):
    (root / 'gcp-secrets.json').write_text('{"project":"bad/project"}', encoding='utf-8')
    with patch.object(setup, 'prepare_python') as prepare:
        assert setup.main([]) == 1
    prepare.assert_not_called()


def test_bootstrap_help_has_no_third_party_imports_or_signin():
    root = Path(__file__).resolve().parents[1]
    # -S ensures a fresh machine need not already have dotenv/google installed.
    result = subprocess.run([sys.executable, '-S', str(root / 'scripts/setup_app.py'), '--help'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and '--skip-login' in result.stdout
