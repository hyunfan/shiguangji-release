"""Read-only collection diagnostics."""
import collections
import json
import sqlite3
import sys
from pathlib import Path

def audit(folder):
    folder=Path(folder)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    media=[{**m,'post_id':p['id']} for p in manifest['posts'] for m in p['media']]
    missing=[m for m in media if m['status']!='saved']
    db=sqlite3.connect(f'file:{(folder/"archive.sqlite").as_posix()}?mode=ro',uri=True)
    result={k:manifest[k] for k in ('username','status','updated_at','counts','errors')}
    result.update(missing_reasons=dict(collections.Counter(m['error'] for m in missing)),
                  media_states={str(k):v for k,v in collections.Counter((m['kind'],m['status']) for m in media).items()},
                  capture_states=db.execute('select status,count(*) from captures group by status').fetchall(),
                  queries=db.execute('select * from queries').fetchall(),
                  unfinished_captures=db.execute("select timestamp,original,error from captures where status!='done' limit 15").fetchall(),
                  missing=missing,
                  empty_posts=[{'id':p['id'],'captures':p['captures'][:2]} for p in manifest['posts'] if not p['text']],
                  raw_files=len(list((folder/'raw').glob('*.txt'))),
                  media_files=len([p for p in (folder/'media').rglob('*') if p.is_file()]))
    db.close()
    return result

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python audit_collection.py collections/username')
    result=audit(sys.argv[1])
    print(json.dumps(result,ensure_ascii=False,indent=2))
