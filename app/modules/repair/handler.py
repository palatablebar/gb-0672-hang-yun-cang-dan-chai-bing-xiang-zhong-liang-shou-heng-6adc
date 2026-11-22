import json
from fastapi import APIRouter, Request, Body
from app import store
from app.config import MODULES
from app.engine import evaluate
from app.errors import BusinessError
from . import rules

router = APIRouter()


def _db_path():
    from app.main import DB_PATH
    return DB_PATH


def _run(run_id):
    return store.get(_db_path(), run_id)


def _change_set(base):
    return {
        'edges': [{'child': e['child'], 'parent': e['parent']} for e in base['selected']],
        'policy': base['policy'],
        'policy_label': base['policy_label'],
        'masters': [{k: m[k] for k in ('id', 'kind', 'basis', 'repair_pieces', 'repair_net_kg',
                                       'repair_gross_kg', 'impact', 'sealable', 'release')}
                    for m in base['masters']],
        'components': [{'kind': c['kind'], 'nodes': c['nodes'], 'status': c['status'],
                        'basis': c['basis']} for c in base['components']],
        'batch_sealable': base['batch_sealable'],
        'batch_blockers': base['batch_blockers'],
        'verdict': base['verdict'],
        'comparison': base['comparison'],
    }


def _validate(payload):
    policy = payload.get('policy', 'unique_only')
    if policy not in rules.POLICIES:
        raise BusinessError('BAD_POLICY', '修复策略须为 unique_only 或 wait_group')
    edges = payload.get('edges')
    if edges is not None and (not isinstance(edges, list) or any(
            not isinstance(e, dict) or not e.get('child') or not e.get('parent') for e in edges)):
        raise BusinessError('BAD_EDGES', '断边须为 {child,parent} 列表')
    return policy, edges


@router.get('/repair')
def page(request: Request):
    from app.web import templates
    from app.config import META
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    run_id = state['manifest']['run_id']
    decisions = store.repair_decisions(DB_PATH, run_id)
    active = decisions['active']
    if active:
        policy = active['policy'] if active['policy'] in rules.POLICIES else 'unique_only'
        shown = rules.build(state, policy=policy, edges=active['edges'])
        shown['run_id'] = run_id
        shown['receipt'] = rules.receipt_id(run_id, shown['receipt_basis'])
        shown['active'] = active
    else:
        if 'repair' not in state:
            fallback = rules.build(state)
            state['repair'] = fallback
            state['views']['repair'] = fallback['view']
        shown = dict(state['repair'])
        shown['run_id'] = run_id
        shown['receipt'] = rules.receipt_id(run_id, shown['receipt_basis'])
        shown['active'] = None
    return templates.TemplateResponse(
        request=request, name='repair.html',
        context={'meta': META, 'key': 'repair', 'state': state, 'view': state['views']['repair'],
                 'repair': shown, 'decisions': decisions})


@router.post('/api/repair')
def calculate(payload: dict = Body(default={})):
    if payload.get('run_id'):
        run_id = payload['run_id']
        run = _run(run_id)
        policy, edges = _validate(payload)
        result = rules.build(run, policy=policy, edges=edges)
        result['run_id'] = run_id
        result['receipt'] = rules.receipt_id(run_id, result['receipt_basis'])
        result['state'] = store.repair_decisions(_db_path(), run_id)
        return result
    return evaluate(payload, until=len(MODULES))


@router.post('/api/repair/{run_id}/decide')
def decide(run_id: str, payload: dict = Body(default={})):
    run = _run(run_id)
    policy, edges = _validate(payload)
    base = rules.build(run, policy=policy, edges=edges)
    if base['selection_errors']:
        detail = '；'.join(base['selection_errors'])
        receipt = rules.receipt_id(run_id, base['receipt_basis'])
        store.record_repair_attempt(_db_path(), run_id, receipt, policy,
                                    json.dumps(edges, ensure_ascii=False),
                                    'UNSUPPORTED_EDGE', detail)
        state = store.repair_decisions(_db_path(), run_id)
        raise BusinessError(
            'UNSUPPORTED_EDGE',
            detail + (f"；已保留上一版裁定 v{state['version']}" if state['version'] else '；当前没有已生效裁定'),
            409)
    receipt = rules.receipt_id(run_id, base['receipt_basis'])
    saved, created = store.save_repair(_db_path(), run_id, policy, base['selected'], receipt, _change_set(base))
    state = store.repair_decisions(_db_path(), run_id)
    return {
        'run_id': run_id,
        'receipt': saved['receipt'],
        'version': saved['version'],
        'created': created,
        'idempotent': not created,
        'decision': saved,
        'state': state,
    }


@router.post('/api/repair/{run_id}/restore')
def restore(run_id: str, payload: dict = Body(default={})):
    run = _run(run_id)
    version = payload.get('version')
    if not isinstance(version, int) or version < 1:
        raise BusinessError('BAD_VERSION', '版本号须为正整数')
    state = store.repair_decisions(_db_path(), run_id)
    target = next((d for d in state['history'] if d['version'] == version), None)
    if target is None:
        store.record_repair_attempt(_db_path(), run_id, 'restore-v' + str(version), 'restore', '[]',
                                    'VERSION_NOT_FOUND',
                                    f'裁定版本 v{version} 不存在，已保留上一版 v{state["version"]}')
        raise BusinessError('VERSION_NOT_FOUND', '裁定版本不存在，已恢复上一版', 404)
    policy = target['policy'] if target['policy'] in rules.POLICIES else 'unique_only'
    check = rules.build(run, policy=policy, edges=target['edges'])
    if check['selection_errors']:
        detail = '；'.join(check['selection_errors'])
        store.record_repair_attempt(_db_path(), run_id, 'restore-v' + str(version), policy,
                                    json.dumps(target['edges'], ensure_ascii=False),
                                    'RESTORE_FAILED',
                                    detail + f'；已保留上一版 v{state["version"]}')
        raise BusinessError('RESTORE_FAILED', detail + f'；已恢复上一版 v{state["version"]}', 409)
    saved, created = store.restore_repair(_db_path(), run_id, version)
    latest = store.repair_decisions(_db_path(), run_id)
    return {
        'run_id': run_id,
        'restored_from': version,
        'version': saved['version'],
        'receipt': saved['receipt'],
        'created': created,
        'idempotent': not created,
        'decision': saved,
        'state': latest,
    }


@router.get('/api/repair/{run_id}')
def decision(run_id: str):
    _run(run_id)
    return store.repair_decisions(_db_path(), run_id)
