"""Exercise runtime evidence, stale claims and the authenticated chat wire schema."""
import json
from types import SimpleNamespace as NS
from uuid import uuid4
from unittest.mock import Mock
import pytest
from core import self_state, deployment_status, engineering_tools
from core.agent import AgentRunner
from core.brain import BrainSettings, JarvisBrain
from core.candidate_tools import CandidateTools
from core.engineering_authority import OwnerDevAuthority, current_owner
from core.task_store import TaskStore
from models.provider import ProviderRegistry
from models import openai_client
from tools import RepositoryTools, ToolRegistry
from test_agent import call, final
from test_brain import ScriptedProvider
def extract(request):
    text = request.instructions if hasattr(request,'instructions') else request['instructions']
    return json.loads(text.split(self_state.MARKER)[1].split('\n</jarvis_self_state>')[0])
@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = TaskStore(tmp_path/'state.db')
    task = store.create_task('Capabilities')
    store.add_brain_runtime(task['id'],enabled=True,selected_provider='local')
    jid, cid = str(uuid4()), str(uuid4())
    record = dict(state='needs_revision',provider_results=[],events=[],secret='HIDDEN')
    live = dict(supported=True,worker='active',observed_at=1.0,jobs=[dict(id=jid,candidate_id=cid,status='failed')])
    production = dict(current={'revision':'a'*40},observed_at='2026-10-01T00:00:00+00:00',source='host_controller',latest={'status':'deployed'})
    reads = []
    def runtime(_):
        reads.append('read')
        return NS(model_dump=lambda:live)
    monkeypatch.setattr(engineering_tools,'runtime',runtime)
    monkeypatch.setattr(deployment_status,'configured_inspector',lambda:NS(status=lambda:production))
    candidates = CandidateTools(task['goal'], store.database, authority=OwnerDevAuthority())
    candidates.production = False
    candidates._service = lambda **_:NS(store=NS(get=lambda *a,**k:record))
    state = self_state.SelfState(store,task['id'],candidates,configured=['local'],route='local')
    return state, store, task, live, record, production, reads
@pytest.mark.parametrize('worker',['active','inactive','unknown'])
@pytest.mark.parametrize('owner',[True,False])
def test_owner_health_privacy_tools_and_no_mutation(setup,worker,owner):
    state,store,task,live,record,production,reads = setup
    live['worker'] = worker
    if not owner: state.candidates.authority = None
    before = store.get_task(task['id'])['activity'].copy()
    result = state.snapshot([], [{'name':'read_text'},{'name':'/HIDDEN'}])
    assert result['engineering']['worker'] == (worker if owner else 'unknown')
    assert bool(result['engineering']['jobs']) == owner and bool(reads) == owner
    assert result['available_tools'] == ['read_text'] and 'HIDDEN' not in json.dumps(result)
    assert result['approvals'] == dict(candidate_apply='consequence_based', production_deploy='human_required') and store.get_task(task['id'])['activity'] == before
    assert result['providers']['availability']['local']['status'] == 'unknown'
@pytest.mark.parametrize('code,expected',[('provider_quota','quota_blocked'),('provider_unavailable','unavailable'),('provider_rate_limit','unknown')])
def test_historical_provider_evidence_is_ordered_and_never_current(setup,code,expected):
    from core.candidate_provider import failure_evidence
    state,store,task,live,record,_,_ = setup
    live['jobs'][0].update(failure=failure_evidence('codex',code,'implementing',6),attempts=6,completed_at='2026-10-01T02:00:00')
    record.update(provider_results=[dict(provider='codex',stage='implementing',attempt=6,iteration=1,status='no_changes',response_sha256='a'*64)],
                  events=[dict(event='provider_response_received',timestamp='2026-10-01T01:00:00')])
    first = state.snapshot([],[])['providers']['availability']['codex']
    assert first['status'] == expected and first['current_usability'] == 'unknown' and first['source'] == 'engineering_failure'
    assert first['last_success'] == '2026-10-01T01:00:00+00:00'
    record['events'][0]['timestamp'] = '2026-10-01T03:00:00'
    latest = state.snapshot([],[])['providers']['availability']['codex']
    assert latest['status'] == 'usable' and latest['current_usability'] == 'unknown' and latest['source'] == 'engineering_response'
    live['jobs'][0].update(failure=failure_evidence('codex','tests_failed','testing',6),completed_at='2026-10-01T04:00:00')
    assert state.snapshot([],[])['providers']['availability']['codex']['status'] == 'usable'
    record.update(provider_results=[],events=[],state='passed')
    live['jobs'][0].update(failure=None,status='waiting_for_approval')
    assert state.snapshot([],[])['providers']['availability']['codex']['status'] == 'unknown'
def test_missing_adapter_success_failure_and_task_boundaries(setup):
    state,store,task,*_ = setup
    state.route = 'claude'
    assert state.snapshot([],[])['providers']['availability']['claude']['scope'] == 'chat_adapter'
    for status,expected in [('completed','usable'),('failed','unknown')]:
        store.add_routing_activity(task['id'],provider='local',reason='configured_provider' if status=='completed' else 'provider_error',iteration=1,status=status)
        value = state.snapshot([],[])['providers']['availability']['local']
        assert value['current_usability'] == expected and value['last_success'] and value['as_of']
    store.add_brain_runtime(task['id'],enabled=True,selected_provider='local')
    assert state.snapshot([],[])['providers']['availability']['local']['last_success'] is None

@pytest.mark.parametrize('area',['worker','job','candidate','revision','deployment','approval_apply','approval_deploy','tool','absent_tool','provider','brain'])
def test_natural_and_persisted_contradictions_use_known_authority(setup,area):
    state,store,task,live,*_ = setup
    store.add_routing_activity(task['id'],provider='local',reason='configured_provider',iteration=1,status='completed')
    jid,cid = live['jobs'][0]['id'],live['jobs'][0]['candidate_id']
    cases = dict(worker=('Worker is inactive','worker','inactive'),job=(f'Job {jid} is completed',f'job.{jid}.state','completed'),
        candidate=(f'Candidate {cid} is passed',f'candidate.{cid}.state','passed'),revision=('Active revision is '+'b'*40,'production.revision','b'*40),
        deployment=('Deployment is failed','deployment.state','failed'),approval_apply=('Candidate apply approval is automatic','approval.candidate_apply','not_required'),
        approval_deploy=('Production deployment approval is not_required','approval.production_deploy','not_required'),tool=('Tool read_text is unavailable','tool.read_text.available',False),
        absent_tool=('Tool execute_shell is available','tool.execute_shell.available',True),provider=('Local is unavailable','provider.local.status','unavailable'),brain=('JarvisBrain is disabled','jarvisbrain_enabled',False))
    text,key,claim = cases[area]
    for history in ([{'role':'assistant','content':text}],[{'role':'user','content':json.dumps({key:claim})}]):
        result = state.snapshot(history,[{'name':'read_text'}])
        assert dict(field=key,recalled=claim,runtime=result['facts'][key]) in result['contradictions']
    assert not state.snapshot(['Codex is unavailable; Worker is HIDDEN'],[])['contradictions']

def test_unknown_sources_remain_unknown(setup,monkeypatch):
    state,_,_,_,_,production,_ = setup
    production.update(current={'revision':'/HIDDEN'},observed_at='HIDDEN',latest={'status':'HIDDEN'})
    monkeypatch.setattr(engineering_tools,'runtime',lambda _:(_ for _ in ()).throw(RuntimeError('HIDDEN')))
    result = state.snapshot(['Worker is active'],[])
    assert result['production'] == dict(revision=None,observed_at=None) and not result['contradictions'] and 'HIDDEN' not in json.dumps(result)

@pytest.mark.parametrize('enabled',[True,False])
def test_stale_memory_before_capability_decision_and_same_turn_fallback(setup,tmp_path,enabled):
    state,store,prior,*_ = setup
    store.update_task(prior['id'],response='JarvisBrain is '+('disabled' if enabled else 'enabled'))
    task = store.create_task('Can you inspect files; should we build Brain?',conversation_id=prior['conversation_id'])
    provider,legacy_requests = ScriptedProvider(call(),RuntimeError('HIDDEN')),[]
    def decide(**request):
        legacy_requests.append(request)
        return final('Brain already active; use it.' if extract(request)['brain']['jarvisbrain_enabled'] else 'Enable existing Brain.')
    runner = AgentRunner(ToolRegistry(RepositoryTools(tmp_path)),model=decide,max_iterations=2)
    result = JarvisBrain(runner,settings=BrainSettings(enabled,'local'),providers=ProviderRegistry([provider])).run(task['goal'],task_id=task['id'],task_store=store)
    requests = provider.requests + legacy_requests
    assert result.response == ('Brain already active; use it.' if enabled else 'Enable existing Brain.') and 'build' not in result.response.lower()
    assert result.status == 'completed' and all('read_text' in extract(r)['available_tools'] for r in requests)
    assert all(extract(r)['contradictions'] == [dict(field='jarvisbrain_enabled',recalled=not enabled,runtime=enabled)] for r in requests)
    if enabled:
        assert extract(legacy_requests[0])['providers']['availability']['local']['current_usability']=='unknown'
        assert extract(legacy_requests[0])['brain']['fallback_used'] and legacy_requests[0]['allow_tools'] is False

@pytest.mark.parametrize('capability',['read_text','execute_shell'])
def test_direct_refusal_and_bounded_broker(setup,tmp_path,monkeypatch,capability):
    from threading import Event, BoundedSemaphore
    state,store,task,*_ = setup
    gate = Event()
    state.candidates.production = True
    monkeypatch.setattr(self_state,'READ_WAIT',0.01)
    monkeypatch.setattr(self_state,'_READ_SLOTS',BoundedSemaphore(2))
    seen = []
    def slow(_):
        seen.append(current_owner.get()); gate.wait(2)
        return NS(model_dump=lambda:dict(supported=True,worker='active',observed_at=1.0,jobs=[]))
    monkeypatch.setattr(engineering_tools,'runtime',slow)
    authority = OwnerDevAuthority(); binding = current_owner.set(authority)
    try:
        assert state.snapshot([],[])['engineering']['worker']=='unknown' and state.snapshot([],[])['engineering']['worker']=='unknown'
        assert seen == [authority]; gate.set(); assert state.pending['done'].wait(1)
        assert state.snapshot([],[])['engineering']['worker']=='active'
    finally:
        gate.set(); current_owner.reset(binding)
    task = store.create_task('Can you use '+capability+'?')
    def decide(**request):
        snapshot = extract(request)
        assert 'execute_shell' not in snapshot['available_tools'] and 'run_command' in snapshot['available_tools']
        return final('I can use '+capability if capability in snapshot['available_tools'] else 'I cannot use '+capability+': unavailable.')
    result = AgentRunner(ToolRegistry(RepositoryTools(tmp_path)),model=decide).run(task['goal'],task_id=task['id'],task_store=store)
    assert result.status=='completed' and result.response==('I can use read_text' if capability=='read_text' else 'I cannot use execute_shell: unavailable.')

def test_authenticated_chat_schemas_are_strict_without_weakening_host_contract(setup,monkeypatch):
    state,store,task,*_ = setup
    tools = CandidateTools('Inspect existing job',store.database,authority=OwnerDevAuthority(),source_context=dict(task_id=task['id'],conversation_id=task['conversation_id']))
    definitions = tools.bind(ToolRegistry()).model_tools(); before = json.dumps(definitions)
    client = Mock(); client.with_options.return_value.responses.create.return_value=NS(output=[],output_text='ok',status='completed')
    monkeypatch.setattr(openai_client,'get_client',lambda:client)
    openai_client.request_agent_turn(input=[],tools=definitions,instructions='',allow_tools=True)
    sent = client.with_options.return_value.responses.create.call_args.kwargs['tools']
    assert all(t['strict'] and set(t['parameters'].get('required',[]))==set(t['parameters']['properties']) for t in sent)
    page = next(t for t in sent if t['name']=='list_engineering_jobs')['parameters']
    assert page['required']==['before'] and {'type':'null'} in page['properties']['before']['anyOf'] and page['additionalProperties'] is False
    assert json.dumps(definitions)==before and tools.authority == state.candidates.authority


def test_engineering_registry_overrides_stale_claims_and_projects_no_secrets(setup, tmp_path):
    from core.engineering_providers import EngineeringRegistry, Registration
    from core.candidate_provider import ProposalProvider
    state, store, _, live, *_ = setup
    registry = EngineeringRegistry(store.database, [Registration(
        ProposalProvider('claude_code', lambda *args: None), 'cli', True)])
    registry.observe('claude_code', 'not_authenticated', 'claude_auth_status',
                     'local_authentication', 'provider_authentication')
    live['provider_registry'] = registry.snapshot()
    result = state.snapshot(['Engineering provider claude_code is available'], [])
    assert result['engineering']['provider_registry']['providers'][0]['current_usability'] == 'not_authenticated'
    assert result['facts']['engineering_provider.claude_code.status'] == 'not_authenticated'
    assert result['contradictions'] == [dict(field='engineering_provider.claude_code.status',
        recalled='usable', runtime='not_authenticated')]
    assert 'HIDDEN' not in json.dumps(result)
