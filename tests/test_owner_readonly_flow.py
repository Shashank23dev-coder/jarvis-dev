"""Production UI, Brain and real mailbox/broker dispatch; no mocked client calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.agent import AgentRunner
from core.brain import BrainSettings
from core.candidate_broker import Broker
from core.candidate_bridge import CandidateClient
from core.candidate_tools import CandidateTools
from core.database import Database
from core.deployment import Settings
from core.engineering_authority import current_owner
from core.engineering_portfolio import Portfolio
from core.engineering_repository import inspect_repository
from core.orchestrator import Orchestrator
from models.provider import ProviderRegistry
from tools import RepositoryTools, ToolRegistry, ToolDenied
from test_agent import call, final
from test_brain import ScriptedProvider
from test_candidate_bridge import request
from test_project_repository import projects


@pytest.fixture
def production_flow(projects, tmp_path, monkeypatch):
    """Separate host process, native request/response files and production HTTP."""
    import main
    from core import candidate_bridge as bridge
    portfolio, roots = projects
    # Host metadata is authoritative; chat metadata deliberately points elsewhere.
    chat_path = tmp_path / 'chat' / 'chat.sqlite3'
    chat_path.parent.mkdir()
    chat = Portfolio(Database(chat_path))
    record = next(p for p in chat.projects() if p['project_id'] == 'trading-bot')
    chat.register_project(record | dict(repository='/untrusted/chat-root'), replace=True)
    mailbox = tmp_path / 'mailbox'
    monkeypatch.setattr(bridge, 'ROOT', mailbox)
    # Only the test's trusted process configuration chooses host state/source.
    host_source = Path(__file__).resolve().parents[1]
    code = ("import sys,json; from pathlib import Path; from core import candidate_broker as b; "
            "b.STATE=Path(sys.argv[1]); b.SOURCE=Path(sys.argv[2]); "
            "print(json.dumps(b.execute_private(json.load(sys.stdin))))")
    host_state = tmp_path / 'host'
    host_state.mkdir()
    # Host database retains the fixture's registered roots and Git identity.
    host=Portfolio(Database(host_state/'candidates.sqlite3'))
    for record in portfolio.projects():
        host.register_project(record,replace=True)
    stop = Event()
    errors, requests = [], []
    def serve():
        while not stop.is_set():
            if mailbox.exists():
                for incoming in mailbox.glob('*.request'):
                    raw = None
                    try:
                        raw = bridge.read(incoming)
                        requests.append(raw)
                        completed = subprocess.run([sys.executable, '-B', '-c', code, str(host_state), str(host_source)],
                            input=json.dumps(raw), capture_output=True, text=True, timeout=20,
                            cwd=host_source,
                            env=dict(os.environ, JARVIS_MODE='development'))
                        assert completed.returncode == 0, 'Host process failed'
                        response = json.loads(completed.stdout)
                        bridge.write(mailbox / (raw['request_id'] + '.response'), response)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                        if raw is not None:
                            bridge.write(mailbox / (raw['request_id'] + '.response'),
                                dict(request_id=raw['request_id'], ok=False,
                                     error=ToolDenied(reason='execution_failed').public_error()))
                    finally:
                        incoming.unlink(missing_ok=True)
            stop.wait(.01)
    thread = Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv('JARVIS_MODE', 'production')
    token = 'owner-readonly-flow-token-' * 3
    settings = Settings(mode='production', database_path=chat_path, state_dir=chat_path.parent,
        auth_token=token, public_origin='https://jarvis.example.test')
    def client_for(*turns):
        provider = ScriptedProvider(*turns)
        runner = AgentRunner(ToolRegistry(RepositoryTools(roots['jarvis-core'])),
            model=lambda **kw: pytest.fail('Brain must use configured provider'),
            max_tool_calls=16, max_iterations=20,
            candidate_service_factory=lambda _: pytest.fail('Production cannot use local candidate service'))
        monkeypatch.setattr(main, 'orchestrator', Orchestrator(runner,
            brain_settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider])))
        return TestClient(main.create_app(settings=settings), base_url=settings.public_origin), provider
    yield SimpleNamespace(client_for=client_for, headers={'Authorization':'Bearer ' + token},
        roots=roots, host_state=host_state, requests=requests, errors=errors)
    stop.set()
    thread.join(25)
    assert not thread.is_alive() and not errors


@pytest.mark.usefixtures('owner')
@pytest.mark.parametrize('failure', ['exit', 'timeout', 'invalid_json'])
def test_fixture_child_failure_returns_error_without_mailbox_timeout(production_flow, monkeypatch, failure):
    flow = production_flow
    original = subprocess.run
    def fail_child(argv, *args, **kwargs):
        if len(argv) > 3 and argv[2] == '-c' and 'b.STATE=' in argv[3]:
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            return subprocess.CompletedProcess(argv, 1 if failure == 'exit' else 0,
                                               stdout='invalid json', stderr='')
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', fail_child)
    raw = request(action='runtime', arguments={})
    client = CandidateClient(
        raw['goal'], {key: raw[key] for key in ('task_id', 'conversation_id')})
    with pytest.raises(ToolDenied) as caught:
        client.invoke('runtime', {})
    assert caught.value.reason == 'execution_failed'
    expected = 'TimeoutExpired' if failure == 'timeout' else 'AssertionError' if failure == 'exit' else 'JSONDecodeError'
    assert flow.errors == [expected]
    flow.errors.clear()  # This test deliberately exercises the fixture's error path.


@pytest.mark.usefixtures('owner')
def test_production_ui_owner_reads_registered_project_through_real_mailbox(production_flow):
    flow = production_flow
    turns = [call('inspect_engineering_runtime', {}, 'caps'),
        call('inspect_portfolio_project', {'project_id':'trading-bot'}, 'meta'),
        call('list_files', {'path':'.'}, 'list'),
        call('read_text', {'path':'docs/readme.md'}, 'read'),
        call('inspect_project_repository', {'project_id':'trading-bot','operation':'search_text','query':'project'}, 'search'),
        call('git_status', {}, 'status'),
        call('git_diff', {'path':None,'staged':False}, 'diff'),
        call('run_command', {'command':['git','log']}, 'log'),
        call('run_command', {'command':['git','rev-parse','HEAD']}, 'rev'), final('Inspection complete.')]
    client, provider = flow.client_for(*turns)
    with client:
        denied = client.post('/task', json={'goal':'Inspect the registered trading-bot project read-only.'})
        assert denied.status_code == 401
        assert client.post('/task', headers={'Authorization':'Bearer invalid'},
            json={'goal':'Inspect the registered trading-bot project read-only.'}).status_code == 401
        assert not provider.requests and not flow.requests
        response = client.post('/task', headers=flow.headers,
            json={'goal':'Inspect the registered trading-bot project read-only.'})
        assert response.status_code == 200
        task = client.get('/tasks/' + response.json()['id'], headers=flow.headers).json()
    outputs = [json.loads(r.input[-1]['output']) for r in provider.requests[1:]]
    assert len(outputs) == 9 and all(r['ok'] for r in outputs), outputs
    assert task['status'] == 'completed'
    assert len([e for e in task['activity'] if e['type']=='tool_completed']) == 9
    assert not any(e['type']=='tool_failed' for e in task['activity'])
    for output in outputs[2:]:
        evidence = output['data']['repository']
        assert evidence['available'] and evidence['root'] == str(flow.roots['trading-bot'])
    assert outputs[3]['data']['text'].startswith('# trading-bot')
    assert outputs[-1]['data']['text'] in outputs[-2]['data']['process']['stdout']
    assert all(r['authority']['principal']=='owner' for r in flow.requests)


@pytest.mark.usefixtures('owner')
@pytest.mark.parametrize('tool,args,gate,reason', [
    ('read_text', {'path':'/etc/passwd'}, 'repository_path','path_escape'),
    ('read_text', {'path':'../jarvis-core/src/app.py'}, 'repository_path','path_escape'),
    ('read_text', {'path':'.env'}, 'repository_path','protected_path'),
    ('read_text', {'path':'config.txt'}, 'repository_read','protected_content'),
    ('read_text', {'path':'security/config.py'}, 'repository_path','protected_path'),
    ('read_text', {'path':'escape.txt'}, 'repository_path','path_unavailable_or_unsafe'),
    ('read_text', {'path':'hardlink.txt'}, 'repository_path','linked_or_special_file'),
    ('inspect_project_repository', {'project_id':'trading-bot','root':'/etc'}, 'tool_arguments','schema_mismatch'),
    ('inspect_project_repository', {'project_id':'/etc'}, 'tool_arguments','schema_mismatch'),
    ('run_command', {'command':['touch','marker']}, 'project_operation','read_only_operation_required'),
    ('run_command', {'command':['systemctl','restart','trading']}, 'project_operation','read_only_operation_required'),
    ('run_command', {'command':['python','trade.py']}, 'project_operation','read_only_operation_required'),
    ('run_command', {'command':['sudo','reboot']}, 'project_operation','read_only_operation_required'),
    ('run_command', {'command':['curl','https://example.test/account/withdraw']}, 'project_operation','read_only_operation_required'),
    ('run_command', {'command':['git','-C','/etc','status']}, 'project_operation','read_only_operation_required'),
])
def test_production_ui_negative_security_gates(production_flow, tool, args, gate, reason):
    flow = production_flow
    if args.get('path')=='config.txt':
        marker='sk-' + 'a' * 40
        (flow.roots['trading-bot']/'config.txt').write_text(marker)
    if args.get('path') in {'escape.txt','hardlink.txt'}:
        target=flow.roots['trading-bot'].parent/'outside.txt'
        target.write_text('OUTSIDE_SENTINEL')
        if args['path']=='escape.txt':
            (flow.roots['trading-bot']/'escape.txt').symlink_to(target)
        else:
            os.link(target,flow.roots['trading-bot']/'hardlink.txt')
    client, provider = flow.client_for(call(tool,args),final('Denied.'))
    with client:
        response=client.post('/task',headers=flow.headers,
            json={'goal':'Inspect the registered trading-bot project read-only.'})
    assert response.status_code == 200
    error=json.loads(provider.requests[-1].input[-1]['output'])['error']
    assert (error['gate'],error['reason']) == (gate,reason)
    assert not (flow.roots['trading-bot']/'marker').exists()
    assert 'OUTSIDE_SENTINEL' not in json.dumps(error)


@pytest.mark.usefixtures('owner')
@pytest.mark.skipif(os.environ.get('JARVIS_REGISTERED_READ_ACCEPTANCE')!='1', reason='Opt-in actual Trading Bot read acceptance')
def test_actual_trading_bot_production_equivalent_acceptance(production_flow, tmp_path):
    flow=production_flow
    root=Path('/srv/jarvis/projects/trading-bot')
    def status():
        return subprocess.check_output(['/usr/bin/git','--no-optional-locks','-C',str(root),'status','--short'],text=True)
    before=status()
    assert before==''
    host=Portfolio(Database(flow.host_state/'candidates.sqlite3'))
    record=next(p for p in host.projects() if p['project_id']=='trading-bot')
    record=record | dict(repository=str(root),baseline_commit='e03c307c64fdaaf267ecb7be3898325af555f966')
    host.register_project(record,replace=True)
    turns=[call('inspect_engineering_runtime',{},'caps'),
        call('inspect_portfolio_project',{'project_id':'trading-bot'},'meta'),
        call('list_files',{'path':'.'},'list'),call('read_text',{'path':'README.md'},'read'),
        call('git_status',{},'status'),call('run_command',{'command':['git','log']},'log'),
        call('run_command',{'command':['git','rev-parse','HEAD']},'revision'),
        call('git_diff',{'path':None,'staged':False},'diff'),final('Read-only inspection complete.')]
    client,provider=flow.client_for(*turns)
    try:
        with client:
            assert client.post('/task',json={'goal':'Inspect trading-bot'}).status_code==401
            assert client.post('/task',headers={'Authorization':'Bearer invalid'},json={'goal':'Inspect trading-bot'}).status_code==401
            response=client.post('/task',headers=flow.headers,
                json={'goal':'Inspect the registered trading-bot project read-only.'})
            assert response.status_code==200
            task=client.get('/tasks/'+response.json()['id'],headers=flow.headers).json()
        results=[json.loads(r.input[-1]['output']) for r in provider.requests[1:]]
        assert len(results)==8 and all(r['ok'] for r in results),results
        for result in results[2:]:
            assert result['data']['repository']['root']==str(root)
            assert result['data']['repository']['available']
        revision=results[-2]['data']['text']
        assert revision==record['baseline_commit']
        assert not results[4]['data']['process']['stdout']
        proof=dict(goal=task['goal'],task_id=task['id'],status=task['status'],
            mode='production',brain_enabled=True,model='scripted configured provider',
            transport='POST /task -> Brain -> native mailbox -> separate host broker process',
            root=str(root),commit=revision,successful_tools=8,
            visible_files=len(results[2]['data']['files']),read_file='README.md',
            read_characters=len(results[3]['data']['text']),missing_auth=401,invalid_auth=401,
            status_before=before,status_after=status(),trading_bot_unchanged=True)
        path=Path('/tmp/jarvis-owner-readonly-acceptance.json')
        path.write_text(json.dumps(proof,indent=2)+'\n')
    finally:
        assert status()==before==''


@pytest.mark.usefixtures('owner')
def test_registered_read_does_not_depend_on_candidate_checkout(projects, monkeypatch):
    portfolio, _ = projects
    service=SimpleNamespace(source=Path('/missing/candidate-source'),store=SimpleNamespace(database=portfolio.database))
    broker=Broker(service,source='/another/candidate-source')
    monkeypatch.setenv('JARVIS_MODE','production')
    result=broker.execute(request('project_repository','Inspect trading-bot',
        {'project_id':'trading-bot','operation':'list_files'}))
    assert result['repository']['available'] and result['files']
    with pytest.raises(ToolDenied) as exc:
        broker.execute(request('create'))
    assert exc.value.gate == 'candidate_workspace'


@pytest.mark.usefixtures('owner')
def test_registered_read_does_not_construct_candidate_service(projects, monkeypatch):
    portfolio, _=projects
    from core import candidate_broker as module
    monkeypatch.setattr(module,'SelfEditService',lambda *a,**kw: pytest.fail('Reads must not construct a write service'))
    broker=Broker(database=portfolio.database,source='/missing/dev-checkout')
    assert broker.execute(request('project_repository',arguments={'project_id':'trading-bot','operation':'list_files'}))['files']
    assert broker.service is None


@pytest.mark.usefixtures('owner')
def test_serialized_owner_and_root_overrides_cannot_authorize_reads(projects):
    portfolio, _=projects
    service=SimpleNamespace(store=SimpleNamespace(database=portfolio.database))
    broker=Broker(service)
    binding=current_owner.set(None)
    try:
        with pytest.raises(ToolDenied) as exc:
            broker.execute(request('project_repository',arguments={'project_id':'trading-bot'}))
        assert exc.value.reason=='owner_required'
        with pytest.raises(ToolDenied):
            CandidateTools('Inspect trading-bot',portfolio.database).inspect_repository(project_id='trading-bot')
    finally:
        current_owner.reset(binding)
    for field in ('root','repository','source_root'):
        with pytest.raises(ValueError):
            broker.execute(request('project_repository',arguments={'project_id':'trading-bot',field:'/etc'}))


@pytest.mark.usefixtures('owner')
def test_owner_read_tools_without_candidate_task_metadata(projects):
    portfolio, roots=projects
    registry=CandidateTools('Inspect trading-bot',portfolio.database).bind(ToolRegistry(RepositoryTools(roots['jarvis-core'])))
    assert registry._definitions['list_files'].handler(path='.').repository.available


@pytest.mark.usefixtures('owner')
def test_plain_registered_repository_reads_do_not_require_git(projects):
    portfolio, roots=projects
    import shutil
    shutil.rmtree(roots['trading-bot']/'.git')
    result=inspect_repository(portfolio.database,project_id='trading-bot',operation='read_text',path='docs/readme.md')
    assert result['repository']['available'] and result['repository']['reason']=='git_unavailable'
    assert result['text'].startswith('# trading-bot')


def test_broker_dispatch_uses_fresh_process_and_fixed_host_configuration(monkeypatch):
    from core import candidate_broker as module
    seen=[]
    def run(argv, **kwargs):
        seen.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,json.dumps({'request_id':'test','ok':True,'record':{}}).encode(),b'')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert module.execute_current({'request_id':'test','action':'project_repository'})['ok']
    argv,kwargs=seen[0]
    assert argv==[sys.executable,'-B','-m','core.candidate_broker','--execute']
    assert kwargs['cwd']==module.SOURCE and kwargs['timeout']==1320
    assert json.loads(kwargs['input'])['action']=='project_repository'


@pytest.mark.usefixtures('owner')
def test_old_broker_response_identifies_legacy_gate(tmp_path, monkeypatch):
    from core import candidate_bridge as bridge
    monkeypatch.setattr(bridge,'ROOT',tmp_path/'mailbox')
    original=bridge.write
    def write(path,data):
        original(path,data)
        if path.suffix=='.request':
            original(path.with_suffix('.response'),{'request_id':data['request_id'],'ok':False})
    monkeypatch.setattr(bridge,'write',write)
    raw=request('project_repository',arguments={'project_id':'trading-bot'})
    with pytest.raises(ToolDenied) as exc:
        bridge.CandidateClient(raw['goal'],{k:raw[k] for k in ('task_id','conversation_id')}).invoke(raw['action'],raw['arguments'])
    assert (exc.value.gate,exc.value.reason)==('broker_response','legacy_rejection')
