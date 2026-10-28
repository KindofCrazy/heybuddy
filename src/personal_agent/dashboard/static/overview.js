import {node, get, badge, empty, errorBox, objectCard, time} from './components.js';
const content = id => document.getElementById(`content-${id}`);
const open = (kind, id) => {location.hash = `inspect/${kind}/${encodeURIComponent(id)}`;};
async function collection(id, kind, limit, className) {
  const target = content(id);
  try {
    const data = await get(`/api/${kind === 'jobs' ? 'learning/jobs' : kind}?limit=${limit}`);
    if (!data.items.length) {target.replaceChildren(empty(`No ${kind} yet`, '真实记录形成后，会出现在这里。')); return;}
    const grid = node('div', className); data.items.forEach(item => grid.append(objectCard(kind, item, open))); target.replaceChildren(grid);
  } catch {target.replaceChildren(errorBox());}
}
async function status() {
  try {
    const data = await get('/api/status'); const word = node('div', 'status-word');
    word.append(node('span', `status-dot ${data.state === 'Idle' ? 'online' : ''}`), node('span', '', data.state));
    const meta = node('dl', 'meta-grid');
    for (const [key, value] of [['STAGE',data.current_stage || 'No active stage'],['LATEST RUN',data.latest_run?.status || 'No runs yet'],['QQ CHANNEL',data.qq],['LAST ACTIVITY',time(data.latest_run?.ended_at)]]) {const row = node('div'); row.append(node('dt','',key),node('dd','',value)); meta.append(row);}
    content('status').replaceChildren(badge('HOST OBSERVED'), word, meta);
    document.getElementById('connection').textContent = 'Connected'; document.getElementById('connection-dot').classList.add('online');
  } catch {content('status').replaceChildren(errorBox()); document.getElementById('connection').textContent = 'Disconnected';}
}
await Promise.allSettled([status(), collection('threads','threads',3,'nested-grid'), collection('runs','runs',8,'run-list'), collection('jobs','jobs',4,'stack')]);
content('activity').replaceChildren(empty('Following the work','Activity monitoring connects in the next milestone.'));
content('character').replaceChildren(empty('Character profile','Open the Character inspector to explore available state.'));
content('memory').replaceChildren(empty('MemoryFS','Markdown files are read on demand.'));
