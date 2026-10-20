"""Headless Chromium release check. Runs only inside the throwaway runner container,
sharing the throwaway app container's network namespace (loopback only).
Writes /out/result.json plus per-viewport screenshots and console logs."""
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request

# Like production (Caddy), the browser talks HTTPS; a local TLS relay with a
# per-run self-signed cert forwards raw bytes to the app on loopback :8000.
# Secure/__Host- cookies need a secure origin, so plain HTTP cannot test login.
BASE = 'https://localhost:8443'
DIRECT = ('127.0.0.1', 8000)
OUT = '/out'
SESSION = '__Host-jarvis_session'
SEEDS = 15  # 15 user + 15 assistant = 30 restored messages
HELLO_REPLY = 'Hello! Jarvis browser check reply'
SHAPE = {'id', 'conversation_id', 'goal', 'status', 'created_at', 'response', 'steps', 'agent', 'activity'}
VIEWPORTS = (('phone-390x664', 390, 664), ('desktop-1440x900', 1440, 900))
STARTED = time.monotonic()
DEADLINE = STARTED + float(os.environ.get('BCHECK_DEADLINE', '100'))
TOKEN = os.environ['JARVIS_AUTH_TOKEN']
USERNAME = os.environ['BCHECK_USERNAME']
PASSWORD = os.environ['BCHECK_PASSWORD']

METRICS = '''() => {
  const m = document.getElementById('messages');
  const c = document.querySelector('.composer');
  const r = c ? c.getBoundingClientRect() : null;
  const s = document.scrollingElement;
  return {sh: m.scrollHeight, ch: m.clientHeight, st: m.scrollTop,
          doc_h: s.scrollHeight, win_h: innerHeight, win_w: innerWidth, scroll_y: scrollY,
          composer: r && {top: r.top, bottom: r.bottom, left: r.left, right: r.right}};
}'''
AT_BOTTOM = "() => { const m = document.getElementById('messages'); return m.scrollHeight - m.scrollTop - m.clientHeight <= 2; }"


def left(cap=30.0):
    remaining = DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('browser check budget exhausted')
    return min(cap, remaining)


def relay():
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-keyout', '/tmp/relay.key', '-out', '/tmp/relay.crt'],
                   check=True, capture_output=True, timeout=30)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/tmp/relay.crt', '/tmp/relay.key')
    server = socket.create_server(('127.0.0.1', 8443))

    def pipe(source, target):
        try:
            while data := source.recv(65536):
                target.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, target):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(client):
        try:
            tls = context.wrap_socket(client, server_side=True)
            upstream = socket.create_connection(DIRECT, timeout=60)
        except OSError:
            client.close()
            return
        threading.Thread(target=pipe, args=(tls, upstream), daemon=True).start()
        pipe(upstream, tls)

    def serve():
        while True:
            client, _ = server.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()


def http(method, path, body=None, headers=None):
    headers = dict(headers or {}, Host='localhost:8443')
    request = urllib.request.Request('http://%s:%d' % DIRECT + path, method=method, data=body, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=left(30)) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def bearer(method, path, payload=None):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode()
    status, raw = http(method, path, body, headers)
    return status, (json.loads(raw) if raw else None)


def wait_ready():
    while True:
        try:
            status, _ = http('GET', '/health')
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        left()
        time.sleep(0.25)


def seed(result):
    conversation, shapes = None, []
    for number in range(1, SEEDS + 1):
        payload = {'goal': f'BCSEED-{number} format a sample answer', 'mode': 'agent'}
        if conversation:
            payload['conversation_id'] = conversation
        status, data = bearer('POST', '/task', payload)
        if status != 200:
            raise RuntimeError(f'seed /task returned {status}')
        conversation = data['conversation_id']
        shapes.append(sorted(SHAPE - set(data)))
    status, history = bearer('GET', f'/conversations/{conversation}/messages')
    count = len(history['messages']) if status == 200 else None
    agent = data.get('agent') or {}
    result['seed'] = dict(messages=count, reply_shape_missing=shapes[-1],
                          agent_keys=sorted(agent) if isinstance(agent, dict) else None,
                          steps_is_list=isinstance(data.get('steps'), list),
                          activity_is_list=isinstance(data.get('activity'), list))
    if count != 2 * SEEDS or shapes[-1] or not {'iterations', 'tool_calls', 'stop_reason'} <= set(agent):
        raise RuntimeError('seeded history or /task reply shape is wrong')
    return conversation


def viewport(browser, name, width, height, conversation):
    checks, console, page_errors, external = {}, [], [], []
    shots = []

    def check(key, ok, detail=None):
        checks[key] = dict(passed=bool(ok), **({'detail': detail} if detail is not None else {}))

    def shot(page, label):
        path = f'{OUT}/{name}-{len(shots) + 1}-{label}.png'
        page.screenshot(path=path)
        shots.append(os.path.basename(path))

    phone = width < 600
    context = browser.new_context(viewport={'width': width, 'height': height}, reduced_motion='reduce',
                                  is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1,
                                  ignore_https_errors=True)
    # No network exists; answer third-party requests (web fonts) locally and record them.
    def offline(route):
        external.append(route.request.url.split('?')[0][:120])
        route.fulfill(status=200, body='', content_type='text/css')
    context.route(lambda url: not url.startswith(BASE + '/'), offline)
    context.add_init_script('try { if (!localStorage.getItem("jarvis.conversationId")) '
                            'localStorage.setItem("jarvis.conversationId", %s); } catch (_) {}' % json.dumps(conversation))
    page = context.new_page()
    page.set_default_timeout(10_000)
    page.on('console', lambda m: console.append({'type': m.type, 'text': m.text[:500],
                                                  'at': round(time.monotonic() - STARTED, 2)}))
    page.on('pageerror', lambda e: page_errors.append(str(e)[:500]))
    try:
        response = page.goto(BASE + '/', timeout=left(15) * 1000)
        first = response.request.redirected_from
        check('root_redirects_to_login', first is not None and first.url == BASE + '/' and
              first.response().status == 303 and page.url == BASE + '/login',
              dict(final=page.url, status=first.response().status if first else None))
        shot(page, 'login')

        page.fill('#username', USERNAME)
        page.fill('#password', PASSWORD)
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/login') as info:
            page.click('button[type=submit]')
        check('login_post_303', info.value.status == 303, info.value.status)
        page.wait_for_url(BASE + '/', timeout=left(10) * 1000)
        cookie = next((c for c in context.cookies() if c['name'] == SESSION), None)
        status = page.evaluate("fetch('/runtime', {cache: 'no-store'}).then(r => r.status)")
        check('login_session', bool(cookie and cookie['httpOnly'] and status == 200),
              dict(cookie=bool(cookie), runtime_status=status))

        page.wait_for_function("document.querySelectorAll('#messages .message-row').length >= 30",
                               timeout=left(15) * 1000)
        formatted = page.evaluate('''() => { const m = document.getElementById('messages'); return {
            rows: m.querySelectorAll('.message-row').length, headings: m.querySelectorAll('h1,h2,h3,h4').length,
            lists: m.querySelectorAll('ul li, ol li').length, code: m.querySelectorAll('pre').length,
            bold: m.querySelectorAll('strong, b').length}; }''')
        check('history_restored', formatted['rows'] >= 30, formatted)
        check('history_formatted', all(formatted[k] > 0 for k in ('headings', 'lists', 'code', 'bold')), formatted)
        try:
            page.wait_for_function(AT_BOTTOM, timeout=3000)
        except Exception:
            pass
        before = page.evaluate(METRICS)
        check('history_scrolls_inside_messages', before['sh'] > before['ch'] + 20, before)
        check('history_at_bottom', before['sh'] - before['st'] - before['ch'] <= 2, before)
        check('history_page_not_scrolling', before['doc_h'] <= before['win_h'] + 1 and before['scroll_y'] == 0, before)
        shot(page, 'history')

        page.fill('#prompt', 'hello')
        page.click('#sendBtn')
        page.wait_for_function("[...document.querySelectorAll('#messages .jarvis-message')]"
                               ".some(e => e.textContent.trim() === 'Thinking...')", timeout=5000)
        check('thinking_shown', True)
        page.wait_for_function('''(reply) => { const m = [...document.querySelectorAll('#messages .jarvis-message')];
            return m.length && !m.some(e => e.textContent.trim() === 'Thinking...') &&
                   m[m.length - 1].textContent.includes(reply); }''', arg=HELLO_REPLY, timeout=left(20) * 1000)
        check('reply_replaced_thinking', True)
        try:
            page.wait_for_function(AT_BOTTOM, timeout=3000)
        except Exception:
            pass
        after = page.evaluate(METRICS)
        composer = after['composer']
        check('reply_at_bottom', after['sh'] - after['st'] - after['ch'] <= 2, after)
        check('reply_auto_scrolled', after['st'] > before['st'], dict(before=before['st'], after=after['st']))
        check('page_not_scrolling', after['doc_h'] <= after['win_h'] + 1 and after['scroll_y'] == 0, after)
        check('composer_in_viewport', bool(composer) and composer['top'] >= 0 and composer['left'] >= 0 and
              composer['bottom'] <= after['win_h'] and composer['right'] <= after['win_w'], composer)
        shot(page, 'reply')

        page.evaluate("() => { const m = document.getElementById('messages'); m.scrollTop = 0;"
                      " m.dispatchEvent(new Event('scroll')); }")
        page.wait_for_selector('#jumpLatest', state='visible', timeout=3000)
        check('jump_latest_shown', True)
        shot(page, 'jump')
        page.click('#jumpLatest')
        page.wait_for_function(AT_BOTTOM, timeout=3000)
        jumped = page.evaluate('''() => ({hidden: document.getElementById('jumpLatest').hidden,
            focus_in_messages: document.getElementById('messages').contains(document.activeElement)})''')
        check('jump_latest_returns_to_bottom', jumped['hidden'], jumped)
        check('jump_latest_focuses_message', jumped['focus_in_messages'], jumped)

        cross = context.request.post(BASE + '/task', data=json.dumps({'goal': 'cross-site probe', 'mode': 'agent'}),
                                     headers={'Origin': 'https://attacker.example', 'Sec-Fetch-Site': 'cross-site',
                                              'Content-Type': 'application/json'}, max_redirects=0)
        check('cross_site_task_403', cross.status == 403, cross.status)
        null_login = context.request.post(BASE + '/login', data='username=x&password=y&csrf=z',
                                          headers={'Origin': 'null', 'Content-Type': 'application/x-www-form-urlencoded'},
                                          max_redirects=0)
        check('null_origin_login_403', null_login.status == 403, null_login.status)

        session = cookie['value'] if cookie else ''
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/logout') as info:
            page.click('.logout-button')
        page.wait_for_url(BASE + '/login', timeout=left(10) * 1000)
        revoked, _ = http('GET', '/runtime', headers={'Cookie': f'{SESSION}={session}'})
        check('logout', info.value.status == 303 and revoked == 401 and
              not any(c['name'] == SESSION for c in context.cookies()),
              dict(status=info.value.status, old_session_status=revoked))
        shot(page, 'logout')
    except Exception as exc:
        check('flow_completed', False, f'{type(exc).__name__}: {str(exc)[:400]}')
        try:
            shot(page, 'failure')
        except Exception:
            pass
    finally:
        errors = [c for c in console if c['type'] == 'error']
        check('no_page_errors', not page_errors, page_errors[:5])
        check('no_console_errors', not errors, errors[:5])
        with open(f'{OUT}/{name}-console.json', 'w') as stream:
            json.dump(dict(console=console, page_errors=page_errors, stubbed_external=external), stream, indent=1)
        context.close()
    return dict(viewport=[width, height], passed=all(c['passed'] for c in checks.values()), checks=checks,
                screenshots=shots, console_log=f'{name}-console.json', stubbed_external_requests=len(external))


def main():
    result = dict(passed=False, viewports={}, failures=[])
    try:
        relay()
        wait_ready()
        result['ready_seconds'] = round(time.monotonic() - STARTED, 2)
        conversation = seed(result)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            result['browser_version'] = browser.version
            for name, width, height in VIEWPORTS:
                left()
                result['viewports'][name] = viewport(browser, name, width, height, conversation)
            browser.close()
        for name, data in result['viewports'].items():
            result['failures'] += [f'{name}:{key}' for key, c in data['checks'].items() if not c['passed']]
        result['passed'] = len(result['viewports']) == len(VIEWPORTS) and not result['failures']
    except Exception as exc:
        result['failures'].append(f'setup:{type(exc).__name__}: {str(exc)[:300]}')
        result['trace'] = traceback.format_exc()[-1500:]
    result['runner_seconds'] = round(time.monotonic() - STARTED, 2)
    with open(f'{OUT}/result.json', 'w') as stream:
        json.dump(result, stream, indent=1)


if __name__ == '__main__':
    main()
