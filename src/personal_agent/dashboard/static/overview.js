import {node, get, empty, errorBox, objectCard, action} from './components.js';
import {openObject} from './inspectors.js';
import {memoryDirectory} from './memory-character.js';
import {status, timeline, replace} from './monitor.js';

const content = id => document.getElementById(`content-${id}`);
const open = (kind, id) => openObject(kind, id, true);
let profile = null;

async function collection(id, kind, limit, className) {
  const target = content(id);
  try {
    const data = await get(`/api/${kind === 'jobs' ? 'learning/jobs' : kind}?limit=${limit}`);
    const box = node('div');
    const grid = node('div', className);
    data.items.forEach(item => grid.append(objectCard(kind, item, open)));
    if (!data.items.length) grid.append(empty(`No ${kind} yet`, '真实记录形成后，会出现在这里。'));
    box.append(grid, action(`Explore all ${kind} ↗`, () => open('collection', kind)));
    if (id === 'threads') box.append(action('All Episodes, including unassigned ↗', () => open('collection', 'episodes')));
    replace(target, data, box);
  } catch { replace(target, {error: true}, errorBox()); }
}

async function character() {
  try {
    profile ??= await get('/api/character');
    const state = profile.enabled ? await get('/api/character/state?limit=1') : null;
    const box = node('div');
    if (!profile.enabled) box.append(empty('No active character package', 'Character is disabled.'));
    else {
      box.append(node('h3', '', profile.character_id));
      box.append(node('p', 'notice', state.active ? `Simulated mood: ${state.active.mood}` : 'No active short-term state.'));
      if (state.active?.current_focus) box.append(node('p', 'muted', state.active.current_focus));
    }
    box.append(action('Explore profile & state ↗', () => open('character', 'current')));
    replace(content('character'), {profile, state}, box);
  } catch { replace(content('character'), {error: true}, errorBox()); }
}

// Memory is read once, then only by explicit navigation. No polling of the file tree.
try {
  content('memory').replaceChildren();
  await memoryDirectory(content('memory'), '', open);
  content('memory').append(action('Explore all Learning jobs ↗', () => open('collection', 'jobs')));
} catch { content('memory').replaceChildren(errorBox()); }
const jobs = node('div'); jobs.id = 'job-diagnostics';
const jobList = node('div'); jobList.id = 'content-job-list';
content('jobs').replaceChildren(jobs, jobList);

let refreshCount = 0;
async function refresh() {
  if (!document.hidden) {
    const work = [status(open), timeline(open)];
    if (refreshCount % 5 === 0) work.push(
      collection('threads', 'threads', 3, 'nested-grid'),
      collection('runs', 'runs', 8, 'run-list'),
      collection('job-list', 'jobs', 4, 'stack'),
      character(),
    );
    await Promise.allSettled(work);
    refreshCount++;
  }
  window.setTimeout(refresh, 3000);
}
refresh();
