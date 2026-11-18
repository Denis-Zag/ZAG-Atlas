// Проверяем настоящие функции api/askWithRecovery с управляемыми часами и сетью.
const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const helpers=source.slice(0,source.indexOf('const post='));
assert(helpers.includes('async function askWithRecovery'));

function harness(respond){
 let elapsed=0,next=0,locked=false;
 const timers=new Map(),calls=[],notice={hidden:true};
 const context={Headers,AbortController,performance:{now:()=>elapsed},
  crypto:{randomUUID:()=> '11111111-1111-4111-8111-111111111111'},
  document:{getElementById:()=>notice},
  setTimeout:(fn,ms)=>{timers.set(++next,{at:elapsed+ms,fn});return next},
  clearTimeout:id=>timers.delete(id),
  fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',options});
   return respond({url,options,elapsed,context})},
 };
 vm.createContext(context);vm.runInContext(helpers,context);
 context.lockScreen=()=>{locked=true};
 return {calls,notice,get elapsed(){return elapsed},get locked(){return locked},
  async run(){
   let done=false,result,error;
   context.askWithRecovery('Сколько стоит монтаж и есть ли гарантия?')
    .then(value=>{result=value;done=true},value=>{error=value;done=true});
   for(let step=0;step<300&&!done;step++){
    await new Promise(setImmediate);
    if(done)break;
    assert(timers.size,'Запрос завис без таймера');
    const [id,timer]=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
    elapsed=timer.at;timers.delete(id);timer.fn();
   }
   assert(done,'Превышен предел действий теста');
   assert.equal(timers.size,0,'Остался незавершённый таймер');
   if(error)throw error;
   return result;
  }};
}
const response=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>data});
const saved={answer:'Цена подтверждена; гарантия требует проверки.',needs_review:1,
 sources_json:'[{"document_id":"prices","quote":"12000 рублей"}]'};
function broken(){throw new TypeError('Соединение закрыто')}

test('обычный ответ возвращается одним POST без опроса истории',async()=>{
 const h=harness(()=>response(200,saved));
 assert.deepEqual(await h.run(),saved);assert.equal(h.calls.length,1);
 assert.equal(h.calls[0].method,'POST');
});
test('сценарий из логов: обрыв на 15 с, ответ сохранён на 26,729 с',async()=>{
 const h=harness(({options,elapsed,context})=>{
  if(options.method==='POST')return new Promise((_,reject)=>context.setTimeout(
   ()=>reject(new TypeError('Обрыв браузера')),15000));
  return elapsed<26729?response(404,{detail:'Вопрос не найден'}):response(200,saved);
 });
 assert.deepEqual(await h.run(),saved);
 assert(h.elapsed>=26729);assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
 for(const c of h.calls.slice(1))assert.equal(c.url,'/kb/history/11111111-1111-4111-8111-111111111111');
});
test('неполный JSON исходного ответа также восстанавливается из истории',async()=>{
 const h=harness(({options})=>options.method==='POST'
  ?{ok:true,status:200,json:async()=>{throw new SyntaxError('Неполный JSON')}}:response(200,saved));
 assert.deepEqual(await h.run(),saved);assert.equal(h.calls.length,2);
});
test('временные 503 и сетевой отказ при чтении не запускают модель заново',async()=>{
 let reads=0;
 const h=harness(({options})=>{
  if(options.method==='POST')return broken();
  if(++reads===1)return response(503,{detail:'Недоступен'});
  if(reads===2)return broken();
  return response(200,saved);
 });
 assert.deepEqual(await h.run(),saved);assert.equal(reads,3);
 assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});
test('завершение сессии останавливает восстановление и закрывает экран',async()=>{
 const h=harness(({options})=>options.method==='POST'?broken():response(401,{detail:'Сессия завершена'}));
 await assert.rejects(()=>h.run(),e=>e.status===401);assert(h.locked);assert.equal(h.calls.length,2);
});
test('ошибка доступа исходного POST не превращается в повторный запрос',async()=>{
 const h=harness(()=>response(403,{detail:'Доступ запрещён'}));
 await assert.rejects(()=>h.run(),e=>e.status===403);assert.equal(h.calls.length,1);
});
test('отсутствующий результат ожидается до трёх минут, без повторного POST',async()=>{
 const h=harness(({options})=>options.method==='POST'?broken():response(404,{detail:'Вопрос не найден'}));
 await assert.rejects(()=>h.run(),/Проверьте «Историю»/);
 assert.equal(h.elapsed,180000);assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});
test('зависшее чтение истории прерывается таймаутом и ограничено общим сроком',async()=>{
 const h=harness(({options})=>{
  if(options.method==='POST')return broken();
  return new Promise((_,reject)=>options.signal.addEventListener('abort',
   ()=>reject(new Error('Таймаут')),{once:true}));
 });
 await assert.rejects(()=>h.run(),/Проверьте «Историю»/);
 assert.equal(h.elapsed,180000);assert(h.calls.length<20);
 assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});

function formHarness(){
 const elements={askForm:{},answer:{replaceChildren(){}},notice:{hidden:false},question:{value:'Вопрос'}};
 let calls=0,rendered=false;
 const context={$:id=>elements[id],
  askWithRecovery:async()=>{calls++;elements.notice.hidden=false;return {needs_review:false}},
  renderAnswer:()=>{rendered=true},notice:()=>{throw Error('Неожиданное предупреждение')}};
 vm.createContext(context);
 const line=source.split('\n').find(s=>s.startsWith("$('askForm').onsubmit="));
 assert(line);vm.runInContext(line,context);
 return {elements,get calls(){return calls},get rendered(){return rendered},
  submit:button=>elements.askForm.onsubmit({preventDefault(){},submitter:button})};
}
test('после восстановления успешного ответа прежнее предупреждение скрыто',async()=>{
 const h=formHarness(),button={disabled:false};
 await h.submit(button);assert(h.rendered);assert.equal(h.calls,1);
 assert.equal(h.elements.notice.hidden,true);assert.equal(button.disabled,false);
});
test('повторная отправка через занятую форму не создаёт второй запрос',async()=>{
 const h=formHarness();await h.submit({disabled:true});
 assert.equal(h.calls,0);assert.equal(h.rendered,false);
});
