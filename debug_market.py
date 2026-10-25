"""Read-only production audit: run checks against a temporary SQLite backup."""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from market import Store

ROOT = Path(__file__).resolve().parent
checks = []
def check(name, ok, detail=''):
    checks.append({'name':name,'passed':bool(ok),'detail':detail})

def main():
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp)
        (target/'data').mkdir()
        shutil.copy2(ROOT/'config.json',target/'config.json')
        source = sqlite3.connect((ROOT/'data/market.sqlite3').as_uri()+'?mode=ro',uri=True)
        backup = sqlite3.connect(target/'data/market.sqlite3')
        source.backup(backup); source.close(); backup.close()
        store = Store(target)
        state = store.state()
        check('SQLite 무결성',store.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
        for item,cities in state['books'].items():
            for city,pair in cities.items():
                for side,book in pair.items():
                    prices=[r['price'] for r in book['levels']]
                    check(f'{item}/{city}/{side} 가격 정렬',prices==sorted(prices,reverse=side=='buy'))
                    check(f'{item}/{city}/{side} 단일 주문 분석 제외',not book.get('single_only') or not book['fresh_levels'])
            for side,extreme in state['city_extremes'][item].items():
                fresh=[];past=[]
                for city,pair in cities.items():
                    b=pair[side]
                    if b.get('single_only'):continue
                    levels=b['fresh_levels'] if b['fresh'] else b['levels']
                    if levels:(fresh if b['fresh'] else past).append((city,levels[0]['price']))
                pool=fresh or past
                best=(max if side=='buy' else min)((p for _,p in pool),default=None)
                check(f'{item}/{side} 도시 최고·최저와 동률 도시',extreme['price']==best and {r['city'] for r in extreme['observations']}=={c for c,p in pool if p==best})
        patterns={}
        for item in state['items']:
            for city in state['cities']:
                p=store.patterns(item,city);patterns[item+'/'+city]=p
                check(f'{item}/{city} 월~일 7행', [r['key'] for r in p['weekdays']]==list(range(7)))
                check(f'{item}/{city} 누락과 0 구분',all((r['total'] is None)==(r['observed_hours']==0) for r in p['weekdays']))
                check(f'{item}/{city} 관측 범위',p['weekday_observed_hours']<=28*24)
        ranges=[dict(r) for r in store.db.execute('SELECT hours,COUNT(*) AS buckets,MIN(stamp) AS first,MAX(stamp) AS last FROM history GROUP BY hours')]
        snapshot={'generated_at':datetime.now(ZoneInfo('Europe/Paris')).isoformat(),'build':state['build'],'history_ranges':ranges,'city_extremes':state['city_extremes'],'patterns':patterns}
        store.db.close()
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-q'],cwd=ROOT,text=True,capture_output=True)
    check('합성 자료 회귀 검사',result.returncode==0,result.stderr.strip())
    node=shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
    js=subprocess.run([str(node),'--check','web/app.js'],cwd=ROOT,text=True,capture_output=True)
    check('브라우저 코드 구문',js.returncode==0,js.stderr.strip())
    snapshot['checks']=checks
    (ROOT/'docs/debug_latest.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 시장 분석 디버깅 점검표','',snapshot['generated_at'],'','운영 DB는 읽기 전용으로 열고 임시 복제본에서 검사. 게임 주문·설정·메일 변경 없음.','','| 번호 | 점검 | 결과 |','|---|---|---|']
    lines.extend(f"| {i} | {r['name']} | {'통과' if r['passed'] else '실패'} |" for i,r in enumerate(checks,1))
    lines+=['','## 원본 이력 범위','',json.dumps(ranges,ensure_ascii=False),'','## 회귀 검사 출력','', '```',result.stderr.strip(),'```','','## 화면 확인','', '화면 검증 기록은 별도 검증 메모 참조. 이 스크립트는 시각적 가독성을 자동 통과 처리하지 않음.']
    ui = ROOT/'docs/화면검증_20261025.md'
    if ui.exists(): lines += ['', ui.read_text(encoding='utf-8')]
    (ROOT/'docs/디버깅_점검표.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'checks':len(checks),'failed':[r['name'] for r in checks if not r['passed']],'report':str(ROOT/'docs/디버깅_점검표.md')},ensure_ascii=False))
    return 0 if all(r['passed'] for r in checks) else 1

if __name__=='__main__':
    raise SystemExit(main())
