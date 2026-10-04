"""Bounded, read-only observations; runtime facts outrank recalled claims."""
import json
import re
from contextvars import copy_context
from datetime import datetime, timezone
from threading import BoundedSemaphore, Event, Thread
from models.provider import PROVIDER_NAMES
from .brain_status import read_current_brain, safe_timestamp
from .database import valid_id
from .engineering_authority import require_owner

MARKER = '\n<jarvis_self_state>\n'
READ_WAIT = 0.5
_READ_SLOTS = BoundedSemaphore(2)
GUIDANCE = '''Check runtime facts before capability claims, refusals or proposing features.
Known runtime facts override stale conversation/memory claims. Surface contradictions.
Unknown is not absent. Registration is not usability. Success proves only the
timestamped route/response, not implementation quality or future availability.
Historical failures do not prove current quota. Observations grant no authority.
Jarvis Executive owns the portfolio; projects and providers are resources.
Use the existing capability before proposing replacement infrastructure. For new
engineering, inspect portfolio and persist decide_engineering rationale first.
Project references grant no filesystem, process, external access or trading authority.
Candidate apply and production deployment require separate human approval.'''

def observed_time(value):
    try:
        return safe_timestamp(value)
    except (TypeError, ValueError):
        return None

def contradictions(history, facts):
    """Match bounded explicit assertions, including canonical fields in saved JSON."""
    context = json.dumps(history, ensure_ascii=True)[:256000].replace('\\"', '"')
    aliases = {'jarvisbrain_enabled': r'jarvis\s*brain', 'worker': r'(?:engineering\s+)?worker',
        'production.revision': r'(?:active|production|deployed)\s+revision', 'deployment.state': r'(?:latest\s+)?deployment(?:\s+state)?',
        'approval.candidate_apply': r'candidate\s+apply\s+approval', 'approval.production_deploy': r'production\s+deploy(?:ment)?\s+approval'}
    result = []
    for field, live in facts.items():
        if live is None or live == 'unknown':
            continue
        parts = field.split('.')
        alias = aliases.get(field, '')
        if parts[0] in {'job', 'candidate'}: alias = parts[0] + r'\s+' + re.escape(parts[1])
        if parts[0] == 'provider': alias = r'(?:provider\s+)?' + parts[1] + r'(?:\s+provider)?'
        if parts[0] == 'engineering_provider': alias = r'engineering\s+(?:provider\s+)?' + parts[1] + r'(?:\s+provider)?'
        if parts[0] == 'tool': alias = r'(?:tool|capability)\s+' + parts[1]
        pattern = r'(?<![\w.])(?:' + re.escape(field) + (('|' + alias) if alias else '') + r')["\s]*(?:[:=]["\s]*|is\s+)([a-z0-9_-]+)(?![\w-])'
        for value in re.findall(pattern, context, re.I):
            value = value.lower()
            if type(live) is bool:
                value = {'true': True, 'false': False, 'enabled': True, 'disabled': False,
                         'available': True, 'unavailable': False}.get(value)
            else:
                value = {'available': 'available' if live == 'available' else 'usable', 'healthy': 'active', 'unhealthy': 'inactive',
                         'automatic': 'not_required', 'required': 'human_required'}.get(value, value)
                allowed = {'available','usable','unavailable','not_authenticated','invocation_failed','quota_blocked','active','inactive','created','queued','planning',
                    'implementing','testing','reviewing','waiting_for_approval','completed','blocked','failed',
                    'needs_revision','passed','rejected','discarded','human_required','not_required','deployed','rolled_back','unproven','needs_discovery','archived'}
                if value not in allowed and not re.fullmatch('[0-9a-f]{40}', value): value = None
            item = dict(field=field, recalled=value, runtime=live)
            if value is not None and value != live and item not in result: result.append(item)
    return result[:64]

class SelfState:
    def __init__(self, store, task_id, candidates, *, configured=None, route=None):
        self.store, self.task_id, self.candidates = store, task_id, candidates
        self.configured = None if configured is None else sorted(set(configured) & PROVIDER_NAMES)
        self.route = route if route in PROVIDER_NAMES else None
        self.pending = None

    def engineering_runtime(self):
        from .engineering_tools import runtime
        def collect():
            live = runtime(self.candidates).model_dump()
            live['candidate_records'] = {}
            for job in live['jobs'][:5]:
                cid = job.get('candidate_id')
                if not valid_id(cid): continue
                try:
                    service = self.candidates._service()
                    # Production reuses explicit inspection evidence; collecting state never acquires candidate leases.
                    live['candidate_records'][cid] = (getattr(self.candidates,'records',{}).get(cid) if self.candidates.production else
                                                       service.store.get(cid, internal=True, recover=False)) or {}
                except Exception:
                    pass
            return live
        if not getattr(self.candidates, 'production', False): return collect()
        if self.pending is None:
            slots = _READ_SLOTS
            if not slots.acquire(blocking=False): return None
            pending, context = {'done': Event()}, copy_context()
            def read():
                try: pending['value'] = collect()
                except Exception: pass
                finally:
                    slots.release()
                    pending['done'].set()
            try: Thread(target=lambda: context.run(read), daemon=True).start()
            except Exception:
                slots.release()
                return None
            self.pending = pending
        if not self.pending['done'].wait(READ_WAIT): return None
        value, self.pending = self.pending.get('value'), None
        return value

    def snapshot(self, history, tools):
        now = datetime.now(timezone.utc).isoformat()
        brain = read_current_brain(self.store, self.task_id).model_dump()
        names = sorted({t['name'] for t in tools if isinstance(t, dict) and isinstance(t.get('name'), str)
                        and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', t['name'])})
        result = dict(observed_at=now, brain=brain, available_tools=names[:128],
            production=dict(revision=None, observed_at=None), deployment=dict(state=None, source=None, as_of=None),
            engineering=dict(supported=None, worker='unknown', observed_at=None, jobs=[], candidates=[], visibility='unavailable', provider_registry=None),
            portfolio=None, pre_engineering_decisions=[],
            providers=dict(configured=self.configured, configured_route=self.route,
                active=brain['provider_used'] if brain['route_status'] in {'started','completed'} else None, availability={}, observations=[]),
            approvals=dict(candidate_apply='human_required', production_deploy='human_required'))
        availability, observations = result['providers']['availability'], result['providers']['observations']
        for provider in set(self.configured or []) | ({self.route, 'codex'} - {None}):
            availability[provider] = dict(status='unknown', current_usability='unknown', as_of=None, scope='unobserved', source=None)
        def observe(provider, status, at, source, scope, code=None):
            if provider not in PROVIDER_NAMES | {'legacy'}: return
            item = dict(provider=provider, status=status, as_of=observed_time(at), source=source,
                        scope=scope, historical=scope != 'current_task', code=code)
            observations.append(item)
            prior = availability.get(provider, {})
            # Compare execution timestamps, never job listing order or inspection time.
            if item['as_of'] and (not prior.get('as_of') or item['as_of'] >= prior['as_of']):
                age = (datetime.fromisoformat(now)-datetime.fromisoformat(item['as_of'])).total_seconds()
                availability[provider] = dict(item, current_usability=status if scope == 'current_task' and 0<=age<60 else 'unknown')
        try:
            from .deployment_status import configured_inspector
            status = configured_inspector().status()
            revision = status.get('current', {}).get('revision')
            result['production'] = dict(revision=revision if isinstance(revision,str) and re.fullmatch('[0-9a-f]{40}',revision) else None,
                                        observed_at=observed_time(status.get('observed_at')))
            from .deployment_status import STATUSES
            state = (status.get('latest') or {}).get('status')
            result['deployment'] = dict(state=state if state in STATUSES else None,
                source=status.get('source') if status.get('source') in {'host_controller','controller_snapshot'} else None,
                as_of=result['production']['observed_at'], scope='last_controller_observation')
        except Exception: pass
        try:
            require_owner(self.candidates.authority)
            from .candidate_provider import project_failure, project_provider_results
            live = self.engineering_runtime()
            engineering = result['engineering']
            engineering.update(supported=live['supported'], worker=live['worker'], observed_at=live['observed_at'], visibility='owner_recent_jobs')
            from .engineering_providers import project_registry
            engineering['provider_registry'] = project_registry(live.get('provider_registry'))
            from .engineering_portfolio import PortfolioSnapshot
            from .engineering_decision import Decision
            if live.get('portfolio') is not None:
                result['portfolio'] = PortfolioSnapshot.model_validate(live['portfolio']).model_dump()
            result['pre_engineering_decisions'] = [Decision.model_validate(d).model_dump() for d in live.get('decisions', [])[:5]]
            for job in live['jobs'][:5]:
                if not valid_id(job.get('id')): continue
                from .candidate_provider import WorkAttribution
                from .engineering_jobs import project_reference, project_decision
                engineering['jobs'].append(dict(id=job['id'], status=job['status'],
                    project_id=project_reference(job.get('project_id')), decision=project_decision(job.get('decision')),
                    attribution=WorkAttribution.model_validate(job['attribution']).model_dump() if job.get('attribution') else None))
                cid = job.get('candidate_id')
                if valid_id(cid):
                    c = live['candidate_records'].get(cid, {})
                    state = c.get('state')
                    engineering['candidates'].append(dict(id=cid, job_id=job['id'], job_status=job['status'],
                        state=state if state in {'created','needs_revision','passed','rejected','discarded'} else None))
                    times = [e.get('timestamp') for e in c.get('events',[]) if e.get('event') == 'provider_response_received']
                    records = project_provider_results(c.get('provider_results',[]))
                    for i, response in enumerate(records):
                        at = times[-len(records)+i] if len(times) >= len(records) else None
                        observe(response['provider'], 'usable', at, 'engineering_response', 'historical', response['status'])
                failure = project_failure(job.get('failure'))
                if failure and failure['attempt'] == job.get('attempts') and failure['code'] in {
                        'provider_quota','provider_unavailable','provider_authentication','provider_rate_limit','provider_timeout','provider_request_rejected','provider_invalid_response','provider_output_limit'}:
                    state = {'provider_quota':'quota_blocked','provider_unavailable':'unavailable','provider_authentication':'not_authenticated'}.get(failure['code'],'unknown')
                    observe(failure['provider'], state, job.get('completed_at'), 'engineering_failure', 'historical', failure['code'])
        except Exception: pass
        if brain['available']:
            with self.store.database.transaction() as db:
                rows = db.execute("""SELECT event FROM activity WHERE task_id=? AND json_extract(event,'$.type')='brain_route'
                    AND json_extract(event,'$.status') IN ('completed','failed','incomplete') AND sequence>
                    (SELECT max(sequence) FROM activity WHERE task_id=? AND json_extract(event,'$.type')='brain_runtime')
                    ORDER BY sequence DESC LIMIT 128""", (self.task_id,self.task_id)).fetchall()
            for row in reversed(rows):
                event = json.loads(row[0])
                observe(event.get('provider'), 'usable' if event.get('status') == 'completed' else 'unknown',
                        event.get('timestamp'), 'brain_route', 'current_task', event.get('status'))
        if self.route and self.configured is not None and self.route not in self.configured:
            availability[self.route] = dict(status='unavailable',current_usability='unavailable',as_of=now,scope='chat_adapter',source='provider_registry')
        for provider, value in availability.items():
            value['chat_adapter_registered'] = None if self.configured is None else provider in self.configured
            value['last_success'] = max((o['as_of'] for o in observations if o['provider']==provider and o['status']=='usable' and o['as_of']), default=None)
        if result['portfolio'] is not None:
            from .engineering_portfolio import Evidence, overlay_capabilities
            from .engineering_decision import task_capability_observations
            changes = task_capability_observations(self.store.database, self.task_id)
            def capability(cid, state, at, source, scope):
                evidence = Evidence(source=source, scope=scope, observed_at=at).model_dump()
                changes[cid] = dict(current_usability=state, evidence=evidence)
                if state == 'available': changes[cid]['last_success'] = evidence
                elif state not in {'unknown', 'unproven'}: changes[cid]['last_failure'] = evidence
            capability('self-state', 'available', now, 'task_runtime', 'current snapshot construction')
            capability('memory', 'available', now, 'task_runtime', 'current SQLite task-state read; not all memory operations')
            result['portfolio']['capabilities'], conflicts = overlay_capabilities(result['portfolio']['capabilities'], changes, now)
            result['portfolio']['contradictions'] = (result['portfolio']['contradictions'] + conflicts)[:64]
        facts = dict(jarvisbrain_enabled=brain['jarvisbrain_enabled'], worker=result['engineering']['worker'])
        facts.update({'production.revision':result['production']['revision'], 'deployment.state':result['deployment']['state']})
        facts.update({'approval.'+k:v for k,v in result['approvals'].items()})
        facts.update({'job.'+v['id']+'.state':v['status'] for v in result['engineering']['jobs']})
        facts.update({'candidate.'+v['id']+'.state':v['state'] for v in result['engineering']['candidates']})
        facts.update({'provider.'+p+'.status':v['current_usability'] for p,v in availability.items()})
        registry = result['engineering']['provider_registry'] or {}
        facts.update({'engineering_provider.'+p['provider_id']+'.status':p['current_usability']
                      for p in registry.get('providers', [])})
        if result['portfolio'] is not None:
            facts.update({'capability.'+c['capability_id']+'.status': c['current_usability'] for c in result['portfolio']['capabilities']})
            facts.update({'project.'+p['project_id']+'.status': p['status'] for p in result['portfolio']['projects']})
            facts.update({'project.'+p['project_id']+'.repository_inspection': p['repository_inspection']['available']
                          for p in result['portfolio']['projects'] if p['repository_inspection'] is not None})
        claimed_tools = [n.lower() for n in re.findall(r'(?:tool[. ]|capability )([a-z][a-z0-9_]{0,63})',json.dumps(history)[:256000],re.I)]
        facts.update({'tool.'+n+'.available':n in names for n in set(names[:128]+claimed_tools[:128]) if n in names or len(names)<=128})
        result['facts'] = facts
        result['contradictions'] = contradictions(history, facts)
        return result

    def enrich(self, request):
        snapshot = self.snapshot(request['input'], request['tools'])
        return dict(request, instructions=request['instructions'].split(MARKER,1)[0] + MARKER +
                    json.dumps(snapshot,sort_keys=True) + '\n</jarvis_self_state>\n' + GUIDANCE)
