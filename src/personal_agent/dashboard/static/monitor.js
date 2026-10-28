import {node,get,action,badge,empty,errorBox,time,short} from './components.js';
const content=id=>document.getElementById(`content-${id}`);
const signatures=new Map();
export function replace(target,data,...children) {
  const signature=JSON.stringify(data);
  if(signatures.get(target)===signature||target.contains(document.activeElement)) return;
  signatures.set(target,signature);target.replaceChildren(...children);
}
export async function status(open) {
  try {
    const data=await get('/api/status'), diagnostic=data.diagnostics;
    const word=node('div','status-word');word.append(node('span',`status-dot ${data.state==='Idle'?'online':''}`),node('span','',data.state));
    const meta=node('dl','meta-grid');
    const fields=[['STAGE',data.current_stage||'No active stage'],['LATEST RUN',data.latest_run?.status||'No runs yet'],['QQ CHANNEL',data.qq.gateway],['PENDING JOBS',diagnostic?.pending_jobs_capped??'Unavailable']];
    for(const [key,value] of fields) {const row=node('div');row.append(node('dt','',key),node('dd','',value));meta.append(row);}
    replace(content('status'),data,badge('HOST OBSERVED'),word,meta,action('Runtime diagnostics ↗',()=>open('runtime','current')));
    document.getElementById('connection').textContent='Connected';document.getElementById('connection-dot').classList.add('online');
    const diagnostics=document.getElementById('job-diagnostics');
    if(diagnostics) {
      const box=node('div','muted');
      box.append(node('p','',diagnostic?.maintenance_running?`Executing: ${data.current_stage}`:diagnostic?.background_task_active?'Waiting for idle / maintenance eligibility':'No background task scheduled'));
      if(diagnostic) box.append(node('p','',`Pending jobs ${diagnostic.pending_jobs_capped} · runs ${diagnostic.pending_runs_capped} · dirty threads ${diagnostic.dirty_threads_capped}`));
      if(diagnostic?.schedule?.last_error) box.append(node('p','notice',`Last cycle outcome: ${diagnostic.schedule.last_error}`));
      if(diagnostic?.episode_progress?.error_code) box.append(node('p','notice',`Organizer: ${diagnostic.episode_progress.error_code}`));
      if(diagnostic?.curator) box.append(node('p','mono',`Curator due: ${time(diagnostic.curator.curator_due_at)}`));
      box.append(action('Inspect runtime & schedule ↗',()=>open('runtime','current')));
      replace(diagnostics,diagnostic,box);
    }
  } catch {
    replace(content('status'),{error:true},errorBox());document.getElementById('connection').textContent='Disconnected';document.getElementById('connection-dot').classList.remove('online');
  }
}
function eventItem(event,open) {
  const row=action('',()=>open(event.related_kind,event.related_object_id),'activity-item');
  const label=node('span');label.append(node('strong','',event.event_type.replaceAll('_',' ')),node('span','mono',`${short(event.related_object_id)} · ${time(event.timestamp)}`),node('span','mono',`${event.origin==='live_runtime'?'LIVE':'STORED'} / ${event.timestamp_basis.replaceAll('_',' ')}`));
  row.append(node('span','activity-icon',event.origin==='live_runtime'?'↯':'↗'),label,badge(event.status));return row;
}
export async function timeline(open,cursor='',target=null) {
  try {
    const data=await get(`/api/activity?limit=10&cursor=${encodeURIComponent(cursor)}`),box=node('div','timeline');
    for(const event of data.items) box.append(eventItem(event,open));
    if(!data.items.length) box.append(empty('No activity yet','Persistent facts and live stage observations will appear here.'));
    if(data.has_more) box.append(action('Earlier activity ↓',()=>open('activity',data.next_cursor),'more-button'));
    box.append(node('p','muted','Bounded recent window · source timestamps are labelled.'));
    if(target) target.append(box);else replace(content('activity'),data,box);
  } catch {if(target)target.append(errorBox());else replace(content('activity'),{error:true},errorBox());}
}
