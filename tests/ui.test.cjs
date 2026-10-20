// Dependency-free behavior checks for the inline UI script using a minimal DOM.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

class Node {
    constructor(tag = 'div') {
        this.tagName = tag;
        this.children = [];
        this.style = {};
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.value = '';
        this.textContent = '';
        this.classList = { toggle() {}, add() {} };
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this.scrolls = [];
    }
    // Like the DOM: textContent includes descendants, and setting it replaces them.
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.ownText = String(value); this.children = []; }
    scrollTo(options) {
        this.scrolls.push(options);
        this.scrollTop = Math.max(0, Math.min(options.top, this.scrollHeight - this.clientHeight));
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(key, callback) { this.listeners[key] = callback; }
    focus(options) { this.focused = true; this.focusOptions = options; Node.activeElement = this; }
}
function setup(fetch, savedConversation = null, globals = {}) {
    const nodes = {};
    for (const match of html.matchAll(/id="([^"]+)"/g)) nodes[match[1]] = new Node();
    const nav = [...html.matchAll(/data-view="([^"]+)"/g)].map(match => {
        const node = new Node('button'); node.dataset.view = match[1]; return node;
    });
    const document = {
        get activeElement() { return Node.activeElement; },
        getElementById: id => nodes[id],
        createElement: tag => new Node(tag),
        createTextNode: value => { const node = new Node('#text'); node.textContent = value; return node; },
        querySelectorAll: selector => selector === '[data-view]' ? nav : []
    };
    const saved = new Map(savedConversation ? [['jarvis.conversationId', savedConversation]] : []);
    const localStorage = {
        getItem: key => saved.get(key) || null,
        setItem: (key, value) => saved.set(key, value),
        removeItem: key => saved.delete(key)
    };
    // The plus-point card's startup request goes to its own handler so other tests see only their endpoints.
    const { experiences = async () => ({ ok: false }), ...rest } = globals;
    const route = (url, options) => String(url).startsWith('/experiences') ? experiences(url, options) : fetch(url, options);
    const context = vm.createContext({ document, fetch: route, AbortController, localStorage,
        setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {}, ...rest });
    vm.runInContext(script, context);
    return { nodes, nav, saved, run: code => vm.runInContext(code, context) };
}
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);
const find = (node, tag) => descendants(node).filter(child => child.tagName === tag);
// Gives the conversation log a height that grows with its text and a 200px viewport.
function layout(ui) {
    const log = ui.nodes.messages;
    Object.defineProperty(log, 'scrollHeight', { get: () => log.textContent.length * 10 });
    log.clientHeight = 200;
    return log;
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('UI contract keeps every view, navigation button, and runtime status label', () => {
    const viewIds = ['assistantView', 'tasksView', 'memoryView', 'agentsView', 'toolsView', 'activityView'];
    for (const id of viewIds) assert.match(html, new RegExp(`id="${id}"`));
    const navViews = [...html.matchAll(/<button\b[^>]*data-view="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(navViews, ['assistant', 'tasks', 'memory', 'agents', 'tools', 'activity']);
    for (const view of navViews) assert.match(html, new RegExp(`<button\\b[^>]*data-view="${view}"`));
    assert.match(html, /id="runtimeStatus"[\s\S]*?>\s*Unavailable\s*</);
});

test('all six views navigate and preserve chat and draft', () => {
    const ui = setup(async () => ({ ok: true, json: async () => ({ tasks: [], activity: [] }) }));
    ui.run('addMessage("user", "Keep this conversation")');
    ui.nodes.prompt.value = 'Unsent draft';
    for (const nav of ui.nav) {
        nav.listeners.click();
        for (const other of ui.nav) assert.equal(ui.nodes[other.dataset.view + 'View'].hidden, other !== nav);
        assert.equal(nav.attributes['aria-current'], 'page');
    }
    ui.run('showView("assistant")');
    assert.match(text(ui.nodes.messages), /Keep this conversation/);
    assert.equal(ui.nodes.prompt.value, 'Unsent draft');
});

test('runtime status displays development, production, and unavailable states', async () => {
    for (const [response, label] of [
        [{ ok: true, json: async () => ({ mode: 'development' }) }, 'Development'],
        [{ ok: true, json: async () => ({ mode: 'production' }) }, 'Production'],
        [{ ok: false }, 'Unavailable'],
        [null, 'Unavailable']
    ]) {
        const ui = setup(async url => {
            assert.equal(url, '/runtime');
            if (response === null) throw new Error('offline');
            return response;
        });
        await tick();
        assert.equal(ui.nodes.runtimeStatus.textContent, label);
    }
});

test('tasks load safely, include plan details, and support empty/error/retry', async () => {
    let payload = { tasks: [{ goal: '<img src=x onerror=alert(1)>', status: 'planned', created_at: '2026-09-24T12:00:00', steps: [{ description: 'Inspect', agent: 'reviewer' }] }] };
    let ok = true;
    const calls = [];
    const ui = setup(async url => { calls.push(url); return { ok, json: async () => payload }; });
    await ui.run('loadView("tasks")');
    assert.deepEqual(calls.filter(url => url !== '/runtime'), ['/tasks']);
    assert.match(text(ui.nodes.tasksContent), /<img src=x onerror=alert\(1\)>/);
    assert.match(text(ui.nodes.tasksContent), /Inspect \[reviewer\]/);
    assert.equal(ui.nodes.tasksContent.children[0].children[0].children[0].children[0].children.length, 0);
    payload = { tasks: [] };
    await ui.run('loadView("tasks")');
    assert.match(text(ui.nodes.tasksContent), /No tasks yet/);
    ok = false;
    await ui.run('loadView("tasks")');
    assert.match(text(ui.nodes.tasksContent), /Unable to load tasks/);
    ok = true;
    await ui.nodes.tasksContent.children[0].children[2].listeners.click();
    assert.match(text(ui.nodes.tasksContent), /No tasks yet/);
});

test('activity loads its endpoint and renders event context', async () => {
    const ui = setup(async url => {
        if (url === '/runtime') return { ok: true, json: async () => ({ mode: 'development' }) };
        assert.equal(url, '/activity');
        return { ok: true, json: async () => ({ activity: [{ message: 'Planning completed', type: 'completed', goal: 'Build Jarvis', timestamp: '2026-09-24T12:00:00' }] }) };
    });
    await ui.run('loadView("activity")');
    assert.match(text(ui.nodes.activityContent), /Planning completed.*Build Jarvis/);
});

test('refresh aborts obsolete requests without replacing the latest results', async () => {
    const pending = [];
    const ui = setup((url, options) => url === '/runtime'
        ? Promise.resolve({ ok: false })
        : new Promise(resolve => pending.push({ resolve, options })));
    const first = ui.run('loadView("tasks")');
    const second = ui.run('loadView("tasks")');
    assert.equal(pending[0].options.signal.aborted, true);
    pending[1].resolve({ ok: true, json: async () => ({ tasks: [] }) });
    await second;
    pending[0].resolve({ ok: true, json: async () => ({ tasks: [{ goal: 'Stale' }] }) });
    await first;
    assert.match(text(ui.nodes.tasksContent), /No tasks yet/);
});

test('chat submits once while pending, renders backend steps and refreshes the visible list', async () => {
    let resolveTask;
    const calls = [];
    const ui = setup((url, options) => {
        calls.push(url);
        if (url === '/task') {
            assert.equal(JSON.parse(options.body).goal, 'Build Jarvis');
            return new Promise(resolve => { resolveTask = resolve; });
        }
        return Promise.resolve({ ok: true, json: async () => ({ tasks: [] }) });
    });
    ui.nodes.prompt.value = 'Build Jarvis';
    const sending = ui.run('sendTask()');
    ui.nodes.prompt.value = 'Next draft';
    await ui.run('sendTask()');
    assert.deepEqual(calls.filter(url => url !== '/runtime'), ['/task']);
    ui.run('showView("tasks")');
    resolveTask({ ok: true, json: async () => ({ goal: 'Build Jarvis', steps: [{ id: 1, description: 'Inspect code', agent: 'reviewer' }] }) });
    await sending;
    await tick();
    assert.deepEqual(find(ui.nodes.messages, 'li').map(item => item.textContent), ['Inspect code [reviewer]']);
    assert.equal(ui.nodes.prompt.value, 'Next draft');
    assert.equal(ui.nodes.sendBtn.disabled, false);
    assert.equal(ui.nodes.prompt.focused, undefined);
    assert.deepEqual(calls.filter(url => url !== '/runtime'), ['/task', '/tasks', '/tasks']);
});

test('network errors recover chat controls and display list retry', async () => {
    const ui = setup(async () => { throw new Error('offline'); });
    ui.nodes.prompt.value = 'A goal';
    await ui.run('sendTask()');
    assert.match(text(ui.nodes.messages), /Unable to reach Jarvis backend/);
    assert.equal(ui.nodes.sendBtn.disabled, false);
    await ui.run('loadView("activity")');
    assert.match(text(ui.nodes.activityContent), /Unable to load activity.*Try again/);
});

test('Assistant requests agent mode and renders the final answer as plain text', async () => {
    const ui = setup(async (url, options) => {
        if (url === '/runtime') return { ok: true, json: async () => ({ mode: 'development' }) };
        assert.equal(url, '/task');
        assert.deepEqual(JSON.parse(options.body), { goal: 'Inspect files', mode: 'agent' });
        return { ok: true, json: async () => ({ goal: 'Inspect files', steps: [], status: 'completed',
            response: 'Inspected two files. <img src=x onerror=alert(1)>',
            agent: { iterations: 3, tool_calls: 2, stop_reason: 'final_response' } }) };
    });
    ui.nodes.prompt.value = 'Inspect files';
    await ui.run('sendTask()');
    assert.match(text(ui.nodes.messages), /Inspected two files\. <img src=x onerror=alert\(1\)>/);
    assert.deepEqual(descendants(ui.nodes.messages.children[1].children[0]).map(node => node.tagName), ['p', '#text']);
    assert.equal(ui.nodes.sendBtn.disabled, false);
});

test('tool activity uses the existing Activity cards', async () => {
    const ui = setup(async () => ({ ok: true, json: async () => ({ activity: [{
        message: 'read_text: Returned 20 text characters; truncated=False.', type: 'tool_completed',
        goal: 'Inspect files', task_id: 'task-1', tool_name: 'read_text', duration_ms: 4,
        timestamp: '2026-09-25T12:00:00', started_at: '2026-09-25T12:00:00'
    }] }) }));
    await ui.run('loadView("activity")');
    assert.match(text(ui.nodes.activityContent), /read_text: Returned 20 text characters/);
    assert.match(text(ui.nodes.activityContent), /tool_completed.*Inspect files/);
});

for (const [goal, answer] of [
    ['Inspect your own repository, tell me what major components exist, and run the test suite. Summarize the result.',
     'Components: FastAPI, agent loop, repository tools, UI. Pytest exit code 0: 7 passed.'],
    ['Hello!', 'Hello! How can I help?'],
    ['Inspect my trading bot and tell me the biggest weaknesses.',
     'I inspected the Trading Bot source. Live runtime behavior remains unknown.'],
    ['What parts of CreatorOS are still unknown?',
     'Its repository location, runtime and implemented features remain unknown.'],
    ['Check Jarvis itself and tell me what capability is currently unavailable.',
     'Source inspection is available. Engineering provider usability is unknown.'],
    ['Deploy the latest Jarvis build.',
     'Deployment requires a verified preview and your approval for the exact commit.'],
    ['Show me secrets from .env.',
     'I cannot reveal secrets from .env.']
]) {
    test('Assistant submits and displays the answer for: ' + goal, async () => {
        const ui = setup(async (url, options) => {
            if (url === '/runtime') return { ok: true, json: async () => ({ mode: 'development' }) };
            assert.equal(url, '/task');
            assert.equal(options.method, 'POST');
            assert.deepEqual(JSON.parse(options.body), { goal, mode: 'agent' });
            return { ok: true, json: async () => ({ goal, status: 'completed', steps: [], response: answer }) };
        });
        ui.nodes.prompt.value = goal;
        await ui.run('sendTask()');
        assert.equal(ui.nodes.messages.children[1].children[0].textContent, answer);
    });
}

test('conversation ID persists and subsequent requests continue it', async () => {
    const payloads = [];
    const ui = setup(async (url, options) => {
        if (url === '/runtime') return { ok: true, json: async () => ({ mode: 'development' }) };
        payloads.push(JSON.parse(options.body));
        return { ok: true, json: async () => ({ conversation_id: 'conversation-1', response: 'Hello' }) };
    });
    ui.nodes.prompt.value = 'First';
    await ui.run('sendTask()');
    assert.equal(ui.saved.get('jarvis.conversationId'), 'conversation-1');
    ui.nodes.prompt.value = 'Second';
    await ui.run('sendTask()');
    assert.equal(payloads[1].conversation_id, 'conversation-1');
});

test('reload restores ordered history as plain text without changing the draft', async () => {
    const ui = setup(async url => {
        if (url === '/runtime') return { ok: true, json: async () => ({ mode: 'development' }) };
        assert.equal(url, '/conversations/conversation-1/messages');
        return { ok: true, json: async () => ({ messages: [
            { role: 'user', content: '<script>history</script>' },
            { role: 'assistant', content: 'Saved answer' }
        ] }) };
    }, 'conversation-1');
    ui.nodes.prompt.value = 'Draft';
    await tick();
    assert.equal(ui.nodes.messages.children.length, 2);
    assert.equal(ui.nodes.messages.children[0].children[0].textContent, '<script>history</script>');
    assert.equal(ui.nodes.messages.children[0].children[0].children.length, 0);
    assert.equal(ui.nodes.prompt.value, 'Draft');
    assert.equal(ui.nodes.sendBtn.disabled, false);
});

test('missing conversation clears stored ID, but temporary outages retain it', async () => {
    const missing = setup(async () => ({ ok: false, status: 404 }), 'missing');
    await tick();
    assert.equal(missing.saved.has('jarvis.conversationId'), false);
    const offline = setup(async () => { throw new Error('offline'); }, 'keep');
    await tick();
    assert.equal(offline.saved.get('jarvis.conversationId'), 'keep');
    assert.equal(offline.nodes.sendBtn.disabled, false);
});

test('core orb is a polite status region and shows every state', () => {
    assert.match(html, /id="coreOrb"[^>]*role="status"[^>]*aria-live="polite"/);
    const ui = setup(async () => ({ ok: false }));
    for (const [name, label] of [['listening', 'LISTENING'], ['thinking', 'THINKING'], ['working', 'WORKING'],
        ['needs_you', 'NEEDS YOU'], ['done', 'DONE'], ['idle', 'STANDING BY']]) {
        ui.run(`setCoreState(${JSON.stringify(name)})`);
        assert.equal(ui.nodes.coreOrb.className, 'core ' + name);
        assert.equal(ui.nodes.coreLabel.textContent, label);
    }
});

test('core orb ignores unknown state names', () => {
    const ui = setup(async () => ({ ok: false }));
    ui.run('setCoreState("working")');
    for (const name of ['"exploding"', '"toString"', '""', 'undefined']) {
        ui.run(`setCoreState(${name})`);
        assert.equal(ui.nodes.coreOrb.className, 'core working');
        assert.equal(ui.nodes.coreLabel.textContent, 'WORKING');
    }
});

test('core orb is idle on load, thinking while pending and done on answer', async () => {
    let resolveTask;
    const ui = setup(url => url === '/task'
        ? new Promise(resolve => { resolveTask = resolve; })
        : Promise.resolve({ ok: false }));
    assert.equal(ui.nodes.coreOrb.className, 'core idle');
    assert.equal(ui.nodes.coreLabel.textContent, 'STANDING BY');
    ui.nodes.prompt.value = 'Hello';
    const sending = ui.run('sendTask()');
    assert.equal(ui.nodes.coreOrb.className, 'core thinking');
    assert.equal(ui.nodes.coreLabel.textContent, 'THINKING');
    resolveTask({ ok: true, json: async () => ({ response: 'Hi there' }) });
    await sending;
    assert.equal(ui.nodes.coreOrb.className, 'core done');
    assert.equal(ui.nodes.coreLabel.textContent, 'DONE');
});

test('core orb returns to idle after a network error', async () => {
    const ui = setup(async () => { throw new Error('offline'); });
    ui.nodes.prompt.value = 'Hello';
    const sending = ui.run('sendTask()');
    assert.equal(ui.nodes.coreOrb.className, 'core thinking');
    await sending;
    assert.equal(ui.nodes.coreOrb.className, 'core idle');
    assert.equal(ui.nodes.coreLabel.textContent, 'STANDING BY');
});

// Fake timers: run() fires every pending callback scheduled for at most the given delay.
function timers() {
    const pending = new Map();
    let next = 1;
    return {
        pending,
        setTimeout: (callback, delay) => { pending.set(next, { callback, delay }); return next++; },
        clearTimeout: id => pending.delete(id),
        run: upTo => { for (const [id, timer] of [...pending]) if (timer.delay <= upTo) { pending.delete(id); timer.callback(); } }
    };
}

test('core orb returns to idle 5 seconds after an answer', async () => {
    const clock = timers();
    const ui = setup(async () => ({ ok: true, json: async () => ({ response: 'Hi there' }) }), null, clock);
    ui.nodes.prompt.value = 'Hello';
    await ui.run('sendTask()');
    assert.equal(ui.nodes.coreOrb.className, 'core done');
    clock.run(4999);
    assert.equal(ui.nodes.coreOrb.className, 'core done');
    clock.run(5000);
    assert.equal(ui.nodes.coreOrb.className, 'core idle');
    assert.equal(ui.nodes.coreLabel.textContent, 'STANDING BY');
});

test('core orb stays busy when a new request starts before the idle return', async () => {
    const clock = timers();
    let resolveSecond;
    let calls = 0;
    const ui = setup(url => url !== '/task' ? Promise.resolve({ ok: false })
        : ++calls === 1 ? Promise.resolve({ ok: true, json: async () => ({ response: 'First' }) })
        : new Promise(resolve => { resolveSecond = resolve; }), null, clock);
    ui.nodes.prompt.value = 'First';
    await ui.run('sendTask()');
    assert.equal(ui.nodes.coreOrb.className, 'core done');
    ui.nodes.prompt.value = 'Second';
    const sending = ui.run('sendTask()');
    clock.run(5000);
    assert.equal(ui.nodes.coreOrb.className, 'core thinking');
    resolveSecond({ ok: true, json: async () => ({ response: 'Second' }) });
    await sending;
    assert.equal(ui.nodes.coreOrb.className, 'core done');
});

test('composer is labelled and the Send hover colour keeps its contrast', () => {
    assert.match(html, /<label\b[^>]*for="prompt"[^>]*>ASK JARVIS ANYTHING<\/label>/);
    assert.match(html, /\.send-button:hover\s*\{[^}]*background:\s*#065E6F/);
});

test('plus-point card loads the latest experience and shows its lesson as plain text', async () => {
    const urls = [];
    const lesson = 'Run the parser tests first. <img src=x onerror=alert(1)>';
    const ui = setup(async () => ({ ok: false }), null, { experiences: async url => {
        urls.push(url);
        return { ok: true, json: async () => [{ id: 'e1', reusable_lesson: lesson }] };
    } });
    await tick();
    assert.deepEqual(urls, ['/experiences?limit=1']);
    assert.equal(ui.nodes.plusPoint.hidden, false);
    assert.equal(ui.nodes.plusPointLesson.textContent, lesson);
    assert.equal(ui.nodes.plusPointLesson.children.length, 0);
    assert.match(html, /id="plusPoint"[^>]*hidden/);
    assert.match(html, /LATEST PLUS POINT/);
});

test('plus-point card hides on an empty list and on errors', async () => {
    for (const experiences of [
        async () => ({ ok: true, json: async () => [] }),
        async () => ({ ok: false, status: 500 }),
        async () => { throw new Error('offline'); }
    ]) {
        const ui = setup(async () => ({ ok: false }), null, { experiences });
        await tick();
        assert.equal(ui.nodes.plusPoint.hidden, true);
    }
});

test('greeting and date line follow the local hour', () => {
    const ui = setup(async () => ({ ok: false }));
    for (const [hour, greeting] of [[9, 'Good morning, Shashank.'], [14, 'Good afternoon, Shashank.'], [20, 'Good evening, Shashank.']]) {
        ui.run(`renderGreeting(new Date(2026, 9, 19, ${hour}, 5))`);
        assert.equal(ui.nodes.coreGreeting.textContent, greeting);
        assert.match(ui.nodes.coreTime.textContent, /19.*·.*05/);
    }
});

test('failed task retains conversation ID from response headers', async () => {
    const ui = setup(async () => ({ ok: false,
        headers: { get: () => 'failed-conversation' },
        json: async () => ({ detail: 'Jarvis could not complete the agent request.' }) }));
    ui.nodes.prompt.value = 'Try';
    await ui.run('sendTask()');
    assert.equal(ui.saved.get('jarvis.conversationId'), 'failed-conversation');
});

test('Jarvis messages render headings, bold, italic, lists and code as Markdown', () => {
    const ui = setup(async () => ({ ok: false }));
    const message = ui.run(`addMessage("jarvis", ${JSON.stringify(
        '# Summary\n\nThis is **bold**, *italic* and `npm test`.\n\n- first\n- second\n\n3. third\n4. fourth\n\n```js\nconst tag = "<b>";\n```')})`);
    assert.equal(find(message, 'h3')[0].textContent, 'Summary');
    assert.equal(find(message, 'strong')[0].textContent, 'bold');
    assert.equal(find(message, 'em')[0].textContent, 'italic');
    assert.deepEqual(find(message, 'ul')[0].children.map(item => item.textContent), ['first', 'second']);
    const ordered = find(message, 'ol')[0];
    assert.equal(ordered.start, 3);
    assert.deepEqual(ordered.children.map(item => item.textContent), ['third', 'fourth']);
    assert.equal(find(message, 'pre')[0].children[0].tagName, 'code');
    assert.equal(find(message, 'pre')[0].textContent, 'const tag = "<b>";');
    assert.deepEqual(find(message, 'code').map(code => code.textContent), ['npm test', 'const tag = "<b>";']);
    assert.equal(ui.nodes.messages.children[0].children[1].className, 'jarvis-avatar');
});

test('an unclosed code fence renders the rest of the message as code', () => {
    const ui = setup(async () => ({ ok: false }));
    const message = ui.run(`addMessage("jarvis", ${JSON.stringify(
        'Run this:\n\n```js\nconst a = 1;\n```js\n\nconsole.log(a);')})`);
    assert.equal(find(message, 'p')[0].textContent, 'Run this:');
    const blocks = find(message, 'pre');
    assert.equal(blocks.length, 1);
    assert.equal(blocks[0].children[0].tagName, 'code');
    assert.equal(blocks[0].textContent, 'const a = 1;\n```js\n\nconsole.log(a);');
    assert.equal(message.textContent, 'Run this:const a = 1;\n```js\n\nconsole.log(a);');
});

test('HTML in Jarvis messages stays inert text and links stay plain', () => {
    assert.doesNotMatch(script, /innerHTML/);
    const ui = setup(async () => ({ ok: false }));
    const message = ui.run(`addMessage("jarvis", ${JSON.stringify(
        '<script>alert(1)</script> and <img src=x onerror=alert(1)> [docs](javascript:alert(1))')})`);
    assert.deepEqual(descendants(message).map(node => node.tagName), ['p', '#text']);
    assert.equal(message.textContent, '<script>alert(1)</script> and <img src=x onerror=alert(1)> [docs](javascript:alert(1))');
});

test('owner messages stay plain text', () => {
    const ui = setup(async () => ({ ok: false }));
    const message = ui.run('addMessage("user", "# Not a heading **not bold** <b>x</b>")');
    assert.equal(message.children.length, 0);
    assert.equal(message.textContent, '# Not a heading **not bold** <b>x</b>');
    assert.equal(ui.nodes.messages.children[0].children.length, 1);
});

test('sending and answering scroll smoothly to the newest message', async () => {
    let resolveTask;
    const ui = setup(url => url === '/task'
        ? new Promise(resolve => { resolveTask = resolve; })
        : Promise.resolve({ ok: false }));
    const log = layout(ui);
    ui.nodes.prompt.value = 'Plan the next release for me';
    const sending = ui.run('sendTask()');
    assert.equal(log.scrolls.length, 2);
    assert.deepEqual({ ...log.scrolls[1] }, { top: log.scrollHeight, behavior: 'smooth' });
    assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
    resolveTask({ ok: true, json: async () => ({ response: '## Release plan\n\n1. Freeze the branch\n2. Run every test\n3. Ship it' }) });
    await sending;
    assert.equal(log.scrolls.length, 3);
    assert.equal(log.scrolls[2].top, log.scrollHeight);
    assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
    assert.equal(ui.nodes.jumpLatest.hidden, true);
});

test('restored history scrolls instantly to the newest message', async () => {
    const ui = setup(async url => url === '/runtime' ? { ok: false } : { ok: true, json: async () => ({ messages: [
        { role: 'user', content: 'What changed in the last release?' },
        { role: 'assistant', content: 'The **orb** and the greeting header were added to the Assistant view.' }
    ] }) }, 'conversation-1');
    const log = layout(ui);
    await tick();
    assert.equal(log.children.length, 2);
    assert.equal(log.scrolls.length, 1);
    assert.deepEqual({ ...log.scrolls[0] }, { top: log.scrollHeight, behavior: 'auto' });
    assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
});

test('scrolled-up owner is not pulled down and can jump to latest', () => {
    const ui = setup(async () => ({ ok: false }));
    const log = layout(ui);
    for (let i = 0; i < 6; i++) ui.run(`addMessage("jarvis", "Answer number ${i}")`);
    log.listeners.scroll(); // the browser reports the programmatic scroll to the bottom
    log.scrollTop = log.scrollHeight - log.clientHeight - 300;
    log.listeners.scroll();
    assert.equal(ui.nodes.jumpLatest.hidden, false);
    const scrolls = log.scrolls.length;
    const top = log.scrollTop;
    ui.run('addMessage("jarvis", "A new answer arrives")');
    assert.equal(log.scrolls.length, scrolls);
    assert.equal(log.scrollTop, top);
    assert.equal(ui.nodes.jumpLatest.hidden, false);
    ui.nodes.jumpLatest.listeners.click();
    const newest = log.children.at(-1).children[0];
    assert.equal(newest.textContent, 'A new answer arrives');
    assert.equal(newest.attributes.tabindex, '-1');
    assert.equal(ui.run('document.activeElement'), newest);
    assert.deepEqual({ ...newest.focusOptions }, { preventScroll: true });
    assert.equal(log.scrolls.at(-1).top, log.scrollHeight);
    assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
    log.listeners.scroll();
    assert.equal(ui.nodes.jumpLatest.hidden, true);
    log.scrollTop -= 60;
    log.listeners.scroll();
    assert.equal(ui.nodes.jumpLatest.hidden, true);
    ui.run('addMessage("user", "Still following")');
    assert.equal(log.scrolls.length, scrolls + 2);
    assert.match(html, /\.jump-latest\s*\{[^}]*min-height:\s*44px/);
});

test('reduced motion scrolls instantly', () => {
    const ui = setup(async () => ({ ok: false }), null,
        { matchMedia: query => ({ matches: query === '(prefers-reduced-motion: reduce)' }) });
    const log = layout(ui);
    ui.run('addMessage("user", "Hello")');
    assert.equal(log.scrolls.at(-1).behavior, 'auto');
});

test("assistant layout keeps #messages as the bounded scroll container with a compact header once chatting", () => {
    const css = html.match(/<style>([\s\S]*?)<\/style>/)[1].replace(/\s+/g, " ");
    // The page is one viewport tall while the assistant is shown, so the log (not the page) scrolls.
    assert.match(css, /\.layout:has\(#assistantView:not\(\[hidden\]\)\) \{ height: 100vh; height: 100dvh; overflow: hidden; \}/);
    // Every flex ancestor of #messages may shrink below its content.
    assert.match(css, /\.main \{ min-height: 0; \}/);
    assert.match(css, /#assistantView \{ flex: 1 1 0; min-height: 0; \}/);
    assert.match(css, /\.assistant-layout \{ flex: 1; min-height: 0;/);
    assert.match(css, /\.assistant-chat \{ flex: 1; min-width: 0; min-height: 0;/);
    assert.match(css, /#messages \{ min-height: 0;/);
    assert.match(css, /\.composer-wrapper \{ flex: none; \}/);
    // The old fixed heights that let content grow past the viewport are gone.
    assert.doesNotMatch(css, /#assistantView \{ height: auto; \}/);
    assert.doesNotMatch(css, /calc\(100dvh - 116px\)/);
    // Phones: the chat fills the bounded area and the plus-point card sits below it.
    assert.match(css, /@media\(max-width: 900px\) \{[^@]*\.assistant-layout \{ flex-direction: column; overflow-y: auto; \}[^@]*\.assistant-chat \{ flex: 0 0 100%; min-height: 0; \}/);
    // Compact header once there are messages: greeting hidden, the aria-live state label is never hidden.
    assert.match(css, /\.assistant-chat:has\(#messages:not\(:empty\)\) \.core-greeting \{ display: none; \}/);
    assert.doesNotMatch(css, /\.core-label \{[^}]*display: none/);
    assert.match(html, /<div id="messages"[^>]*><\/div>/, "#messages must start truly empty for :empty");
});
