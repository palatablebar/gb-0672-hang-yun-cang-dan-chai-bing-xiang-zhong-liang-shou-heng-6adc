from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.engine import evaluate
from app.modules.repair import rules


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'repair.sqlite'))
    return TestClient(main.app)


def test_dirty_cycle_has_no_unique_edge(sample):
    result = rules.build(sample)
    cycle = next(c for c in result['components'] if c['kind'] == 'DEPENDENCY_CYCLE')
    assert cycle['nodes'] == ['LOOP-A', 'LOOP-B']
    assert cycle['valid_count'] == 2
    assert cycle['unique'] is None
    assert cycle['status'] == '待补齐'
    assert '无法唯一定位' in cycle['basis']
    assert result['selected'] == []


def test_dirty_orphan_waits_for_missing_parent(sample):
    result = rules.build(sample)
    orphan = next(c for c in result['components'] if c['kind'] == 'MISSING_PARENT')
    assert orphan['nodes'] == ['ORPHAN']
    assert orphan['external_parent'] == 'UNKNOWN'
    assert orphan['status'] == '待补齐'


def test_dirty_masters_keep_original_totals_and_scope(sample):
    result = rules.build(sample)
    masters = {m['id']: m for m in result['masters']}
    assert set(masters) == {'M001', 'M002', 'M003', 'M004', 'M005', 'M006'}
    assert (masters['M001']['repair_pieces'], masters['M001']['repair_net_kg'],
            masters['M001']['repair_gross_kg']) == (40, '400', '441')
    assert masters['M001']['sealable'] == '否'
    assert 'LOOP-A' not in masters['M001']['impact'] or '不在本主单子树' in masters['M001']['impact']
    assert masters['M002']['sealable'] == '是'
    assert '错过' not in masters['M002']['impact']
    assert result['batch_sealable'] is False


def test_dirty_verdict_releases_unaffected_masters(sample):
    verdict = rules.build(sample)['verdict']
    assert '不切断任何关系' in verdict
    assert 'M002' in verdict and '整批不封存' in verdict


def test_wait_group_policy_holds_every_master(sample):
    result = rules.build(sample, policy='wait_group')
    assert result['selected'] == []
    assert all(m['sealable'] == '否' for m in result['masters'])
    assert result['comparison'][0]['masters_sealable'] == 5
    assert result['comparison'][1]['masters_sealable'] == 0


def test_clean_batch_seals(sample_clean):
    result = rules.build(sample_clean)
    assert result['components'] == []
    assert result['batch_sealable'] is True
    assert all(m['sealable'] == '是' for m in result['masters'])


@pytest.fixture
def sample_clean():
    return evaluate({'dataset': 'clean'})


UNIQUE_CYCLE = '''id,parent,packages,package_unit,net,gross,weight_unit,hazard,un_code,container,revision
X,Y,3,carton,3,6,kg,none,,C1,1
Y,X,2,carton,2,4,kg,none,,C1,1
D,X,1,carton,1,2,kg,none,,C1,1
'''


def test_unique_edge_repairs_only_supported_cut():
    context = {'declaration': {'pieces': 3, 'net_kg': '3', 'gross_kg': '6'}}
    state = evaluate({'csv_text': UNIQUE_CYCLE, 'overrides': context})
    result = rules.build(state)
    cycle = result['components'][0]
    assert cycle['status'] == '已修复'
    assert cycle['unique']['child'] == 'X'
    assert cycle['unique']['parent'] == 'Y'
    assert result['selected'] == [{'child': 'X', 'parent': 'Y'}]
    master = next(m for m in result['masters'] if m['id'] == 'X')
    assert (master['repair_pieces'], master['repair_net_kg'], master['repair_gross_kg']) == (3, '3', '6')
    assert master['sealable'] == '是'
    assert result['batch_sealable'] is True


def test_self_reference_is_unique():
    csv_text = ('id,parent,packages,package_unit,net,gross,weight_unit,hazard,un_code,container,revision\n'
                'A,A,1,carton,1,2,kg,none,,C,1\n')
    state = evaluate({'csv_text': csv_text,
                      'overrides': {'declaration': {'pieces': 1, 'net_kg': '1', 'gross_kg': '2'}}})
    result = rules.build(state)
    assert result['components'][0]['status'] == '已修复'
    assert result['selected'] == [{'child': 'A', 'parent': 'A'}]


def test_repair_does_not_mutate_evidence(sample):
    before = deepcopy(sample['records'])
    rules.build(sample)
    assert sample['records'] == before


def test_decision_receipt_is_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c:
        run = c.post('/api/runs', json={}).json()
        rid = run['manifest']['run_id']
        first = c.post('/api/repair/' + rid + '/decide', json={}).json()
        second = c.post('/api/repair/' + rid + '/decide', json={}).json()
        assert first['receipt'] == second['receipt']
        assert first['version'] == second['version'] == 1
        assert second['idempotent'] is True
        assert c.get('/api/repair/' + rid).json()['version'] == 1


def test_unsupported_edge_restores_previous_decision(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c:
        rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
        c.post('/api/repair/' + rid + '/decide', json={'policy': 'wait_group'})
        bad = c.post('/api/repair/' + rid + '/decide',
                     json={'edges': [{'child': 'LOOP-A', 'parent': 'LOOP-B'}]})
        assert bad.status_code == 409
        assert bad.json()['code'] == 'UNSUPPORTED_EDGE'
        state = c.get('/api/repair/' + rid).json()
        assert state['version'] == 1
        assert state['active']['edges'] == []
        assert state['attempts'][0]['code'] == 'UNSUPPORTED_EDGE'
        # 同一失败回执重试不新增尝试记录
        c.post('/api/repair/' + rid + '/decide', json={'edges': [{'child': 'LOOP-A', 'parent': 'LOOP-B'}]})
        assert len(c.get('/api/repair/' + rid).json()['attempts']) == 1


def test_restore_switches_back_to_previous_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c:
        rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
        v1 = c.post('/api/repair/' + rid + '/decide', json={}).json()
        v2 = c.post('/api/repair/' + rid + '/decide', json={'policy': 'wait_group'}).json()
        assert (v1['version'], v2['version']) == (1, 2)
        restored = c.post('/api/repair/' + rid + '/restore', json={'version': v1['version']}).json()
        assert restored['restored_from'] == 1
        assert restored['decision']['policy'] == 'unique_only'
        assert restored['decision']['masters'] == v1['decision']['masters']
        again = c.post('/api/repair/' + rid + '/restore', json={'version': v1['version']}).json()
        assert again['version'] == restored['version']
        assert again['idempotent'] is True
        missing = c.post('/api/repair/' + rid + '/restore', json={'version': 99})
        assert missing.status_code == 404
        assert c.get('/api/repair/' + rid).json()['active']['version'] == restored['version']


def test_repair_page_has_master_rulings(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c:
        rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
        c.post('/api/repair/' + rid + '/decide', json={})
        html = c.get('/repair?run_id=' + rid).text
        assert '环路修复裁定' in html
        assert 'M002' in html and 'M001' in html
        assert '能否封存' in html
        assert '修复后净重 kg' in html
        assert '回执' in html


def test_repair_module_is_in_workbench(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c:
        assert c.post('/api/repair', json={}).status_code == 200
        assert c.get('/repair').status_code == 200
        assert '/repair?run_id=' in c.get('/archive').text
