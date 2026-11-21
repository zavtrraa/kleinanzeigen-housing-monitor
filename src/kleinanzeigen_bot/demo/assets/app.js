const el = id => document.getElementById(id);
let busy = false;
let noticeTimer;

function notice(text) {
  el('notice').textContent = text;
  el('notice').classList.add('visible');
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => el('notice').classList.remove('visible'), 3500);
}

function render(state) {
  const prefs = state.preferences;
  for (const option of el('city').options) option.selected = prefs.cities.includes(option.value);
  el('query').value = prefs.query;
  el('budget').value = prefs.max_rent;
  el('area').value = prefs.min_area;
  el('replies').checked = prefs.auto_reply;
  el('mode').textContent = state.status.paused ? 'Monitoring paused' : 'Monitoring ready';
  el('queued').textContent = state.status.jobs.queued || 0;
  el('queue-note').textContent = state.status.paused ? 'Resume to process queued searches' : 'Runs are processed one at a time';
  const last = state.reports.at(-1);
  el('found').textContent = last?.found ?? '—';
  el('matched').textContent = last?.matched ?? '—';
  el('notified').textContent = last?.notified ?? '—';
  const box = el('messages');
  box.replaceChildren();
  for (const item of state.messages) {
    const bubble = document.createElement('article');
    bubble.className = 'bubble' + (item.kind === 'user' ? ' user' : '');
    if (item.kind !== 'user') {
      const sender = document.createElement('div'); sender.className = 'sender';
      sender.textContent = 'Housing Monitor'; bubble.append(sender);
    }
    if (item.demo_image) {
      const img = document.createElement('img'); img.className = 'listing-image';
      img.src = '/assets/apartment.svg'; img.alt = 'Illustration of a fictional apartment building';
      const label = document.createElement('small'); label.className = 'image-label';
      label.textContent = 'Fictional listing · illustrative image'; bubble.append(img, label);
    }
    const text = document.createElement('p');
    const raw = item.text || item.caption || '';
    text.textContent = item.parse_mode === 'HTML'
      ? new DOMParser().parseFromString(raw, 'text/html').body.textContent : raw;
    bubble.append(text);
    const rows = item.reply_markup?.inline_keyboard || [];
    if (rows.length) {
      const keyboard = document.createElement('div'); keyboard.className = 'keyboard';
      for (const buttons of rows) {
        const row = document.createElement('div'); row.className = 'keyboard-row';
        for (const button of buttons) {
          const node = document.createElement('button'); node.type = 'button'; node.textContent = button.text;
          if (button.callback_data) node.addEventListener('click', () => run([button.callback_data], item.message_id));
          else node.addEventListener('click', () => notice('This is a fictional listing. Live cards link to the source website.'));
          row.append(node);
        }
        keyboard.append(row);
      }
      bubble.append(keyboard);
    }
    box.append(bubble);
  }
  box.scrollTop = box.scrollHeight;
}

async function run(commands, message_id = null) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(button => button.disabled = true);
  try {
    let state;
    for (const command of commands) {
      const response = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({command, message_id})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Command failed');
      state = result;
    }
    if (state) render(state);
  } catch (error) { notice(error.message); }
  finally { busy = false; document.querySelectorAll('button').forEach(button => button.disabled = false); }
}

el('filters').addEventListener('submit', event => {
  event.preventDefault();
  const selectedCities = [...el('city').selectedOptions].map(option => option.value);
  if (!selectedCities.length) { notice('Choose at least one location.'); return; }
  run([`/city ${selectedCities.join(',')}`,
       `/search ${el('query').value.trim() || '-'}`, `/budget ${el('budget').value}`, `/area ${el('area').value}`]);
});
el('command-form').addEventListener('submit', event => {
  event.preventDefault(); const command = el('command').value.trim();
  if (command) { el('command').value = ''; run([command]); }
});
el('replies').addEventListener('change', () => run([`/replies ${el('replies').checked ? 'on' : 'off'}`]));
document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => run([button.dataset.command])));
fetch('/api/state').then(response => response.json()).then(render).catch(() => notice('Cannot connect to the local demo.'));
