import hashlib,json

def decide(state):
    b=state['balance'];repair=state.get('repair') or {};edges=repair.get('selected') or []
    if edges:
        # 有证据唯一定位的断边时，封存以修复后重算的图与逐层重量为准。
        blocked=len(repair.get('batch_blockers') or [])
        remaining_graph=repair.get('remaining_graph_errors',0)
        remaining_weight=repair.get('remaining_weight_errors',0)
        sealable=b['row_balanced'] and bool(repair.get('batch_sealable'))
        evidence={'repair_edges':edges,'masters':repair.get('masters'),'hazards':state['hazard_issues']}
    else:
        blocked=b['graph_errors']+b['weight_errors']+len(b['declaration_differences'])+len(state['hazard_issues'])+len(state['quarantine'])
        remaining_graph=b['graph_errors'];remaining_weight=b['weight_errors']
        sealable=b['row_balanced'] and not blocked
        evidence={'roots':state['graph']['roots'],'weights':state['weight_checks'],'hazards':state['hazard_issues']}
    return {'status':'可封存' if sealable else '待处理','blocked':blocked,
            'graph_errors_remaining':remaining_graph,'weight_errors_remaining':remaining_weight,
            'repair_edges':edges,
            'seal':hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()}
def apply(state):
    state['archive']=decide(state);state['views']['archive']={'title':'版本封存','rows':[{'id':r['id'],'revision':r['revision'],'parent':r['parent']} for r in state['records']],'metrics':{'主单':len(state['graph']['roots']),'状态':state['archive']['status'],'阻断':state['archive']['blocked']}}
