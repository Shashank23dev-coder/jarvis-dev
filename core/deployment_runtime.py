"""Fixed Docker deployment adapter. Never returns container env or raw output."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from dotenv import dotenv_values
from tools.policy import ToolDenied
from .deployment import Settings

DOCKER = ['/usr/bin/docker', '--host=unix:///var/run/docker.sock']
FIXED_ENV = dict(JARVIS_MODE='production', JARVIS_HOST='0.0.0.0', JARVIS_PORT='8000',
                 JARVIS_STATE_DIR='/var/lib/jarvis', JARVIS_DB_PATH='/var/lib/jarvis/jarvis.sqlite3')
REQUIRED_SECRETS = ('OPENAI_API_KEY', 'JARVIS_AUTH_TOKEN', 'JARVIS_PUBLIC_ORIGIN')
# Optional: older images simply ignore it, so restore/rollback keeps working.
OWNER_LOGIN = 'JARVIS_OWNER_LOGIN_HASH'


class DockerRuntime:
    def __init__(self, config):
        self.config = config

    def docker(self, *args, timeout=30, optional=False):
        # Capture everything; never reflect Docker diagnostics, build logs or env.
        # Docker's build client writes beneath HOME even without user config.
        # Give each invocation an empty mode-0700 home, outside the build context;
        # never inherit operator credentials/config or pass this path to containers.
        # Cleanup also runs on command failure, timeout and interruption.
        with tempfile.TemporaryDirectory(prefix='jarvis-controller-home-') as directory:
            result = subprocess.run([*DOCKER, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=timeout, env={'PATH': '/usr/bin:/bin', 'HOME': directory})
        if result.returncode:
            if optional:
                return None
            raise ToolDenied('Docker deployment operation failed')
        return result.stdout.decode()

    def inspect_container(self, name):
        result = self.docker('container', 'inspect', name, optional=True)
        return json.loads(result)[0] if result else None

    def publish_status(self, snapshot):
        # Only the strict public projection enters the existing state volume.
        # No new mounts, host journal, secrets, controller config or Docker socket.
        from .deployment_status import projection
        safe = self.redact(projection(snapshot['current'], snapshot['history'][::-1], snapshot['observed_at']))
        script = '''import json, os, sys, tempfile
from pathlib import Path
root = Path('/var/lib/jarvis')
assert root.resolve() == root
data = json.loads(sys.argv[1])
name = None
try:
    with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as stream:
        name = stream.name
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, root / 'deployment-status.json')
finally:
    if name and os.path.exists(name): os.unlink(name)
'''
        self.docker('exec', self.config.container, 'python', '-c', script, json.dumps(safe))

    def secrets(self):
        path = self.config.env_file
        if path.resolve() != path or not path.is_file() or path.stat().st_mode & 0o077:
            raise ToolDenied('Production env file must be private and non-symlinked')
        raw = dotenv_values(path, interpolate=False)
        env = {key: raw.get(key) for key in REQUIRED_SECRETS}
        if OWNER_LOGIN in raw:
            env[OWNER_LOGIN] = raw[OWNER_LOGIN]
        if any(not value or '\n' in value or '\r' in value or '\x00' in value for value in env.values()):
            raise ToolDenied('Invalid production environment')
        Settings.from_env({**env, **FIXED_ENV})
        return env

    def redact(self, result):
        encoded = json.dumps(result)
        for key, value in self.secrets().items():
            if key != 'JARVIS_PUBLIC_ORIGIN':
                encoded = encoded.replace(json.dumps(value)[1:-1], '[redacted]')
        return json.loads(encoded)

    def security(self, info, volume, port, *, new=False):
        host, config = info['HostConfig'], info['Config']
        bindings = host.get('PortBindings')
        mounts = info['Mounts']
        persistent = [m for m in mounts if m['Destination'] != '/tmp']
        if not (config['User'] == '10001:10001' and host['ReadonlyRootfs'] and
                host['CapDrop'] == ['ALL'] and not host.get('CapAdd') and not host['Privileged'] and
                'no-new-privileges:true' in host['SecurityOpt'] and
                not any('unconfined' in s for s in host['SecurityOpt']) and
                host['Memory'] == 1073741824 and host['NanoCpus'] == 2000000000 and
                host['PidsLimit'] == 256 and host.get('Init') and
                host.get('NetworkMode') not in {'host', 'none'} and
                not host.get('NetworkMode', '').startswith('container:') and
                host.get('PidMode', '') == '' and host.get('IpcMode') == 'private' and
                not host.get('Devices') and not host.get('DeviceRequests') and
                bindings == {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]} and
                len(persistent) == 1 and persistent[0]['Type'] == 'volume' and
                persistent[0]['Name'] == volume and persistent[0]['Destination'] == '/var/lib/jarvis' and
                persistent[0]['RW'] and set(host.get('Tmpfs', {})) == {'/tmp'} and
                all(m['Type'] == 'tmpfs' for m in mounts if m['Destination'] == '/tmp')):
            raise ToolDenied('Production security configuration mismatch')
        environment = dict(item.split('=', 1) for item in config['Env'])
        if any(environment.get(k) != v for k, v in FIXED_ENV.items()):
            raise ToolDenied('Production state configuration mismatch')
        secrets = self.secrets()
        # Handover: a running (or retained rollback) container is compared on the
        # three original keys only, so one started before the owner login existed
        # still passes. A container this tool just started must also carry the
        # file's owner login hash exactly (or lack it when the file lacks it).
        if any(environment.get(k) != secrets[k] for k in REQUIRED_SECRETS):
            raise ToolDenied('Production env differs from configured secret file; reconcile separately')
        if new and environment.get(OWNER_LOGIN) != secrets.get(OWNER_LOGIN):
            raise ToolDenied('Production env differs from configured secret file; reconcile separately')

    def current(self):
        info = self.inspect_container(self.config.container)
        if not info or not info['State']['Running']:
            raise ToolDenied('A running known-good production container is required')
        self.security(info, self.config.volume, 8000)
        return dict(container_id=info['Id'], image=info['Image'],
                    revision=(info['Config'].get('Labels') or {}).get('org.opencontainers.image.revision'))

    def assert_current(self, previous):
        if self.current() != previous:
            raise ToolDenied('Production changed since preview')

    def image(self, sha):
        output = self.docker('image', 'inspect', 'jarvis-release:' + sha, optional=True)
        return json.loads(output)[0]['Id'] if output else None

    def build(self, sha, context):
        # The approved Git Dockerfile must retain the reviewed runtime contract.
        # Never build a candidate-supplied Dockerfile with new host capabilities.
        trusted = Path(__file__).resolve().parents[1] / 'Dockerfile'
        if (context / 'Dockerfile').read_bytes() != trusted.read_bytes():
            raise ToolDenied('Dockerfile differs from the running reviewed controller')
        self.docker('build', '--pull=false', '--network=default', '--label',
                    'org.opencontainers.image.revision=' + sha,
                    '--tag', 'jarvis-release:' + sha, str(context), timeout=900)
        image = self.image(sha)
        metadata = json.loads(self.docker('image', 'inspect', image))[0]
        if metadata['Config'].get('Volumes') != {'/var/lib/jarvis': {}}:
            raise ToolDenied('Unexpected image volumes')
        return image

    def browser_check(self, image, artifacts):
        # Throwaway app + Chromium containers only; no production secrets, names or ports.
        from .deployment_browser import BrowserCheck
        return BrowserCheck().run(image, artifacts)

    def run(self, name, image, volume, port, release_id):
        # Only the existing production values (plus the optional owner login hash) enter a private transient env file.
        # They never enter argv, build context, journal, API results or logs.
        with tempfile.NamedTemporaryFile(mode='w', prefix='jarvis-runtime-env-') as stream:
            for key, value in self.secrets().items():
                stream.write(key + '=' + value + '\n')
            stream.flush()
            args = ['run', '-d', '--pull=never', '--name', name, '--label', 'jarvis.release=' + release_id,
                    '--user=10001:10001', '--init', '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges:true', '--memory=1g', '--memory-swap=1g',
                    '--cpus=2', '--pids-limit=256', '--ipc=private', '--restart=unless-stopped',
                    '--stop-timeout=390', '--log-driver=json-file', '--log-opt=max-size=10m',
                    '--log-opt=max-file=5', '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777',
                    '--mount', f'type=volume,src={volume},dst=/var/lib/jarvis',
                    '--publish', f'127.0.0.1:{port}:8000', '--env-file', stream.name]
            for key, value in FIXED_ENV.items():
                args += ['--env', key + '=' + value]
            self.docker(*args, image, timeout=60)
        info = self.inspect_container(name)
        self.security(info, volume, port, new=True)
        if info['Image'] != image:
            raise ToolDenied('Image identity mismatch')

    def request(self, url, path, *, authenticated=True):
        env = self.secrets()
        headers = {'Host': urlsplit(env['JARVIS_PUBLIC_ORIGIN']).netloc}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + env['JARVIS_AUTH_TOKEN']
        # Disable proxy inheritance and redirects so credentials stay at target.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(urllib.request.Request(url + path, headers=headers), timeout=3) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ToolDenied('Smoke result exceeds bound')
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def verify(self, name, port, *, public=False, baseline=None):
        deadline = time.monotonic() + 60
        while True:
            try:
                info = self.inspect_container(name)
                if not info or not info['State']['Running']:
                    raise ToolDenied('Container did not start')
                urls = [f'http://127.0.0.1:{port}']
                if public:
                    urls.append(self.secrets()['JARVIS_PUBLIC_ORIGIN'])
                fingerprints = None
                for url in urls:
                    if self.request(url, '/health', authenticated=False) != (200, {'status': 'ok'}):
                        raise ToolDenied('Health check failed')
                    if self.request(url, '/conversations', authenticated=False)[0] != 401:
                        raise ToolDenied('Authentication boundary failed')
                    status, body = self.request(url, '/conversations')
                    if status != 200 or not isinstance(body, dict) or not isinstance(body.get('conversations'), list):
                        raise ToolDenied('Authenticated application smoke failed')
                    fingerprints = sorted(hashlib.sha256(row['id'].encode()).hexdigest() for row in body['conversations'])
                    if baseline and not set(baseline).issubset(fingerprints):
                        raise ToolDenied('Persistent application records missing')
                return fingerprints
            except Exception:
                if time.monotonic() >= deadline:
                    raise ToolDenied('Health, authentication or persistence verification failed') from None
                time.sleep(1)

    def precheck(self, record):
        identifier = record['id']
        name, volume = 'jarvis-preflight-' + identifier, 'jarvis-preflight-' + identifier
        # First milestone disallows database implementation changes. Arbitrary
        # destructive migrations require a separately reviewed backup/restore plan.
        prior_hash = self.docker('exec', record['previous']['container_id'],
                                 'sha256sum', '/app/core/database.py').split()[0]
        if prior_hash != record['database_hash']:
            raise ToolDenied('Database changes require a separate migration deployment plan')
        self.docker('volume', 'create', '--label', 'jarvis.release=' + identifier, volume)
        try:
            self.run(name, record['image'], volume, 18000, identifier)
            self.verify(name, 18000)
        finally:
            self.cleanup(record)

    def remove_owned(self, name, identifier):
        info = self.inspect_container(name)
        if info:
            if (info['Config'].get('Labels') or {}).get('jarvis.release') != identifier:
                raise ToolDenied('Refusing to remove an unrelated container')
            self.docker('rm', '--force', info['Id'], timeout=60)

    def cleanup(self, record):
        name = 'jarvis-preflight-' + record['id']
        self.remove_owned(name, record['id'])
        volume = self.docker('volume', 'inspect', name, optional=True)
        if volume:
            if (json.loads(volume)[0].get('Labels') or {}).get('jarvis.release') != record['id']:
                raise ToolDenied('Refusing to remove an unrelated volume')
            self.docker('volume', 'rm', name)

    def switch(self, record):
        previous = record['previous']
        # Retain both container configuration/secrets and immutable image. No rm,
        # image prune, volume deletion, Compose down, or Caddy modification.
        self.docker('image', 'tag', previous['image'], 'jarvis-rollback:' + record['id'])
        self.docker('stop', '--time=390', previous['container_id'], timeout=420)
        self.docker('rename', previous['container_id'], 'jarvis-previous-' + record['id'])
        self.run(self.config.container, record['image'], self.config.volume, 8000, record['id'])

    def restore(self, record):
        previous = self.inspect_container(record['previous']['container_id'])
        if not previous or previous['Image'] != record['previous']['image']:
            raise ToolDenied('Retained rollback container is missing or changed')
        current = self.inspect_container(self.config.container)
        if current and current['Id'] != previous['Id']:
            if (current['Config'].get('Labels') or {}).get('jarvis.release') != record['id']:
                raise ToolDenied('Refusing to stop an unrelated container')
            self.docker('stop', '--time=390', current['Id'], timeout=420)
            self.remove_owned(self.config.container, record['id'])
        if previous['Name'] != '/' + self.config.container:
            self.docker('rename', previous['Id'], self.config.container)
        self.docker('start', previous['Id'], timeout=60)
        self.security(self.inspect_container(previous['Id']), self.config.volume, 8000)
        self.cleanup(record)
