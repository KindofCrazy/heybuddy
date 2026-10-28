import {node, get, badge, empty, errorBox, objectCard, time, action} from './components.js';
import {openObject} from './inspectors.js';
import {memoryDirectory} from './memory-character.js';
const content = id => document.getElementById(`content-${id}`);
const open = (kind, id) => openObject(kind,id,true);
async function collection(id, kind, limit, className) {
  const target = content(id);
  try {
    const data = await get(`/api/${kind === 'jobs' ? 'learning/jobs' : kind}?limit=${limit}`);
    if (!data.items.length) {target.replaceChildren(empty(`No ${kind} yet`, '真实记录形成后，会出现在这里。')); return;}
    const grid = node('div', className); data.items.forEach(item => grid.append(objectCard(kind, item, open))); target.replaceChildren(grid);
    target.append(action(`Explore all ${kind} ↗`,() => open('collection',kind)));
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
try {
  const character = await get('/api/character');
  content('character').replaceChildren(character.enabled ? node('h3','',character.character_id) : empty('No active character package','Character is disabled.'));
  if (character.enabled) {const state = await get('/api/character/state?limit=1'); content('character').append(node('p','notice',state.active ? `Simulated mood: ${state.active.mood}` : 'No active short-term state.'));}
  content('character').append(action('Explore profile & state ↗',() => open('character','current')));
} catch {content('character').replaceChildren(errorBox());}
try {content('memory').replaceChildren(); await memoryDirectory(content('memory'),'',open);} catch {content('memory').replaceChildren(errorBox());}
content('threads').append(action('Explore all Episodes, including unassigned ↗',() => open('collection','episodes')));
