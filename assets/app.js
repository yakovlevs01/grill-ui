'use strict';
const $ = id => document.getElementById(id);
const fragment = location.hash.slice(1);
if (fragment) { sessionStorage.setItem('grill-token', fragment); history.replaceState(null, '', '/'); }
const token = sessionStorage.getItem('grill-token');
let state, active, chatOpen = false, pending = Promise.resolve(), saveTimer, dirty = false, chatFingerprint = '', busy = false;
const chatDrafts = {}, summaryDrafts = {};
function notice(text, error=false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch('/api/' + path, {method:data ? 'POST':'GET',headers:{'X-Grill-Token':token || '', 'Content-Type':'application/json'}, ...(data ? {body:JSON.stringify(data)}:{})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function node(tag, text, cls) { const e = document.createElement(tag); if(text !== undefined)e.textContent=text;if(cls)e.className=cls;return e; }
// Render a small Markdown subset with DOM nodes; model text never becomes HTML.
function inline(parent,text){for(const part of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)){if(part.startsWith('**')&&part.endsWith('**'))parent.append(node('b',part.slice(2,-2)));else if(part.startsWith('`')&&part.endsWith('`'))parent.append(node('code',part.slice(1,-1)));else parent.append(document.createTextNode(part));}return parent;}
function markdown(text){
  const result=node('div',undefined,'markdown'),lines=text.split('\n');
  const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());
  for(let i=0;i<lines.length;i++){
    const line=lines[i];if(!line.trim())continue;
    if(line.trim().startsWith('```')){const code=[];while(++i<lines.length&&!lines[i].trim().startsWith('```'))code.push(lines[i]);result.append(node('pre',code.join('\n')));continue;}
    if(i+1<lines.length && line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])){
      const wrap=node('div',undefined,'table-wrap'),table=node('table'),head=node('tr');for(const cell of cells(line))head.append(inline(node('th'),cell));table.append(head);i++;
      while(i+1<lines.length&&lines[i+1].includes('|')){const row=node('tr');for(const cell of cells(lines[++i]))row.append(inline(node('td'),cell));table.append(row);}wrap.append(table);result.append(wrap);continue;
    }
    const heading=line.match(/^#{1,6}\s+(.+)$/);if(heading){result.append(inline(node('h3'),heading[1]));continue;}
    const bullet=line.match(/^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$/);if(bullet){const list=node(/^\s*\d/.test(line)?'ol':'ul');list.append(inline(node('li'),bullet[1]));while(i+1<lines.length){const next=lines[i+1].match(/^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$/);if(!next)break;i++;list.append(inline(node('li'),next[1]));}result.append(list);continue;}
    result.append(inline(node('p'),line));
  }return result;
}
function question(){return state.round.questions.find(q=>q.id===active);}
function renderNav() {
  $('questions').replaceChildren();
  for (const q of state.round.questions) {
    const b=node('button');b.type='button';b.setAttribute('aria-current',String(q.id===active));
    const done=state.answers[q.id].confirmed;
    b.append(node('span','', 'dot'+(done?' done':'')),node('span',q.title+(done?' · готово':'')));
    b.onclick=()=>switchQuestion(q.id);$('questions').append(b);
  }
  const count=Object.values(state.answers).filter(a=>a.confirmed).length;
  $('progress').textContent=`${count} из ${state.round.questions.length} ответов`;
  $('submit').disabled=state.submitted || count!==state.round.questions.length || busy || dirty || Object.values(state.branches).some(b=>b.status==='running');
  $('submit').textContent=state.submitted?'Раунд отправлен':'Отправить раунд';
  $('submitHint').textContent=state.submitted?'Вернись в основной чат и напиши «Готово».': 'Сначала подтверди ответы на все вопросы.';
}
function renderQuestion() {
  const q=question(), a=state.answers[active];
  $('discuss').disabled=state.submitted&&!state.branches[active].messages.length;
  $('discuss').textContent=state.submitted?'Посмотреть обсуждение':'Обсудить отдельно';
  $('questionId').textContent=q.id;$('title').textContent=q.title;$('body').textContent=q.body;
  $('recommendation').textContent=q.recommendation;$('optionList').replaceChildren();
  $('options').hidden=q.mode==='text' || !q.options?.length;
  for(const o of q.options || []) {
    const label=node('label',undefined,'option'), input=node('input');
    input.type=q.mode==='multiple'?'checkbox':'radio';input.name='choice';input.value=o.id;
    input.checked=a.selected.includes(o.id);input.disabled=state.submitted;
    input.onchange=()=>{state.answers[active].selected=[...$('optionList').querySelectorAll('input:checked')].map(e=>e.value); changed();};
    const text=node('span');text.append(node('strong',o.label));if(o.description)text.append(node('small',o.description));
    label.append(input,text);$('optionList').append(label);
  }
  // Free text stays available, including as an alternative to every option.
  if(q.options?.length && q.mode!=='text') {
    const clear=node('button','Снять выбор');clear.type='button';clear.disabled=state.submitted;
    clear.onclick=()=>{state.answers[active].selected=[];changed();renderQuestion();};$('optionList').append(clear);
  }
  $('answer').value=a.text;$('answer').disabled=state.submitted;
  renderConfirmation();renderNav();renderChat(true);
}
function renderConfirmation(){const a=state.answers[active];$('confirm').disabled=state.submitted || busy || (!a.selected.length&&!a.text.trim());$('confirm').textContent=a.confirmed?'Ответ подтверждён':'Подтвердить ответ';$('saved').textContent=dirty?'Сохраняю…':a.confirmed?'Подтверждено':'Черновик сохранён';}
function changed(){state.answers[active].confirmed=false;dirty=true;renderConfirmation();renderNav();clearTimeout(saveTimer);saveTimer=setTimeout(()=>save().catch(e=>notice(e.message,true)),350);}
function save(){clearTimeout(saveTimer);const qid=active,value=structuredClone(state.answers[qid]);const serialized=JSON.stringify(value);pending=pending.catch(()=>{}).then(()=>api('answer',{question_id:qid,...value}));return pending.then(()=>{if(active===qid && JSON.stringify(state.answers[qid])===serialized){dirty=false;renderConfirmation();renderNav();}});}
async function switchQuestion(qid){if(qid===active)return;try{if(dirty)await save();rememberChatDrafts();active=qid;chatFingerprint='';renderQuestion();}catch(e){notice('Ответ не сохранился: '+e.message,true);}}
function rememberChatDrafts(){if(!active)return;chatDrafts[active]=$('message').value;summaryDrafts[active]=$('summary').value;}
function renderChat(force=false){
  $('chatPanel').hidden=!chatOpen;document.querySelector('.workspace').classList.toggle('with-chat',chatOpen);
  const modal=chatOpen && matchMedia('(max-width:660px)').matches;
  for(const el of [document.querySelector('header'),document.querySelector('.sidebar'),$('main')])el.inert=modal;
  if(!chatOpen)return;
  const b=state.branches[active],q=question();$('chatQuestion').textContent=q.title;
  const rt=b.thread_id&&b.runtime||state.runtime;$('runtime').textContent=`${{codex:'Codex',claude:'Claude Code'}[rt.harness]||rt.harness} · ${rt.model} · effort ${rt.effort}`;
  const fingerprint=JSON.stringify([active,b.messages,b.summary]);
  if(force || fingerprint!==chatFingerprint){
    $('messages').replaceChildren();
    if(!b.messages.length)$('messages').append(node('p','Обсуди последствия, сравни варианты или попробуй сформулировать свой. Этот чат помнит только обсуждение текущего вопроса.','muted'));
    for(const m of b.messages){const el=node('div',undefined,'message '+m.role);el.append(node('strong',m.role==='user'?'Ты':'Агент'),m.role==='assistant'?markdown(m.text):node('div',m.text));$('messages').append(el);}
    $('messages').scrollTop=$('messages').scrollHeight;
    if(b.summary && !summaryDrafts[active])summaryDrafts[active]=b.summary;
    if(fingerprint!==chatFingerprint && b.summary && b.summary!==renderChat.lastSummary?.[active]){summaryDrafts[active]=b.summary;renderChat.lastSummary={...renderChat.lastSummary,[active]:b.summary};}
    $('summary').value=summaryDrafts[active]||'';$('message').value=chatDrafts[active]||'';chatFingerprint=fingerprint;
  }
  const running=b.status==='running';$('chatStatus').textContent=running?'Агент отвечает…':b.error||'';
  $('stop').hidden=!running;$('send').disabled=running||state.submitted||busy;
  $('summarize').disabled=running||state.submitted||!b.messages.some(m=>m.role==='assistant');
  $('summaryBox').hidden=!b.summary;$('useSummary').disabled=state.submitted;$('summary').disabled=state.submitted;
}
async function refresh(){if(!state || !dirty){const next=await api('state');if(!dirty){state=next;renderNav();renderChat();}}else{const next=await api('state');state.branches=next.branches;renderChat();}}
async function send(summarize=false){if(busy)return;const qid=active,text=summarize?'summary':$('message').value;rememberChatDrafts();busy=true;renderChat();try{if(dirty)await save();await api('chat',{question_id:qid,message:text,summarize});if(!summarize){chatDrafts[qid]='';if(active===qid)$('message').value='';}await refresh();}catch(e){notice(e.message,true);}finally{busy=false;renderChat();}}
$('answer').oninput=()=>{state.answers[active].text=$('answer').value;changed();};
$('message').oninput=()=>{chatDrafts[active]=$('message').value;};
$('summary').oninput=()=>{summaryDrafts[active]=$('summary').value;};
$('confirm').onclick=async()=>{busy=true;try{state.answers[active].confirmed=true;dirty=true;await save();notice('Ответ подтверждён. Его можно изменить до отправки раунда.');}catch(e){state.answers[active].confirmed=false;dirty=true;notice(e.message,true);}finally{busy=false;renderConfirmation();renderNav();}};
$('discuss').onclick=()=>{chatOpen=true;renderChat(true);$('message').focus();if(!state.submitted&&!state.branches[active].messages.length){$('message').value='Помоги разобраться в этом вопросе: объясни варианты, их плюсы и минусы с учётом контекста.';send();}};
$('closeChat').onclick=()=>{rememberChatDrafts();chatOpen=false;renderChat();$('discuss').focus();};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&chatOpen)$('closeChat').click();});
matchMedia('(max-width:660px)').addEventListener('change',()=>{if(state)renderChat();});
$('chatForm').onsubmit=e=>{e.preventDefault();send();};
$('summarize').onclick=()=>send(true);
$('stop').onclick=async()=>{try{await api('stop',{question_id:active});await refresh();}catch(e){notice(e.message,true);}};
$('useSummary').onclick=()=>{const a=state.answers[active];a.text=$('summary').value;a.selected=[];changed();renderQuestion();$('answer').focus();};
$('submit').onclick=async()=>{busy=true;renderNav();try{if(dirty)await save();await api('submit',{});await refresh();renderQuestion();notice('Раунд отправлен. Вернись в основной чат и напиши «Готово». Агент прочитает подтверждённые ответы.');}catch(e){notice(e.message,true);}finally{busy=false;renderNav();}};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
(async()=>{try{state=await api('state');active=state.round.questions[0].id;$('roundTitle').textContent=state.round.title;document.title='Grill · '+state.round.title;renderQuestion();setInterval(()=>refresh().catch(e=>notice('Нет связи с локальным сервером: '+e.message,true)),1600);}catch(e){notice('Не удалось открыть раунд: '+e.message+'. Открой полную ссылку, которую дал агент.',true);}})();
