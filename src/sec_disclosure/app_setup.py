"""Standard-library-only bootstrap for a fresh Mac or Windows checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SDK_VERSION = '592.0.0'
# Pinned official release and published SHA256s, not a mutable 'latest' download.
# https://docs.cloud.google.com/sdk/docs/downloads-versioned-archives
ARCHIVES = {
    ('Darwin', 'arm64'): ('darwin-arm.tar.gz', '57553ae3c300f4ebaaa8fb4dee8d40c4d8c1141d84d266b41c1daf1669c4001a'),
    ('Darwin', 'x86_64'): ('darwin-x86_64.tar.gz', 'bad0a5caba7b67c5d97a278e8ef31e56d341c72f944293f4e3b06091ef69148d'),
    ('Windows', 'x86_64'): ('windows-x86_64.zip', '95b606b92ff63d84c7ac23fbc20752f1f3fd673b973f77200994d480a53791ab'),
    ('Windows', 'x86'): ('windows-x86.zip', 'c7af3ab51a963b9d8bb2172e6ff6692b43e41db1503d629fa250049dc21f4ca8'),
}
LOCAL_DATABASE = '''# Local PostgreSQL. Shared API settings are loaded from Google Secret Manager.
POSTGRES_HOST="127.0.0.1"
POSTGRES_PORT="5432"
POSTGRES_USER="sec"
POSTGRES_PASSWORD="sec_local_pw"
POSTGRES_DB="sec_disclosure"
'''


class SetupError(ValueError):
    """An actionable setup failure; credential contents never appear here."""


def archive_spec(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    machine = {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64',
               'i386': 'x86', 'i686': 'x86'}.get(machine, machine)
    try:
        suffix, checksum = ARCHIVES[(system, machine)]
    except KeyError:
        raise SetupError('Automatic Google CLI installation supports Intel/Apple silicon Macs and x86/x64 Windows. Install Google Cloud CLI manually on this platform, then rerun setup.') from None
    filename = f'google-cloud-cli-{SDK_VERSION}-{suffix}'
    return 'https://storage.googleapis.com/cloud-sdk-release/' + filename, checksum


def run(command, *, stage, environment=None, capture=False):
    try:
        result = subprocess.run([str(part) for part in command], cwd=ROOT, env=environment,
                                capture_output=capture, text=True, encoding='utf-8', errors='replace', check=False)
    except OSError:
        raise SetupError(f'Could not start {stage}. Check the installed Python and file permissions.') from None
    if not capture and result.returncode:
        raise SetupError(f'{stage} failed (exit {result.returncode}). Resolve the error shown above and rerun this setup command.')
    return result


def prepare_python():
    directory = ROOT / '.venv'
    executable = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.is_file():
        if directory.exists() and any(directory.iterdir()):
            raise SetupError('The existing .venv belongs to another platform or is incomplete. Rename it and rerun setup to create a new environment.')
        print('Creating the project Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(directory)
    result = run([executable, '-c', 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,15) else 1)'],
                 stage='the project Python version check', capture=True)
    if result.returncode:
        raise SetupError('The project .venv needs Python 3.10-3.15. Use Python 3.13 to create a new .venv.')
    return executable


def install_dependencies(executable):
    print('Installing the app and its Python dependencies...', flush=True)
    run([executable, '-m', 'pip', 'install', '--upgrade', 'pip'], stage='pip installation')
    run([executable, '-m', 'pip', 'install', '-r', 'app/requirements.txt', '-e', '.'], stage='app dependency installation')
    run([executable, '-m', 'pip', 'check'], stage='dependency verification')


def prepare_database_settings():
    # Preserve existing settings exactly, including credentials and comments.
    path = ROOT / '.env'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(LOCAL_DATABASE)
    print('Created local PostgreSQL settings in .env. API keys will come from Google Cloud.', flush=True)


def download_archive(url, destination, checksum):
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(url, timeout=45) as response, destination.open('xb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 300 * 1024 * 1024:
                    raise SetupError('The Google CLI download exceeded its expected size. Nothing was installed.')
                digest.update(chunk)
                output.write(chunk)
    except (URLError, TimeoutError, OSError):
        raise SetupError('Could not download Google Cloud CLI. Check your internet connection and rerun setup.') from None
    if digest.hexdigest() != checksum:
        raise SetupError('The Google CLI download failed its SHA256 check. Nothing was installed; rerun setup to download it again.')


def archive_target(directory, name):
    # Validate before creating any paths; links are materialized only after files.
    path = PurePosixPath(name)
    if ('\\' in name or path.is_absolute() or PureWindowsPath(name).drive or '..' in path.parts
            or not path.parts or path.parts[0] != 'google-cloud-sdk'):
        raise SetupError('The Google CLI archive contains an invalid path. Nothing was installed.')
    return directory.joinpath(*path.parts)


def unpack_archive(archive, directory):
    links = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = archive_target(directory, member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    links.append((target, source.read(member).decode('utf-8')))
                elif member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as data, target.open('xb') as output:
                        shutil.copyfileobj(data, output)
    else:
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                target = archive_target(directory, member.name)
                if member.issym():
                    links.append((target, member.linkname))
                elif member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as data, target.open('xb') as output:
                        shutil.copyfileobj(data, output)
                    target.chmod(member.mode & 0o777)
                else:
                    raise SetupError('The Google CLI archive contains an unsupported entry. Nothing was installed.')
    # Copy relative documentation/test-fixture links instead of requiring
    # symlink privileges on Windows.
    for target, link in links:
        relative = target.relative_to(directory).as_posix()
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(relative), link))
        source = archive_target(directory, normalized)
        if PurePosixPath(link).is_absolute() or PureWindowsPath(link).drive or '\\' in link:
            raise SetupError('The Google CLI archive contains an invalid link. Nothing was installed.')
        # Google ships dangling links to omitted vendored test fixtures. They
        # provide no runtime content and need not become symlinks on Windows.
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if source == target or source in target.parents or target in source.parents:
                raise SetupError('The Google CLI archive contains a recursive link. Nothing was installed.')
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)


def gcloud_environment(executable, sdk):
    return {**os.environ, 'CLOUDSDK_PYTHON': str(executable), 'CLOUDSDK_ROOT_DIR': str(sdk),
            'CLOUDSDK_CORE_DISABLE_USAGE_REPORTING': 'true'}


def existing_sdk():
    local = ROOT / '.tools/google-cloud-sdk'
    if (local / 'lib/gcloud.py').is_file():
        return local
    located = shutil.which('gcloud')
    if located:
        sdk = Path(located).resolve().parent.parent
        if (sdk / 'lib/gcloud.py').is_file():
            return sdk
    return None


def prepare_gcloud(executable):
    sdk = existing_sdk()
    if sdk is None:
        tools = ROOT / '.tools'
        tools.mkdir(exist_ok=True)
        sdk = tools / 'google-cloud-sdk'
        if sdk.exists():
            raise SetupError('The existing .tools/google-cloud-sdk installation is incomplete. Rename that folder and rerun setup.')
        url, checksum = archive_spec()
        print(f'Downloading Google Cloud CLI {SDK_VERSION} from Google...', flush=True)
        with TemporaryDirectory(prefix='.gcloud-setup-', dir=tools) as temporary:
            directory = Path(temporary)
            archive = directory / 'download'
            download_archive(url, archive, checksum)
            unpack_archive(archive, directory)
            extracted = directory / 'google-cloud-sdk'
            if not (extracted / 'lib/gcloud.py').is_file():
                raise SetupError('The Google CLI download is incomplete. Nothing was installed.')
            extracted.rename(sdk)
    # Call Google's Python entry point directly, avoiding shell/.cmd quoting
    # differences and using the project's known Python on both platforms.
    command = [executable, '-S', sdk / 'lib/gcloud.py']
    run([*command, '--version'], stage='Google CLI verification',
        environment=gcloud_environment(executable, sdk), capture=False)
    return command, gcloud_environment(executable, sdk)


def cloud_project():
    try:
        project = json.loads((ROOT / 'gcp-secrets.json').read_text(encoding='utf-8'))['project']
        if not isinstance(project, str) or not re.fullmatch(r'(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]+)', project):
            raise ValueError
        return project
    except (OSError, ValueError, KeyError, TypeError):
        raise SetupError('gcp-secrets.json must contain a valid Google Cloud project ID.') from None


def verify_cloud(executable, command, environment, project, *, skip_login=False):
    check = [executable, ROOT / 'scripts/run_with_gcp_secrets.py', '--check']
    print('Checking access to the shared API settings...', flush=True)
    result = run(check, stage='Google secret access check', capture=True)
    if result.returncode and not skip_login:
        print('Complete Google sign-in in your browser using the account granted access to the shared secret.', flush=True)
        run([*command, 'auth', 'application-default', 'login', '--project=' + project],
            stage='Google sign-in', environment=environment)
        result = run(check, stage='Google secret access check', capture=True)
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        raise SetupError('Shared API settings are not ready. Check Google sign-in and Secret Accessor permissions, then rerun setup.')
    print(result.stdout.strip(), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-login', action='store_true', help='Use existing Google credentials; fail without opening a browser if they cannot access the secret')
    parser.add_argument('--skip-install', action='store_true', help='Reuse Python dependencies already installed in .venv')
    args = parser.parse_args(argv)
    try:
        if not (3, 10) <= sys.version_info[:2] <= (3, 15):
            raise SetupError('Run setup with Python 3.13 (supported: Python 3.10-3.15).')
        project = cloud_project()
        executable = prepare_python()
        if not args.skip_install:
            install_dependencies(executable)
        prepare_database_settings()
        command, environment = prepare_gcloud(executable)
        verify_cloud(executable, command, environment, project, skip_login=args.skip_login)
    except (SetupError, OSError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f'Setup stopped: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nSetup interrupted. Rerun the same command to continue.', file=sys.stderr)
        return 130
    launch = [str(executable.relative_to(ROOT)), 'scripts/run_with_gcp_secrets.py']
    formatted = subprocess.list2cmdline(launch) if os.name == 'nt' else shlex.join(launch)
    print('\nSetup complete. Start Docker Desktop, then run:', flush=True)
    print('docker compose up -d --wait')
    print(formatted if os.name != 'nt' else '.\\' + formatted)
    return 0
