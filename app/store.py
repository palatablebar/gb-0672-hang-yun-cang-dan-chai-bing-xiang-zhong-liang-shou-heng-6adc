import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS repair_decisions (
        run_id TEXT NOT NULL, version INTEGER NOT NULL, receipt TEXT NOT NULL,
        policy TEXT NOT NULL, edges TEXT NOT NULL, result TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(run_id, version), UNIQUE(run_id, receipt))""")
    con.execute("""CREATE TABLE IF NOT EXISTS repair_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, receipt TEXT NOT NULL,
        policy TEXT NOT NULL, edges TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL,
        restored_version INTEGER, created_at TEXT NOT NULL)""")
    return con

def _now():
    return datetime.now(timezone.utc).isoformat()

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _decision(row):
    data=json.loads(row['result'])
    data.update(version=row['version'],receipt=row['receipt'],policy=row['policy'],
                edges=json.loads(row['edges']),created_at=row['created_at'])
    return data

def repair_decisions(path,run_id):
    with connect(path) as con:
        rows=con.execute('SELECT * FROM repair_decisions WHERE run_id=? ORDER BY version',(run_id,)).fetchall()
        attempts=con.execute('SELECT * FROM repair_attempts WHERE run_id=? ORDER BY id',(run_id,)).fetchall()
    history=[_decision(r) for r in rows]
    return {'run_id':run_id,'version':history[-1]['version'] if history else 0,
            'receipt':history[-1]['receipt'] if history else None,
            'active':history[-1] if history else None,'history':history,
            'attempts':[dict(r) for r in attempts]}

def save_repair(path,run_id,policy,edges,receipt,result):
    """同一 receipt 重试只返回既有结果，不新增裁定版本。"""
    with connect(path) as con:
        existing=con.execute('SELECT * FROM repair_decisions WHERE run_id=? AND receipt=?',(run_id,receipt)).fetchone()
        if existing:
            return _decision(existing), False
        version=(con.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM repair_decisions WHERE run_id=?',(run_id,)).fetchone()['v'])
        con.execute('INSERT INTO repair_decisions(run_id,version,receipt,policy,edges,result,created_at) VALUES(?,?,?,?,?,?,?)',
                    (run_id,version,receipt,policy,json.dumps(edges,ensure_ascii=False),
                     json.dumps(result,ensure_ascii=False), _now()))
        row=con.execute('SELECT * FROM repair_decisions WHERE run_id=? AND version=?',(run_id,version)).fetchone()
    return _decision(row), True

def record_repair_attempt(path,run_id,receipt,policy,edges,code,detail,restored_version=None):
    """同一回执的同一失败原因只记一次，重试不新增结果。"""
    with connect(path) as con:
        existing=con.execute('SELECT * FROM repair_attempts WHERE run_id=? AND receipt=? AND code=?',
                             (run_id,receipt,code)).fetchone()
        if existing:
            return dict(existing), False
        cur=con.execute('INSERT INTO repair_attempts(run_id,receipt,policy,edges,code,detail,restored_version,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (run_id,receipt,policy,json.dumps(edges,ensure_ascii=False),code,detail,restored_version,_now()))
        row=con.execute('SELECT * FROM repair_attempts WHERE id=?',(cur.lastrowid,)).fetchone()
        return dict(row), True

def restore_repair(path,run_id,version):
    """切回指定历史版本：以该版断边与策略重新落定，失败时上一版 stays active。"""
    decisions=repair_decisions(path,run_id)
    target=next((d for d in decisions['history'] if d['version']==version),None)
    if target is None:
        record_repair_attempt(path,run_id,'restore-v'+str(version),'restore','[]','VERSION_NOT_FOUND',
                              f'裁定版本 v{version} 不存在，已保留上一版 v{decisions["version"]}',decisions['version'])
        raise BusinessError('VERSION_NOT_FOUND','裁定版本不存在，已恢复上一版',404)
    receipt=target['receipt']+'-restore-v'+str(version)
    policy=target['policy'] if target['policy'] in {'unique_only','wait_group'} else 'unique_only'
    result={**{k:v for k,v in target.items() if k not in ('version','receipt','created_at')},
            'restored_from':version,
            'verdict':f"裁定切回 v{version}：{target.get('verdict','')}"}
    saved,created=save_repair(path,run_id,policy,target['edges'],receipt,result)
    return saved,created
