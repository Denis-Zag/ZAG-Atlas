"""Живая проверка гибридного RAG на временной БД и отдельной коллекции."""
import asyncio,json,os,sys,tempfile,uuid
from pathlib import Path
sys.path.insert(0,'/app')
from app import ai, search, storage as st
from app.main import app
from app.verification import VerificationClient as TestClient
report={'checks':[], 'comparison':[]}
os.environ['ADMIN_PASSWORD']='verify-admin'
os.environ['STAFF_PASSWORD']='verify-staff'
with tempfile.TemporaryDirectory() as temp:
 st.DATA=Path(temp)
 with TestClient(app) as client:
  index=app.state.vector_index
  index.collection='atlas_verify_'+uuid.uuid4().hex
  try:
   docs=[json.loads(s) for s in Path('/fixtures/kb_documents.jsonl').read_text().splitlines()]
   for d in docs:
    r=client.post('/kb/documents',auth=('admin','verify-admin'),json=d)
    assert r.status_code==200,r.text
    assert 'index_error' not in r.json(),r.text
   cached=client.post('/kb/reindex',auth=('admin','verify-admin')).json()
   assert cached['embedding_tokens']==0,cached
   report['cached_reindex']=cached
   questions=[json.loads(s) for s in Path('/fixtures/kb_questions.jsonl').read_text().splitlines()]
   technical=0
   for i,q in enumerate(questions,1):
    r=client.post('/kb/ask',auth=('staff','verify-staff'),json={'question':q['question']})
    a=r.json()
    hist=client.get('/kb/history',auth=('staff','verify-staff')).json()[0]
    ok=(r.status_code==200 and a['needs_review']==q['expected_needs_review'] and (a['needs_review'] or bool(a['sources'])))
    report['checks'].append({'number':i,'question':q['question'],'expected_needs_review':q['expected_needs_review'],'result':a,'error':hist['error'],'passed':ok})
    print(json.dumps({'progress':i,'passed':ok},ensure_ascii=False),flush=True)
    technical+=bool(hist['error'] and ('недоступен' in hist['error'] or 'внешнего сервиса' in hist['error']))
    if technical>=2:
     raise RuntimeError('Два технических сбоя: прогон остановлен')
   snippets=st.rows('SELECT snippets.*,documents.title FROM snippets JOIN documents ON documents.id=snippets.document_id')
   for question,target in [('Можно передвинуть приезд мастера?','Перенос, отмена и гарантийные обращения'),('Сколько стоит поставить сплит-систему?','Стоимость и условия расчёта')]:
    lexical=search.retrieve(question,snippets)
    async def retrieve():
     async with ai.make_client() as c:
      from app.vectors import VectorIndex
      v=VectorIndex(c)
      v.collection=index.collection
      return await v.retrieve(question,20000)
    hybrid,meta=asyncio.run(retrieve())
    report['comparison'].append({'question':question,'target':target,'lexical_titles':list(dict.fromkeys(s['title'] for s in lexical)), 'hybrid_titles':list(dict.fromkeys(s['title'] for s in hybrid)),'semantic_candidates':meta['semantic_candidates'],'hybrid_found_target':any(s['title']==target for s in hybrid)})
   doc=client.get('/kb/documents',auth=('staff','verify-staff')).json()[0]
   assert client.delete('/kb/documents/'+doc['id'],auth=('admin','verify-admin')).status_code==200
   report['deletion_remaining_snippets']=len(st.rows('SELECT * FROM snippets'))
   report['passed']=all(x['passed'] for x in report['checks']) and all(x['hybrid_found_target'] for x in report['comparison'])
  finally:
   async def clean():
    async with ai.make_client() as c:
     await c.delete(index.url+'/collections/'+index.collection)
   asyncio.run(clean())
   print('REPORT_JSON='+json.dumps(report,ensure_ascii=False),flush=True)
