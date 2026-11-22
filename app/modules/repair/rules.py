"""环路修复裁定。

原则：只处理证据能唯一定位的错误关系。候选断边必须同时通过图校验与逐层
守恒；若同一组件存在多条等价候选，或断边会改变主单申报总量，则不猜测、
不切断，整组挂起等待补齐。本模块只生成裁定与修复后视图，不修改原始
records、采信隔离与危险品证据。
"""
from collections import defaultdict
from decimal import Decimal
import hashlib, json

from app.modules.grouping.rules import graph as build_graph
from app.modules.weight.rules import check_weights

POLICIES = {
    'unique_only': '只处理证据能唯一定位的错误关系',
    'wait_group': '等整组补齐',
}
FIELDS = ('pieces', 'net_kg', 'gross_kg')


def _dec(value):
    return Decimal(str(value))


def _parent(row):
    return (row.get('parent') or '').strip()


def _node_rows(rows):
    return {r['id']: r for r in rows}


def _descendants(root, children):
    seen, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children.get(node, []))
    return seen


def _totals(rows):
    pieces = sum(r['pieces'] for r in rows)
    net = sum((_dec(r['net_kg']) for r in rows), Decimal(0))
    gross = sum((_dec(r['gross_kg']) for r in rows), Decimal(0))
    return {'pieces': pieces, 'net_kg': net, 'gross_kg': gross}


def _fmt(totals):
    return {f: (totals[f] if f == 'pieces' else format(totals[f], 'f')) for f in FIELDS}


def _declaration(state):
    value = state['context'].get('declaration', {})
    return value if isinstance(value, dict) else {}


def _matches(totals, decl):
    return all(_dec(totals[f]) == _dec(decl[f]) for f in FIELDS if f in decl)


def _deviation(totals, decl):
    return sum(abs(_dec(totals[f]) - _dec(decl[f])) for f in FIELDS if f in decl)


def _master_declarations(state):
    value = state['context'].get('master_declaration') or state['context'].get('masters') or {}
    return value if isinstance(value, dict) else {}


def _find_cycles(parent):
    """函数图（每节点最多一个上级）上的环检测，稳定排序输出。"""
    nodes = set(parent)
    state, cycles = {}, []
    for start in sorted(nodes):
        if state.get(start):
            continue
        path, index, node = [], {}, start
        while node in nodes and state.get(node, 0) == 0:
            state[node] = 1
            index[node] = len(path)
            path.append(node)
            node = parent.get(node, '')
        if node in index and state.get(node) == 1:
            cycles.append(path[index[node]:])
        for item in path:
            state[item] = 2
    return sorted(cycles, key=lambda c: (min(c), len(c), c))


def _missing_groups(rows):
    parent = {r['id']: _parent(r) for r in rows}
    nodes = set(parent)
    groups = defaultdict(list)
    for child, par in parent.items():
        if par and par not in nodes:
            groups[par].append(child)
    return parent, groups


def _apply_edges(rows, edges):
    cuts = {(e['child'], e['parent']) for e in edges}
    repaired = []
    for row in rows:
        row = dict(row)
        if (row['id'], _parent(row)) in cuts:
            row['parent'] = ''
        repaired.append(row)
    return repaired


def _graph_valid(repaired_rows, component_nodes):
    g = build_graph(repaired_rows)
    for err in g['errors']:
        if err['code'] == 'DEPENDENCY_CYCLE' and set(err.get('path', [])) & set(component_nodes):
            return False, g
    roots = set(g['roots'])
    rows = _node_rows(repaired_rows)
    for node in component_nodes:
        cursor, seen = node, set()
        while cursor and cursor not in roots:
            if cursor in seen or cursor not in rows:
                return False, g
            seen.add(cursor)
            cursor = _parent(rows[cursor])
    return True, g


def _component_balance(repaired_rows, g, root, component_nodes):
    checks = check_weights(repaired_rows, g)
    subtree = _descendants(root, g['children'])
    if not set(component_nodes) <= subtree:
        return False, checks, '断边后组件未全部落在同一主单子树下'
    relevant = [c for c in checks if c['id'] in subtree]
    bad = [c for c in relevant if c['code'] != 'BALANCED']
    if bad:
        detail = '；'.join(
            f"{c['id']} {c['field']} 申报{c['declared']} 子单合计{c['children_sum']}" for c in bad)
        return False, checks, f'断边后逐层不守恒：{detail}'
    if not relevant:
        if len(component_nodes) == 1 and not g['children'].get(root):
            return True, checks, '断边后为单节点主单，无下级关系可校验'
        return False, checks, '断边后没有可校验的层级关系'
    return True, checks, '断边后逐层守恒'


def _candidate_label(child, parent):
    return f'断开 {child} 的上级 {parent}'


def _candidates(state, rows, parent, component_nodes, kind):
    decl = _declaration(state)
    masters = _master_declarations(state)
    pre_totals = _totals([r for r in rows if not _parent(r)])
    pre_match = _matches(pre_totals, decl)
    pre_deviation = _deviation(pre_totals, decl)
    out = []
    for child in sorted(component_nodes):
        par = parent.get(child, '')
        edge = {'parent': par, 'child': child}
        repaired = _apply_edges(rows, [edge])
        valid, g = _graph_valid(repaired, component_nodes)
        balanced, checks, note = (False, [], '断边后无法校验')
        root_row = _node_rows(repaired).get(child)
        if valid and root_row is not None:
            balanced, checks, note = _component_balance(repaired, g, child, component_nodes)
        post_totals = _totals([r for r in repaired if not _parent(r)])
        post_match = _matches(post_totals, decl)
        post_deviation = _deviation(post_totals, decl)
        counter = []
        if decl:
            if pre_match and not post_match:
                counter.append('断边前主单合计与申报清单一致，断边后不一致')
            elif not pre_match and post_deviation > pre_deviation:
                counter.append('断边使主单合计偏离申报清单更远')
        master_decl = masters.get(child)
        master_match = bool(root_row is not None and isinstance(master_decl, dict)
                            and all(_dec(root_row[f]) == _dec(master_decl[f]) for f in FIELDS if f in master_decl))
        if kind == 'MISSING_PARENT':
            note = (f'升为主单后上级 {par} 不再引用（该节点不在本批）；'
                    '结构上可行，但需要外部申报证据才能确认归属') if (valid and balanced) else note
        out.append({
            'pre_match': bool(pre_match),
            'edge': edge,
            'child': child,
            'parent': par,
            'label': _candidate_label(child, par),
            'valid': bool(valid and balanced),
            'graph_valid': bool(valid),
            'conservation': bool(balanced),
            'reason': note if valid else '断边后仍存在环路或无法回溯到根',
            'post_totals': _fmt(post_totals),
            'post_match': bool(post_match),
            'master_match': master_match,
            'counter': counter,
            'evidence': [],
        })
    return out


def _mark_evidence(candidates, kind):
    """只把能唯一指向某条边的证据写进候选；多条等价候选时不做猜测。"""
    valid = [c for c in candidates if c['valid']]
    structural = False
    if kind == 'DEPENDENCY_CYCLE' and len(valid) == 1:
        valid[0]['evidence'].append('唯一通过图校验且逐层守恒的断边')
        structural = True
    for cand in valid:
        if cand['child'] == cand['parent'] and '自环' not in ''.join(cand['evidence']):
            cand['evidence'].append('自环：节点把自己声明为上级，唯一可断开的关系')
            structural = True
    # 外部申报：断边前主单合计与申报清单不一致、断边后完全一致，且这种候选只有一条。
    decl_positive = [c for c in valid if c['post_match'] and not c['pre_match'] and not c['counter']]
    if len(decl_positive) == 1:
        decl_positive[0]['evidence'].append('断边前主单合计与申报清单不一致，断边后完全一致')
    # 独立主单申报：升为主单后的申报能与外部主单申报逐项对上，且只有一条。
    master_positive = [c for c in valid if c['master_match'] and not c['counter']]
    if len(master_positive) == 1:
        master_positive[0]['evidence'].append('升为主单后与独立主单申报逐项一致')
    supported = []
    for cand in candidates:
        if not cand['valid'] or cand['counter'] or not cand['evidence']:
            continue
        if structural:
            # 结构唯一时，其他候选只能作为并列证据，不再单独计数。
            if '唯一通过图校验且逐层守恒的断边' in cand['evidence'] or '自环' in ''.join(cand['evidence']):
                supported.append(cand)
            continue
        supported.append(cand)
    deduped = []
    for cand in supported:
        if cand not in deduped:
            deduped.append(cand)
    return deduped


def _analyze_component(state, rows, kind, nodes, path=None, external=None):
    parent = {r['id']: _parent(r) for r in rows}
    component_nodes = sorted(set(nodes))
    candidates = _candidates(state, rows, parent, component_nodes, kind)
    supported = _mark_evidence(candidates, kind)
    unique = supported[0] if len(supported) == 1 else None
    node_set = set(component_nodes)
    lines = {r['id']: r.get('_line') for r in rows if r['id'] in node_set}
    containers = sorted({r.get('container', '') for r in rows if r['id'] in node_set and r.get('container', '')})
    if unique:
        basis = '；'.join(unique['evidence']) or '唯一可复核断边'
    elif kind == 'DEPENDENCY_CYCLE':
        valid_count = len([c for c in candidates if c['valid']])
        countered = [c for c in candidates if c['counter']]
        if valid_count > 1:
            basis = (f'证据无法唯一定位：{valid_count} 条候选断边均通过图校验且逐层守恒，'
                     '断边后主单合计也相同；任选一条都会改错依据')
            if countered and len(countered) == valid_count:
                basis += '；且任一断边都会使主单合计偏离申报清单，不能据此放行'
        elif valid_count == 0:
            basis = '没有候选断边能同时通过图校验与逐层守恒，不能靠断边修复'
        else:
            basis = '候选断边存在与申报清单相反的证据，无法唯一定位'
    else:
        valid_count = len([c for c in candidates if c['valid']])
        if valid_count:
            basis = (f'缺父级 {external} 不在本批：升为主单在结构上可行，但'
                     '没有独立申报证据能唯一定位该节点的归属，因此整组挂起等待补齐')
        else:
            basis = (f'缺父级 {external} 不在本批：补齐上级前无法确认 {",".join(component_nodes)} 的归属；'
                     '也没有独立申报证据能证明该节点应升为主单')
    return {
        'kind': kind,
        'nodes': component_nodes,
        'path': (path or []) + ([external] if external else []),
        'external_parent': external,
        'lines': lines,
        'containers': containers,
        'candidates': candidates,
        'candidate_count': len(candidates),
        'valid_count': len([c for c in candidates if c['valid']]),
        'supported_count': len(supported),
        'unique': unique,
        'status': '已修复' if unique else '待补齐',
        'basis': basis,
    }


def analyze_components(state):
    rows = state['records']
    parent = {r['id']: _parent(r) for r in rows}
    components, seen = [], set()
    for cycle in _find_cycles(parent):
        key = tuple(sorted(cycle))
        if key in seen:
            continue
        seen.add(key)
        components.append(_analyze_component(
            state, rows, 'DEPENDENCY_CYCLE', cycle, path=cycle + [cycle[0]]))
    _, missing = _missing_groups(rows)
    for external in sorted(missing):
        components.append(_analyze_component(state, rows, 'MISSING_PARENT', missing[external], external=external))
    return components


def _plan(state, policy, edges, components):
    if policy == 'wait_group':
        return [], (['等整组补齐策略下不切断任何关系'] if edges else [])
    auto = [c['unique']['edge'] for c in components if c['unique']]
    if edges is None:
        return auto, []
    supported = {(c['unique']['child'], c['unique']['parent']): c for c in components if c['unique']}
    selected, errors = [], []
    for edge in edges:
        key = (edge.get('child'), edge.get('parent'))
        if key not in supported:
            errors.append(
                f"断边 {key[0]}→{key[1]} 不在证据可唯一定位的范围内；"
                '若强行切断会改错依据，已恢复上一版裁定')
            continue
        if not any((e['child'], e['parent']) == key for e in selected):
            selected.append({'child': key[0], 'parent': key[1]})
    return selected, errors


def _issues_for_root(state, rows_by_id, g, checks, root, held_nodes):
    subtree = _descendants(root, g['children'])
    issues = []
    for err in g['errors']:
        if err.get('id') in subtree or set(err.get('path', [])) & subtree:
            issues.append(f"图错误 {err.get('code')}：{err.get('id')} {'→'.join(err.get('path', []))}")
    for check in checks:
        if check['id'] in subtree and check['code'] != 'BALANCED':
            issues.append(
                f"{check['id']} {check['field']} 申报 {check['declared']}，子单合计 {check['children_sum']}，差 {check['difference']}")
    containers = {rows_by_id[node].get('container', '') for node in subtree if node in rows_by_id}
    for hazard in state.get('hazard_issues', []):
        if hazard.get('id') in subtree:
            issues.append(f"危险品 {hazard.get('code')}：{hazard.get('id')}")
        elif hazard.get('code') == 'INCOMPATIBLE' and hazard.get('id') in containers:
            issues.append(f"危险品 {hazard.get('code')}：箱号 {hazard.get('id')} {hazard.get('classes')}")
    for item in state.get('quarantine', []):
        raw = item.get('raw') or {}
        if (raw.get('parent') or '').strip() in subtree:
            issues.append(f"采信隔离 {item.get('code')}：{item.get('id')}（上级落在本主单子树）")
    master_decl = _master_declarations(state).get(root)
    if isinstance(master_decl, dict):
        bad = [f for f in FIELDS if f in master_decl and _dec(rows_by_id[root][f]) != _dec(master_decl[f])]
        if bad:
            issues.append(f"独立主单申报不一致：{', '.join(bad)}")
    if root in held_nodes:
        issues.append('本主单候选根属于待补齐组件')
    return issues


def _impact(state, root, subtree, components, promoted_edge=None):
    if promoted_edge:
        comp = next((c for c in components if c['unique']
                     and c['unique']['child'] == promoted_edge['child']), None)
        nodes = ','.join(comp['nodes']) if comp else promoted_edge['child']
        return (f"由组件 {nodes} 断边后在本次裁定中升为独立主单；"
                f"断开 {promoted_edge['child']} 的上级 {promoted_edge['parent']}，"
                f"原上级不再计入本票；本票子树 {len(subtree)} 个节点")
    held = [c for c in components if c['status'] != '已修复']
    if not held:
        return f"本主单及子树 {len(subtree)} 个节点；修复未改动本主单层级"
    labels = '、'.join(','.join(c['nodes']) for c in held)
    return (f"本主单及子树 {len(subtree)} 个节点；待补齐组件 {labels} 均为独立组件，"
            '不在本主单子树，不牵连本主单件净毛重')


def _masters(state, policy, repaired_rows, g, checks, components, selected):
    held_nodes = set()
    for comp in components:
        if comp['status'] != '已修复':
            held_nodes.update(comp['nodes'])
    selection = {e['child']: e for e in selected}
    rows_by_id = _node_rows(repaired_rows)
    masters = []
    for root in g['roots']:
        row = rows_by_id[root]
        subtree = _descendants(root, g['children'])
        issues = _issues_for_root(state, rows_by_id, g, checks, root, held_nodes)
        if policy == 'wait_group':
            issues = ['等整组补齐策略下整批挂起，未受影响主单也等待，可能错过截单']
        if root in held_nodes:
            issues.append('本节点属于待补齐组件，不能升为主单放行')
        promoted_edge = selection.get(root)
        if promoted_edge:
            comp = next((c for c in components if c['unique'] and c['unique']['child'] == root), None)
            evidence = ''
            if comp and comp['unique']['evidence']:
                evidence = '；' + '；'.join(comp['unique']['evidence'])
            basis = f"修复后升为主单：{_candidate_label(root, promoted_edge['parent'])}{evidence}"
            kind = '修复后升为主单'
        else:
            mismatch = any(c['id'] == root and c['code'] != 'BALANCED' for c in checks)
            tail = '；主单原申报与子单合计存在差异，原样保留并在封存说明中阻断' if mismatch else ''
            basis = (f'原始层级未改动：采用主单原申报值，未改写件净毛重；'
                     f'子树 {len(subtree)} 个节点{tail}')
            kind = '原主单'
        masters.append({
            'id': root,
            'kind': kind,
            'basis': basis,
            'impact': _impact(state, root, subtree, components, promoted_edge),
            'repair_pieces': row['pieces'],
            'repair_net_kg': row['net_kg'],
            'repair_gross_kg': row['gross_kg'],
            'sealable': '否' if issues else '是',
            'seal_reason': '子树内图关系、逐层重量、危险品与采信隔离均无阻断' if not issues else '；'.join(issues),
            'release': '挂起' if issues else '可逐票封存并继续申报',
        })
    return masters


def _batch_blockers(state, repaired_rows, g, checks, components, policy, masters):
    blockers = []
    if policy == 'wait_group':
        blockers.append('等整组补齐策略：整批挂起，未受影响主单也未放行')
    for comp in components:
        if comp['status'] != '已修复':
            blockers.append(f"{comp['kind']} 组件 {','.join(comp['nodes'])} 待补齐")
    for check in checks:
        if check['code'] != 'BALANCED':
            blockers.append(f"{check['id']} {check['field']} 差 {check['difference']}")
    for hazard in state.get('hazard_issues', []):
        blockers.append(f"危险品 {hazard.get('code')}：{hazard.get('id')}")
    for item in state.get('quarantine', []):
        blockers.append(f"采信隔离 {item.get('code')}：{item.get('id')}")
    decl = _declaration(state)
    totals = _totals([r for r in repaired_rows if not _parent(r)])
    if decl and not _matches(totals, decl):
        blockers.append(
            f"主单申报合计 {totals['pieces']}/{format(totals['net_kg'], 'f')}/{format(totals['gross_kg'], 'f')} 与申报清单不一致")
    blocked = [m['id'] for m in masters if m['sealable'] != '是']
    if blocked:
        blockers.append('未封存主单：' + ','.join(blocked))
    return blockers


def _verdict(policy, components, masters, blockers):
    repaired = [c for c in components if c['status'] == '已修复']
    held = [c for c in components if c['status'] != '已修复']
    sealable = [m['id'] for m in masters if m['sealable'] == '是']
    if policy == 'wait_group':
        return ('裁定：采用等整组补齐。不切断任何关系，全部主单等待整组补齐，整批不封存；'
                f'未受影响的 {len(masters)} 张主单也会错过本班截单。')
    head = '采用原则：只处理证据能唯一定位的错误关系；证据不唯一时整组挂起，不猜测断边。'
    if not components:
        return head + ('本批没有拆并环路或缺失父级，'
                       + ('全部主单可按各自子树逐票封存。' if not blockers else '主单仍受其他问题阻断，逐票见下表。'))
    if not repaired:
        count = f'{len(held)} 个图错误组件' if len(held) != 1 else '1 个图错误组件'
        return (head + f'本批 {count}无法由证据唯一定位，因此不切断任何关系，'
                f'组件整组挂起等待补齐；' + (f'未受影响的 {len(sealable)} 张主单（{",".join(sealable)}）逐票可封存，'
                                            if sealable else '')
                + '整批不封存，也不隔离组件以外的货物。')
    if not held:
        count = f'{len(repaired)} 个组件均只有' if len(repaired) != 1 else '1 个组件只有'
        return (head + f'本批 {count}唯一可复核断边，全部按证据修复；'
                + (f'{len(sealable)} 张主单逐票可封存。' if sealable else '修复后仍有主单未封存。'))
    return (head + f'本批修复 {len(repaired)} 个证据唯一定位的组件，{len(held)} 个组件整组挂起；'
            + (f'未受影响的 {len(sealable)} 张主单可逐票封存，' if sealable else '')
            + '整批不封存。')


def build(state, policy='unique_only', edges=None, applied=False, compare=True):
    if policy not in POLICIES:
        policy = 'unique_only'
    components = analyze_components(state)
    selected, selection_errors = _plan(state, policy, edges, components)
    repaired_rows = _apply_edges(state['records'], selected)
    g = build_graph(repaired_rows)
    checks = check_weights(repaired_rows, g)
    for comp in components:
        if comp['unique'] and any((e['child'], e['parent']) == (comp['unique']['child'], comp['unique']['parent'])
                                  for e in selected):
            comp['status'] = '已修复'
            comp['selected'] = {k: comp['unique'][k] for k in ('child', 'parent', 'label')}
    masters = _masters(state, policy, repaired_rows, g, checks, components, selected)
    blockers = _batch_blockers(state, repaired_rows, g, checks, components, policy, masters)
    verdict = _verdict(policy, components, masters, blockers)
    comparison = [_policy_summary(state, p) for p in ('unique_only', 'wait_group')] if compare else []
    receipt_basis = {
        'policy': policy,
        'edges': sorted([{'child': e['child'], 'parent': e['parent']} for e in selected],
                        key=lambda e: (e['child'], e['parent'])),
        'verdict': verdict,
        'masters': [{'id': m['id'], 'sealable': m['sealable'],
                     'pieces': m['repair_pieces'], 'net_kg': m['repair_net_kg'],
                     'gross_kg': m['repair_gross_kg']} for m in masters],
        'batch_sealable': not blockers,
    }
    rows_view = [{k: m[k] for k in ('id', 'kind', 'basis', 'repair_pieces', 'repair_net_kg',
                                    'repair_gross_kg', 'impact', 'sealable', 'release')} for m in masters]
    component_view = [{
        'component': ','.join(c['nodes']) + (f'（缺 {c["external_parent"]}）' if c['external_parent'] else ''),
        'kind': '依赖环' if c['kind'] == 'DEPENDENCY_CYCLE' else '缺父级',
        'nodes': ','.join(c['nodes']),
        'path': '→'.join(c['path']),
        'lines': ','.join(str(c['lines'].get(n)) for n in c['nodes']),
        'containers': ','.join(c['containers']),
        'status': c['status'],
        'basis': c['basis'],
    } for c in components]
    candidate_view = [{
        'component': ','.join(c['nodes']),
        'candidate': cand['label'],
        'valid': '是' if cand['valid'] else '否',
        'conservation': '守恒' if cand['conservation'] else '不守恒',
        'totals': f"{cand['post_totals']['pieces']}/{cand['post_totals']['net_kg']}/{cand['post_totals']['gross_kg']}",
        'evidence': '；'.join(cand['evidence']) if cand['evidence'] else ('证据相反：' + '；'.join(cand['counter'])
                                                                          if cand['counter'] else '无决定性证据'),
        'reason': cand['reason'],
    } for c in components for cand in c['candidates']]
    view = {
        'title': '环路修复裁定',
        'rows': rows_view,
        'components': component_view,
        'candidates': candidate_view,
        'metrics': {
            '主单': len(masters),
            '可封存主单': len([m for m in masters if m['sealable'] == '是']),
            '已修复组件': len([c for c in components if c['status'] == '已修复']),
            '待补齐组件': len([c for c in components if c['status'] != '已修复']),
            '整套可封存': '是' if not blockers else '否',
        },
    }
    return {
        'policy': policy,
        'policy_label': POLICIES[policy],
        'verdict': verdict,
        'components': components,
        'masters': masters,
        'comparison': comparison,
        'batch_blockers': blockers,
        'batch_sealable': not blockers,
        'selection_errors': selection_errors,
        'applied': applied,
        'selected': selected,
        'remaining_graph_errors': len(g['errors']),
        'remaining_weight_errors': len([c for c in checks if c['code'] != 'BALANCED']),
        'repaired_totals': _fmt(_totals([r for r in repaired_rows if not _parent(r)])),
        'receipt_basis': receipt_basis,
        'view': view,
    }


def receipt_id(run_id, basis):
    payload = json.dumps({'run_id': run_id, **basis}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _policy_summary(state, policy):
    result = build(state, policy=policy, edges=None, compare=False)
    return {
        'policy': policy,
        'label': POLICIES[policy],
        'components_repaired': len([c for c in result['components'] if c['status'] == '已修复']),
        'components_held': len([c for c in result['components'] if c['status'] != '已修复']),
        'masters_sealable': len([m for m in result['masters'] if m['sealable'] == '是']),
        'masters_blocked': len([m for m in result['masters'] if m['sealable'] != '是']),
        'batch_sealable': result['batch_sealable'],
        'selected': len(result['selected']),
        'note': result['verdict'],
    }


def apply(state):
    result = build(state)
    state['repair'] = result
    state['views']['repair'] = result['view']
