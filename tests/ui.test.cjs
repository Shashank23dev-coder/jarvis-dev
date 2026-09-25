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
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(key, callback) { this.listeners[key] = callback; }
    focus() { this.focused = true; }
}
function setup(fetch, savedConversation = null) {
    const nodes = {};
    for (const match of html.matchAll(/id="([^"]+)"/g)) nodes[match[1]] = new Node();
    const nav = [...html.matchAll(/data-view="([^"]+)"/g)].map(match => {
        const node = new Node('button'); node.dataset.view = match[1]; return node;
    });
    const document = {
        getElementById: id => nodes[id],
        createElement: tag => new Node(tag),
        querySelectorAll: selector => selector === '[data-view]' ? nav : []
    };
    const saved = new Map(savedConversation ? [['jarvis.conversationId', savedConversation]] : []);
    const localStorage = {
        getItem: key => saved.get(key) || null,
        setItem: (key, value) => saved.set(key, value),
        removeItem: key => saved.delete(key)
    };
    const context = vm.createContext({ document, fetch, AbortController, localStorage });
    vm.runInContext(script, context);
    return { nodes, nav, saved, run: code => vm.runInContext(code, context) };
}
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const tick = () => new Promise(resolve => setImmediate(resolve));

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

test('tasks load safely, include plan details, and support empty/error/retry', async () => {
    let payload = { tasks: [{ goal: '<img src=x onerror=alert(1)>', status: 'planned', created_at: '2026-09-24T12:00:00', steps: [{ description: 'Inspect', agent: 'reviewer' }] }] };
    let ok = true;
    const calls = [];
    const ui = setup(async url => { calls.push(url); return { ok, json: async () => payload }; });
    await ui.run('loadView("tasks")');
    assert.deepEqual(calls, ['/tasks']);
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
        assert.equal(url, '/activity');
        return { ok: true, json: async () => ({ activity: [{ message: 'Planning completed', type: 'completed', goal: 'Build Jarvis', timestamp: '2026-09-24T12:00:00' }] }) };
    });
    await ui.run('loadView("activity")');
    assert.match(text(ui.nodes.activityContent), /Planning completed.*Build Jarvis/);
});

test('refresh aborts obsolete requests without replacing the latest results', async () => {
    const pending = [];
    const ui = setup((url, options) => new Promise(resolve => pending.push({ resolve, options })));
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
    assert.deepEqual(calls, ['/task']);
    ui.run('showView("tasks")');
    resolveTask({ ok: true, json: async () => ({ goal: 'Build Jarvis', steps: [{ id: 1, description: 'Inspect code', agent: 'reviewer' }] }) });
    await sending;
    await tick();
    assert.match(text(ui.nodes.messages), /1\. Inspect code \[reviewer\]/);
    assert.equal(ui.nodes.prompt.value, 'Next draft');
    assert.equal(ui.nodes.sendBtn.disabled, false);
    assert.equal(ui.nodes.prompt.focused, undefined);
    assert.deepEqual(calls, ['/task', '/tasks', '/tasks']);
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
        assert.equal(url, '/task');
        assert.deepEqual(JSON.parse(options.body), { goal: 'Inspect files', mode: 'agent' });
        return { ok: true, json: async () => ({ goal: 'Inspect files', steps: [], status: 'completed',
            response: 'Inspected two files. <img src=x onerror=alert(1)>',
            agent: { iterations: 3, tool_calls: 2, stop_reason: 'final_response' } }) };
    });
    ui.nodes.prompt.value = 'Inspect files';
    await ui.run('sendTask()');
    assert.match(text(ui.nodes.messages), /Inspected two files\. <img src=x onerror=alert\(1\)>/);
    assert.equal(ui.nodes.messages.children[1].children[0].children.length, 0);
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
    ['Hello!', 'Hello! How can I help?']
]) {
    test('Assistant submits and displays the answer for: ' + goal, async () => {
        const ui = setup(async (url, options) => {
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

test('failed task retains conversation ID from response headers', async () => {
    const ui = setup(async () => ({ ok: false,
        headers: { get: () => 'failed-conversation' },
        json: async () => ({ detail: 'Jarvis could not complete the agent request.' }) }));
    ui.nodes.prompt.value = 'Try';
    await ui.run('sendTask()');
    assert.equal(ui.saved.get('jarvis.conversationId'), 'failed-conversation');
});
