import {node,get,action,badge,empty,errorBox,objectCard,metadata,short} from './components.js';
export function section(target,title) {const header=node('div','section-label'); header.append(node('h3','',title)); target.append(header);}
export function prose(html) {
  // html=False server renderer; a second DOM allowlist copies no attributes.
  const parsed=new DOMParser().parseFromString(html||'','text/html');
  const allowed=new Set(['P','H1','H2','H3','H4','H5','H6','PRE','CODE','STRONG','EM','UL','OL','LI','BLOCKQUOTE','HR','BR']);
  function copy(source,parent) {for(const child of source.childNodes) {
    if(child.nodeType===Node.TEXT_NODE) parent.append(document.createTextNode(child.textContent));
    else if(child.nodeType===Node.ELEMENT_NODE&&allowed.has(child.tagName)) {const element=node(child.tagName.toLowerCase()); copy(child,element); parent.append(element);}
  }}
  const result=node('div','prose'); copy(parsed.body,result); return result;
}
export async function cardsPage(target,path,kind,open,cursor=null) {
  const data=await get(path+(cursor===null?'':`&cursor=${encodeURIComponent(cursor)}`));
  if(!data.items.length) {target.append(empty(`No ${kind} yet`)); return;}
  const grid=node('div','nested-grid'); data.items.forEach(item=>grid.append(objectCard(kind,item,open))); target.append(grid);
  if(data.has_more) {const more=action('Load next page ↓',async()=>{more.disabled=true; try {await cardsPage(target,path,kind,open,data.next_cursor); more.remove();} catch(error) {more.disabled=false; target.append(errorBox(error.message));}},'more-button'); target.append(more);}
}
const json=(target,value)=>target.append(node('pre','record',JSON.stringify(value,null,2)));
export async function objectDetail(kind,id,target,open) {
  const data=await get(`/api/${kind==='jobs'?'learning/jobs':kind}/${encodeURIComponent(id)}`);
  target.append(badge(data.status||data.state),metadata(data,['id','conversation_id','stream_id','status','state','kind','revision','source_revision','dirty','thread_dirty','started_at','ended_at','last_active_at','updated_at','outcome','rounds','error']));
  if(kind==='threads') {
    section(target,'Current state'); target.append(node('p','prose',data.current_state));
    if(data.memory_ref) target.append(action(`Linked memory ↗ ${data.memory_ref}`,()=>open('file',data.memory_ref),'action-button'));
    if(data.dirty) target.append(node('p','notice','Thread state is marked dirty; it may lag behind its Episode sources.'));
    section(target,'Episodes'); await cardsPage(target,`/api/episodes?limit=6&thread_id=${encodeURIComponent(id)}`,'episodes',open);
  } else if(kind==='episodes') {
    section(target,'Summary'); target.append(node('p','prose',data.summary));
    if(data.thread_id) target.append(action(`Parent Thread ↗ ${short(data.thread_id)}`,()=>open('threads',data.thread_id)));
    section(target,'Highlights'); if(!data.highlights.length) target.append(empty('No highlights recorded'));
    for(const highlight of data.highlights) {const box=node('div','nested'); box.append(badge(highlight.attribution),node('p','prose',highlight.content)); for(const source of highlight.sources) box.append(action(`Evidence ↗ ${short(source.run_id)} / #${source.sequence}`,()=>open('runs',source.run_id))); target.append(box);}
    section(target,'Source runs'); const grid=node('div','nested-grid'); for(const runId of data.run_ids) grid.append(objectCard('runs',{id:runId,status:'source'},open)); target.append(grid);
  } else if(kind==='runs') {
    const reveal=action('Reveal private messages & tool records ↓',async()=>{reveal.disabled=true; try {await messages(target,id,0); reveal.remove();} catch(error) {reveal.disabled=false; target.append(errorBox(error.message));}},'action-button');
    target.append(node('p','notice','原始对话默认折叠，仅在本机点击后读取。工具记录是历史审计数据，不会执行。'),reveal);
  } else if(kind==='jobs') {
    section(target,'Source Episodes / Runs');
    for(const source of data.sources.episodes||[]) target.append(action(`Episode ↗ ${short(source.id)} · REV ${source.revision}`,()=>open('episodes',source.id),'file-row'));
    for(const source of data.sources.runs||[]) target.append(action(`Run ↗ ${short(source.id)}`,()=>open('runs',source.id),'file-row'));
    if(!(data.sources.runs?.length||data.sources.episodes?.length)) target.append(empty('No Run / Episode sources recorded','Curator can use MemoryFS targets.'));
    section(target,'Operations & publication receipts');
    for(const operation of data.operations) {const box=node('div','nested'); if(operation.path) box.append(action(operation.path,()=>open('file',operation.path))); json(box,operation); target.append(box);}
    if(!data.operations.length) target.append(empty('No operations recorded'));
    section(target,'Available metrics'); json(target,data.metrics);
    target.append(node('p','notice','回执、Hash 与来源来自实际记录。未保存旧正文时，不提供虚构 Diff 或完整版本历史。'));
  }
  return data.title||`${kind==='jobs'?data.kind:kind} / ${short(id)}`;
}
async function messages(target,id,after) {
  const data=await get(`/api/runs/${encodeURIComponent(id)}/messages?limit=6&after=${after}`);
  for(const message of data.items) {const detail=node('details','message'); detail.append(node('summary','',`#${message.sequence} · ${message.role.toUpperCase()}${message.payload_large?' · bounded preview':''}`)); if(message.text) detail.append(node('pre','record',message.text)); if(message.tool_calls) {section(detail,'Tool calls'); json(detail,message.tool_calls);} if(message.result) {section(detail,'Tool result / call_id'); json(detail,message.result);} target.append(detail);}
  if(!data.items.length) target.append(empty('No persisted messages'));
  if(data.has_more) {const more=action('Next messages ↓',async()=>{more.disabled=true; try {await messages(target,id,data.next_cursor); more.remove();} catch(error) {more.disabled=false; target.append(errorBox(error.message));}},'more-button'); target.append(more);}
}
