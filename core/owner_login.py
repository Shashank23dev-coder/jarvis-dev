"""Owner username/password login: scrypt hash, server-side sessions, lockout and CLI.

Standard library only, so `python3 -m core.owner_login set --env-file PATH` runs on
the host without the application virtualenv. Never logs or prints the username,
password, hash, session id or CSRF token.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import html
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata

ENV_KEY = 'JARVIS_OWNER_LOGIN_HASH'
SESSION_COOKIE = '__Host-jarvis_session'
CSRF_COOKIE = '__Host-jarvis_csrf'
SESSION_SECONDS = 14 * 24 * 3600
MAXMEM = 64 * 1024 * 1024
PRODUCTION_PARAMS = dict(n=2 ** 15, r=8, p=1)
MIN_PASSWORD = 12
LOCK_EVERY, LOCK_FIRST, LOCK_MAX = 5, 60, 900
_FORMAT = re.compile(r'scrypt\$([1-9][0-9]{0,9})\$([1-9][0-9]{0,3})\$([1-9][0-9]{0,3})\$([A-Za-z0-9_-]+)\$([A-Za-z0-9_-]+)')
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')


def _b64encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _b64decode(text):
    data = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if _b64encode(data) != text:
        raise ValueError('Non-canonical base64')
    return data


def parse_hash(value, *, production):
    """Return (n, r, p, salt, digest) or raise ValueError without echoing the value."""
    match = _FORMAT.fullmatch(value or '')
    try:
        if not match:
            raise ValueError
        n, r, p = (int(match.group(i)) for i in (1, 2, 3))
        salt, digest = _b64decode(match.group(4)), _b64decode(match.group(5))
    except (ValueError, TypeError):
        raise ValueError(ENV_KEY + ' must be scrypt$<n>$<r>$<p>$<salt>$<hash>') from None
    if n < 2 or n & (n - 1) or len(salt) < 8 or len(digest) < 16 or len(digest) > 64:
        raise ValueError(ENV_KEY + ' has invalid scrypt parameters')
    if 128 * r * (n + p + 2) > MAXMEM:
        raise ValueError(ENV_KEY + ' exceeds the scrypt memory bound')
    if production and (n < 2 ** 15 or r != 8 or p != 1 or len(salt) != 16 or len(digest) != 32):
        raise ValueError(ENV_KEY + ' must use n>=2**15, r=8, p=1, a 16-byte salt and a 32-byte hash')
    return n, r, p, salt, digest


def _material(username, password):
    user = unicodedata.normalize('NFKC', username.lower())
    return (user + '\0' + unicodedata.normalize('NFKC', password)).encode('utf-8')


def make_hash(username, password, *, n=PRODUCTION_PARAMS['n'], r=PRODUCTION_PARAMS['r'],
              p=PRODUCTION_PARAMS['p'], salt=None):
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.scrypt(_material(username, password), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=32)
    return 'scrypt$%d$%d$%d$%s$%s' % (n, r, p, _b64encode(salt), _b64encode(digest))


def verify(stored, username, password):
    """One scrypt over username and password together, so a wrong username costs
    exactly as much as a wrong password and the result never says which was wrong."""
    try:
        n, r, p, salt, digest = parse_hash(stored, production=False)
        material = _material(username, password)
    except (ValueError, UnicodeError):
        return False
    candidate = hashlib.scrypt(material, salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=len(digest))
    return hmac.compare_digest(candidate, digest)


def fingerprint(stored):
    return hashlib.sha256(stored.encode()).hexdigest()


def lock_seconds(failures):
    if failures < LOCK_EVERY or failures % LOCK_EVERY:
        return 0
    return min(LOCK_FIRST * 2 ** (failures // LOCK_EVERY - 1), LOCK_MAX)


class LoginStore:
    """Sessions and the failure counter live in the existing Jarvis SQLite file, in
    separate tables (core/database.py and its schema version are unchanged)."""
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute('''CREATE TABLE IF NOT EXISTS owner_sessions (
            id_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, login_fingerprint TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS owner_login_failures (
            id INTEGER PRIMARY KEY CHECK(id = 1), failures INTEGER NOT NULL, locked_until REAL NOT NULL)''')
        return connection

    def begin_attempt(self):
        """Atomically refuse while locked, otherwise count this attempt as a failure
        up front (a success resets it), so parallel guesses cannot skip the lock.
        Returns 0 when the attempt may proceed, else the seconds left on the lock."""
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT failures, locked_until FROM owner_login_failures WHERE id = 1').fetchone()
            failures, locked_until = row or (0, 0.0)
            now = self.clock()
            if locked_until > now:
                connection.execute('COMMIT')
                return max(1, int(locked_until - now + 0.999))
            failures += 1
            wait = lock_seconds(failures)
            connection.execute('INSERT OR REPLACE INTO owner_login_failures(id, failures, locked_until) VALUES (1, ?, ?)',
                               (failures, now + wait if wait else 0.0))
            connection.execute('COMMIT')
            return 0
        finally:
            connection.close()

    def failures(self):
        connection = self._connect()
        try:
            row = connection.execute('SELECT failures, locked_until FROM owner_login_failures WHERE id = 1').fetchone()
            return row or (0, 0.0)
        finally:
            connection.close()

    def succeed(self, stored):
        """Reset the counter and create a session; returns the raw session id."""
        session = secrets.token_urlsafe(32)
        now = self.clock()
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM owner_login_failures')
            connection.execute('DELETE FROM owner_sessions WHERE expires_at <= ?', (now,))
            connection.execute('INSERT INTO owner_sessions(id_hash, expires_at, login_fingerprint) VALUES (?, ?, ?)',
                               (hashlib.sha256(session.encode()).hexdigest(), now + SESSION_SECONDS, fingerprint(stored)))
            connection.execute('COMMIT')
        finally:
            connection.close()
        return session

    def valid(self, session, stored):
        if not session or not stored or not _TOKEN.fullmatch(session):
            return False
        connection = self._connect()
        try:
            row = connection.execute('SELECT expires_at, login_fingerprint FROM owner_sessions WHERE id_hash = ?',
                                     (hashlib.sha256(session.encode()).hexdigest(),)).fetchone()
        finally:
            connection.close()
        return bool(row and row[0] > self.clock() and hmac.compare_digest(row[1], fingerprint(stored)))

    def delete(self, session):
        if not session or not _TOKEN.fullmatch(session):
            return
        connection = self._connect()
        try:
            connection.execute('DELETE FROM owner_sessions WHERE id_hash = ?',
                               (hashlib.sha256(session.encode()).hexdigest(),))
        finally:
            connection.close()


def cookies(header):
    result = {}
    for part in header.split(';'):
        name, sep, value = part.strip().partition('=')
        if sep and name not in result:
            result[name] = value.strip()
    return result


def set_cookie(name, value, max_age=None):
    cookie = f'{name}={value}; Path=/; Secure; HttpOnly; SameSite=Strict'
    return cookie + (f'; Max-Age={max_age}' if max_age is not None else '')


LOGIN_CSP = "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"

_PAGE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - Jarvis</title>
<style>
body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: #0b1020; color: #e8ecf8; font: 16px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { width: 100%; max-width: 360px; padding: 24px; box-sizing: border-box; }
h1 { font-size: 24px; margin: 0 0 20px; }
label { display: block; margin: 14px 0 6px; }
input { width: 100%; box-sizing: border-box; min-height: 44px; padding: 10px 12px; font-size: 16px;
  border: 1px solid #39425e; border-radius: 8px; background: #121a30; color: inherit; }
button { width: 100%; min-height: 48px; margin-top: 22px; font-size: 16px; border: 0; border-radius: 8px;
  background: #3b82f6; color: #fff; cursor: pointer; }
.error { color: #fca5a5; margin: 0 0 8px; }
</style>
</head>
<body>
<main>
<h1>Jarvis</h1>
{message}<form method="post" action="/login">
<input type="hidden" name="csrf" value="{csrf}">
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form>
</main>
</body>
</html>
'''


def login_page(csrf, message=''):
    note = f'<p class="error" role="alert">{html.escape(message)}</p>\n' if message else ''
    return _PAGE.replace('{message}', note).replace('{csrf}', html.escape(csrf, quote=True))


def write_env_hash(path, value):
    """Atomically replace only the ENV_KEY line in a private (0600, non-symlink) env file."""
    path = Path(path)
    if '\n' in value or '\r' in value:
        raise ValueError('Invalid hash')
    if os.path.islink(path):
        raise ValueError('Refusing a symlinked env file')
    # O_NOFOLLOW + fstat: the checked file is the file that is read.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Env file must be a regular file with mode 600')
        lines = stream.read().decode('utf-8').splitlines(keepends=True)
    entry = ENV_KEY + '=' + value + '\n'
    pattern = re.compile(r'\s*(?:export\s+)?' + ENV_KEY + r'\s*=')
    output, placed = [], False
    for line in lines:
        if pattern.match(line):
            if not placed:
                output.append(entry)
                placed = True
        else:
            output.append(line)
    if not placed:
        if output and not output[-1].endswith('\n'):
            output[-1] += '\n'
        output.append(entry)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(''.join(output).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None, *, ask=input, secret=getpass.getpass, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog='python3 -m core.owner_login')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('set', help='Set the owner username and password hash')
    command.add_argument('--env-file', required=True)
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    try:
        info = os.lstat(path)
    except OSError:
        print('Env file not found.', file=err)
        return 1
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        print('Refusing: env file must be a regular, non-symlinked file with mode 600.', file=err)
        return 1
    username = ask('Username: ')
    if not username.strip() or any(ord(c) < 32 for c in username):
        print('Username must not be empty.', file=err)
        return 1
    password = secret('Password: ')
    if len(password) < MIN_PASSWORD:
        print(f'Password must be at least {MIN_PASSWORD} characters.', file=err)
        return 1
    if any(ord(c) < 32 for c in password):
        print('Password must not contain control characters.', file=err)
        return 1
    if not hmac.compare_digest(password.encode(), secret('Repeat password: ').encode()):
        print('Passwords do not match.', file=err)
        return 1
    value = make_hash(username, password)
    parse_hash(value, production=True)
    try:
        write_env_hash(path, value)
    except (OSError, ValueError, UnicodeError):
        print('Could not write the env file; nothing was changed.', file=err)
        return 1
    print('Login hash saved.', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
