const $ = (selector, root = document) => root.querySelector(selector);
const view = $('#view');

const STATUS = {
  recording: 'در حال ضبط',
  queued: 'در صف پردازش',
  preparing: 'آماده‌سازی صدا',
  transcribing: 'در حال رونویسی',
  summarizing: 'در حال تحلیل',
  done: 'آماده',
  failed: 'ناموفق',
};
const ACTIVE = new Set(['queued', 'preparing', 'transcribing', 'summarizing']);
const POLLED = new Set([...ACTIVE, 'recording']); // pages refresh themselves while a meeting is in one of these states
const STEPS = [['preparing', 'آماده‌سازی صدا'], ['transcribing', 'رونویسی'], ['summarizing', 'تحلیل و استخراج وظایف']];
const POLL_MS = 2000;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fa = (n) => Number(n).toLocaleString('fa-IR');
const toDate = (s) => new Date(`${s.replace(' ', 'T')}Z`);
const fmtDateTime = (s) => toDate(s).toLocaleString('fa-IR', { dateStyle: 'medium', timeStyle: 'short' });
const fmtTime = (s) => toDate(s).toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' });
const MARKDOWN_ALLOW = { ADD_TAGS: ['input'], ADD_ATTR: ['checked', 'disabled', 'type'] }; // keeps "- [ ]" checklists
// The document's direction comes from its majority script (a stray English heading must not flip a Persian text); paragraphs and lists still follow their own first letter.
const textDir = (t) => ((t.match(/[\u0600-\u06FF]/g) ?? []).length >= (t.match(/[A-Za-z]/g) ?? []).length ? 'rtl' : 'ltr');
window.DOMPurify?.addHook('afterSanitizeAttributes', (node) => /^(P|UL|OL|LI|BLOCKQUOTE)$/.test(node.tagName) && node.setAttribute('dir', 'auto'));
const markdown = (text) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(text), MARKDOWN_ALLOW) : `<pre>${esc(text)}</pre>`);
const faRaw = (n) => String(n).replace(/\d/g, (d) => '۰۱۲۳۴۵۶۷۸۹'[d]);
const todayIso = () => new Date().toLocaleDateString('sv-SE'); // local date as YYYY-MM-DD
const fmtJalali = (iso, dateStyle = 'long') => new Date(`${iso}T12:00:00Z`).toLocaleDateString('fa-IR', { dateStyle, timeZone: 'UTC' });
const state = (status, extra = '') => `<span class="state ${status}"><i></i>${STATUS[status]}${extra}</span>`;
const fmtDuration = (s) => (s >= 60 ? `${fa(Math.floor(s / 60))} دقیقه${s % 60 ? ` و ${fa(s % 60)} ثانیه` : ''}` : `${fa(s)} ثانیه`);
const fmtMegabytes = (bytes) => fa((bytes / 1048576).toFixed(1));

/* Live job state from the server -> one overall fraction (null = unknown) and a plain-language sentence. */
function overall(job) {
  if (!job) return null;
  if (job.stage === 'transcribing') return 0.05 + 0.8 * job.fraction;
  if (job.stage === 'summarizing') return 0.85 + 0.15 * job.fraction;
  return null;
}

function describe(status, job) {
  if (status === 'queued' || !job) return 'در صف پردازش…';
  if (job.stage === 'preparing') return 'صدا برای ارسال فشرده و بخش‌بندی می‌شود…';
  if (job.stage === 'transcribing') {
    const parts = job.parts > 1 ? ` · ${fa(job.done)} از ${fa(job.parts)} بخش تمام شد` : '';
    return `صدا به سرویس رونویسی ارسال شده و در حال تبدیل به متن است${parts}`;
  }
  const names = { minutes: 'صورت‌جلسه', tasks: 'وظایف' };
  const finished = (job.finished ?? []).map((key) => names[key]);
  return finished.length ? `${finished.join(' و ')} آماده شد؛ در حال تکمیل بقیه…` : 'مدل در حال نوشتن صورت‌جلسه و استخراج وظایف است…';
}

function timing(job) {
  if (!job) return '';
  const parts = [`${fmtDuration(job.elapsed)} گذشته`];
  if (job.stage === 'transcribing' && job.parts > 1 && job.fraction >= 0.08) {
    const minutes = Math.round((job.stage_elapsed * (1 - job.fraction)) / job.fraction / 60);
    parts.push(minutes < 1 ? 'کمتر از یک دقیقه‌ی دیگر (تخمینی)' : `حدود ${fa(minutes)} دقیقه‌ی دیگر (تخمینی)`);
  }
  return parts.join(' · ');
}

/* Browser -> app upload: shown on every page, because it must finish before the browser can be closed. */
let uploading = false;
const warnBeforeUnload = (e) => e.preventDefault();

function upload(formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/meetings');
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded, e.total);
    xhr.onload = () => {
      const body = JSON.parse(xhr.responseText || '{}');
      xhr.status < 300 ? resolve(body) : reject(new Error(typeof body.detail === 'string' ? body.detail : 'آپلود ناموفق بود.'));
    };
    xhr.onerror = () => reject(new Error('آپلود ناموفق بود؛ اتصال را بررسی کنید.'));
    xhr.send(formData);
  });
}

function showUploadProgress(name, loaded, total) {
  const strip = $('#uploading');
  strip.hidden = false;
  $('span', strip).textContent = `در حال آپلود «${name}» · ${fmtMegabytes(loaded)} از ${fmtMegabytes(total)} مگابایت`;
  $('b', strip).textContent = `${fa(Math.round((loaded / total) * 100))}٪`;
  $('.bar i', strip).style.width = `${(loaded / total) * 100}%`;
}

function dayLabel(iso) {
  const days = Math.round((new Date(todayIso()) - new Date(iso)) / 864e5); // both parse as UTC midnight
  return days === 0 ? 'امروز' : days === 1 ? 'دیروز' : fmtJalali(iso);
}

async function api(path, { method = 'GET', body } = {}) {
  const isForm = body instanceof FormData;
  const response = await fetch(`/api${path}`, {
    method,
    headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
    body: body && !isForm ? JSON.stringify(body) : body,
  });
  if (!response.ok) {
    const { detail } = await response.json().catch(() => ({}));
    throw new Error(typeof detail === 'string' ? detail : response.statusText);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, kind = 'ok', action = null) {
  const el = $('#toast');
  el.textContent = message;
  if (action) {
    const button = Object.assign(document.createElement('button'), { type: 'button', className: 'toast-action', textContent: action.label });
    button.onclick = guard(async () => { el.className = ''; await action.run(); });
    el.append(' ', button);
  }
  el.className = `show ${kind} ${action ? 'actionable' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = ''), action ? 8000 : 3500); // an undo stays long enough to be used
}
const guard = (fn) => async (...args) => {
  try { return await fn(...args); } catch (e) { toast(e.message, 'err'); }
};

/* ---------- model choice: made for every meeting, never globally ---------- */

const providerOf = (id) => (id.includes('/') ? id.split('/')[0] : 'other');
const lastModel = () => { try { return localStorage.getItem('lastModel') ?? ''; } catch { return ''; } };
const rememberModel = (id) => { try { localStorage.setItem('lastModel', id); } catch { /* private mode: the choice is just not remembered */ } };
let modelList; // the provider's model list: fetched once per page load, again on demand
const loadModels = async (force = false) => (modelList = !force && modelList ? modelList : await api('/models'));

/** Two linked selects (provider, model) rendered into `host`. `selected` is a meeting's own model; otherwise the last choice is offered. */
function modelPicker(host, selected = '') {
  host.classList.add('model-picker');
  host.innerHTML = '<div class="select"><select class="provider" aria-label="پروایدر"><option>در حال دریافت…</option></select></div><div class="select"><select class="model" dir="ltr" aria-label="مدل"></select></div>';
  const providerSelect = $('.provider', host), modelSelect = $('.model', host);
  let byProvider = {};
  // Nothing is preselected unless a choice was made before: an empty value means "the user has not chosen yet".
  const showModels = () => {
    const ids = byProvider[providerSelect.value] ?? [];
    modelSelect.innerHTML = providerSelect.value
      ? `<option value="">مدل…</option>${ids.map((id) => `<option value="${esc(id)}">${esc(id.split('/').slice(1).join('/') || id)}</option>`).join('')}`
      : '<option value="">ابتدا پروایدر را انتخاب کنید</option>';
  };
  const fill = (models, wanted) => {
    const ids = selected && !models.includes(selected) ? [...models, selected] : models; // a meeting's model stays visible even if the provider dropped it
    const pick = ids.includes(wanted) ? wanted : ids.includes(lastModel()) && !selected ? lastModel() : '';
    byProvider = Object.groupBy(ids, providerOf);
    providerSelect.innerHTML = `<option value="">پروایدر…</option>${Object.keys(byProvider).map((p) => `<option>${esc(p)}</option>`).join('')}`;
    if (pick) providerSelect.value = providerOf(pick);
    showModels();
    if (pick) modelSelect.value = pick;
  };
  const failed = (e) => {
    providerSelect.innerHTML = '<option value="">پروایدر…</option>';
    modelSelect.innerHTML = `<option value="">${esc(e.message)}</option>`;
  };
  const load = (force) => loadModels(force).then((models) => fill(models, modelSelect.value || selected), failed);
  providerSelect.addEventListener('change', showModels);
  load(false);
  return {
    get value() { return modelSelect.value; },
    set(id) { if (modelList) fill(modelList, id); },
    reload: () => load(true),
  };
}

/* ---------- participants: pick from the people directory, or add a guest ---------- */

/** Toggle chips for everyone in the directory, plus a box for a guest who is not in it. `value` is the chosen names. */
function participantPicker(host) {
  const chosen = new Set();
  const chip = (name, { guest = false, title = '' } = {}) =>
    `<button type="button" class="chip-toggle ${guest ? 'guest' : ''}" data-name="${esc(name)}" aria-pressed="${chosen.has(name)}" title="${esc(title)}">${esc(name)}</button>`;
  const draw = (directory = []) => {
    host.className = 'chips';
    host.innerHTML = `${directory.map((p) => chip(p.name, { title: p.role })).join('')}
      <input class="guest-input" placeholder="${directory.length ? 'نام دیگر…' : 'نام شرکت‌کننده…'}" aria-label="افزودن نام شرکت‌کننده">
      ${directory.length ? '' : '<span class="hint muted">افراد را در تنظیمات تعریف کنید تا با یک کلیک انتخاب شوند.</span>'}`;
  };
  const addGuest = (input) => {
    const name = input.value.trim().replace(/\s+/g, ' ');
    input.value = '';
    if (!name || chosen.has(name)) return;
    chosen.add(name);
    input.insertAdjacentHTML('beforebegin', chip(name, { guest: true }));
  };
  host.addEventListener('click', (e) => {
    const button = e.target.closest('.chip-toggle');
    if (!button) return;
    const { name } = button.dataset;
    if (chosen.delete(name)) button.classList.contains('guest') ? button.remove() : button.setAttribute('aria-pressed', 'false');
    else { chosen.add(name); button.setAttribute('aria-pressed', 'true'); }
  });
  host.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.matches('.guest-input')) { e.preventDefault(); addGuest(e.target); } });
  host.addEventListener('focusout', (e) => e.target.matches?.('.guest-input') && addGuest(e.target));
  let directory = [];
  api('/people').then((list) => { directory = list; draw(list); }, () => draw());
  return {
    get value() { return [...chosen].join(', '); },
    clear() { chosen.clear(); draw(directory); },
  };
}

/* ---------- tasks (shared by the meeting page and the tasks page) ---------- */

const ICONS = {
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
  edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5ZM15 5l4 4"/></svg>',
  trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2M10 11v6M14 11v6"/></svg>',
};
const PRIORITY = { high: 'فوری', medium: 'عادی', low: 'کم‌اولویت' };
const isOverdue = (t) => !t.done && t.deadline && t.deadline < todayIso();
let onTaskChange = null; // the open page may want to redraw when a task is ticked

function taskRow(t, withMeeting, compact = false, removable = false) {
  const meta = (compact
    ? [esc(t.owner ?? ''), isOverdue(t) ? `<span class="urgent">موعد ${fmtJalali(t.deadline, 'medium')}</span>` : '']
    : [
      t.priority === 'high' ? '<b class="urgent">فوری</b>' : t.priority === 'low' ? PRIORITY.low : '',
      t.deadline ? `<span class="${isOverdue(t) ? 'urgent' : ''}">موعد ${fmtJalali(t.deadline, 'medium')}</span>` : '',
      withMeeting ? `<a href="#/meeting/${t.meeting_id}">${esc(t.meeting_title)}</a>` : '',
      t.jira_key ? `<a class="jira-key" href="${esc(t.jira_link ?? '#')}" target="_blank" rel="noopener" dir="ltr">${esc(t.jira_key)}</a>` : '',
    ]).filter(Boolean).join(' · ');
  return `
    <div class="task ${t.done ? 'done' : ''}">
      <label class="check"><input type="checkbox" data-task="${t.id}" ${t.done ? 'checked' : ''} aria-label="انجام شد"><span class="box" aria-hidden="true"></span></label>
      <div class="task-main"><a class="task-text" href="#/task/${t.id}" dir="auto">${esc(t.title)}</a>${meta ? `<span class="task-meta">${meta}</span>` : ''}</div>
      ${removable ? `<button class="icon-btn mini danger row-del" data-delete-task="${t.id}" title="حذف وظیفه" aria-label="حذف وظیفه ${esc(t.title)}">${ICONS.trash}</button>` : ''}
    </div>`;
}

const taskGroups = (tasks, withMeeting = false, removable = false) =>
  Object.entries(Object.groupBy(tasks, (t) => t.owner || 'بدون مسئول')).map(([owner, items]) => `
    <section class="owner">
      <h3><b>${esc(owner)}</b><small>${fa(items.length)} وظیفه</small></h3>
      ${items.map((t) => taskRow(t, withMeeting, false, removable)).join('')}
    </section>`).join('');

/* ---------- meetings ---------- */

function meetingsView() {
  view.innerHTML = `
    <h1 class="title">جلسه‌ها</h1>
    <p class="lead">همه‌ی جلسه‌ها، به ترتیب روزی که برگزار شده‌اند.</p>
    <section id="list" aria-live="polite"></section>`;
  loadList();
}

const WAVE = `<svg viewBox="0 0 52 32" aria-hidden="true">${[8, 16, 26, 12, 22, 30, 14, 20, 10].map((h, i) => `<rect x="${i * 6}" y="${(32 - h) / 2}" width="3" height="${h}" rx="1.5"/>`).join('')}</svg>`;
const FILE_HINT = 'یا برای انتخاب کلیک کنید · MP3، WAV، M4A، MP4 تا ۲۵۶ مگابایت';

function newMeetingView() {
  view.innerHTML = `
    <a class="crumb" href="#/meetings">جلسه‌ها</a>
    <h1 class="title">جلسه‌ی جدید</h1>
    <p class="lead">فایل صوتی یا تصویری جلسه را بدهید؛ رونویسی، صورت‌جلسه و وظایف ساخته می‌شود.</p>
    <form id="upload" class="form new-meeting">
      <label class="file-panel" id="drop">
        <input type="file" name="file" accept="audio/*,video/*">
        <span class="wave">${WAVE}</span>
        <span class="file-main"><strong id="drop-title">فایل جلسه را اینجا رها کنید</strong><span class="muted" id="drop-hint">${FILE_HINT}</span></span>
        <span class="btn" id="drop-action">انتخاب فایل</span>
      </label>
      <section>
        <div class="setting">
          <label for="m-title">عنوان<span class="hint">اگر خالی بماند، از روی گفتگوی جلسه نوشته می‌شود.</span></label>
          <input id="m-title" name="title" autocomplete="off">
        </div>
        <div class="setting">
          <label for="m-date">تاریخ جلسه<span class="hint" id="held-hint">پیش‌فرض، تاریخ خودِ فایل است.</span></label>
          <input id="m-date" type="date" name="held_on">
        </div>
        <div class="setting">
          <div class="label">شرکت‌کنندگان<span class="hint">از فهرست افراد انتخاب کنید یا نام مهمان را بنویسید.</span></div>
          <div id="upload-people"></div>
        </div>
        <div class="setting">
          <div class="label">مدل پردازش<span class="hint">برای جلسه‌ی طولانی یا مهم مدل قوی‌تر، و برای جلسه‌ی ساده مدل سبک‌تر.</span></div>
          <div id="upload-model"></div>
        </div>
      </section>
      <div class="form-actions"><button class="btn primary" type="submit" disabled>شروع پردازش</button><span class="muted" id="submit-hint">ابتدا فایل جلسه را انتخاب کنید.</span></div>
    </form>`;

  const form = $('#upload'), drop = $('#drop'), input = form.file;
  const picker = modelPicker($('#upload-model'));
  const attendees = participantPicker($('#upload-people'));
  const showHeld = () => {
    $('#held-hint').textContent = form.held_on.value ? `${fmtJalali(form.held_on.value)} · پیش‌فرض، تاریخ خودِ فایل است.` : 'پیش‌فرض، تاریخ خودِ فایل است.';
  };
  const showFile = () => {
    const file = input.files[0];
    drop.classList.toggle('has-file', !!file);
    $('#drop-title').textContent = file ? file.name : 'فایل جلسه را اینجا رها کنید';
    $('#drop-hint').textContent = file ? `${fa((file.size / 1048576).toFixed(1))} مگابایت` : FILE_HINT;
    $('#drop-action').textContent = file ? 'تغییر فایل' : 'انتخاب فایل';
    $('button[type=submit]', form).disabled = !file;
    $('#submit-hint').textContent = file ? '' : 'ابتدا فایل جلسه را انتخاب کنید.';
    if (file) {
      form.held_on.value = new Date(file.lastModified).toLocaleDateString('sv-SE'); // a recording's modified time is when it ended
      showHeld();
    }
  };
  form.held_on.addEventListener('change', showHeld);
  input.addEventListener('change', showFile);
  ['dragenter', 'dragover'].forEach((type) => drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((type) => drop.addEventListener(type, () => drop.classList.remove('over')));
  drop.addEventListener('drop', (e) => { e.preventDefault(); input.files = e.dataTransfer.files; showFile(); });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (uploading) return toast('آپلود قبلی هنوز ادامه دارد.', 'err');
    const file = input.files[0];
    const model = picker.value;
    if (!model) return toast('یک مدل برای پردازش این جلسه انتخاب کنید.', 'err');
    rememberModel(model);
    const data = new FormData(form);
    data.set('model', model);
    data.set('participants', attendees.value);
    location.hash = '#/meetings'; // the upload goes on in the background; its progress is shown on every page
    sendMeeting(data, file);
  });
}

async function sendMeeting(data, file) {
  uploading = true;
  window.addEventListener('beforeunload', warnBeforeUnload);
  showUploadProgress(file.name, 0, file.size);
  try {
    await upload(data, (loaded, total) => showUploadProgress(file.name, loaded, total));
    toast('آپلود تمام شد. پردازش روی سرور ادامه دارد و می‌توانید مرورگر را ببندید.');
    if ($('#list')) loadList();
  } catch (error) {
    toast(error.message, 'err');
  } finally {
    uploading = false;
    window.removeEventListener('beforeunload', warnBeforeUnload);
    $('#uploading').hidden = true;
  }
}

const loadList = guard(async () => {
  const el = $('#list');
  if (!el) return;
  const meetings = (await api('/meetings')).sort((a, b) => (b.held_on ?? '').localeCompare(a.held_on ?? '') || b.created_at.localeCompare(a.created_at));
  const days = Object.groupBy(meetings, (m) => dayLabel(m.held_on ?? m.created_at.slice(0, 10)));
  el.innerHTML = meetings.length
    ? Object.entries(days).map(([day, items]) => `
        <h2 class="group-day">${day}</h2>
        ${items.map((m) => `
          <a class="entry" href="#/meeting/${m.id}">
            <span class="entry-main"><b dir="auto">${esc(m.title)}</b><small>بارگذاری ${fmtTime(m.created_at)}</small></span>
            <span class="entry-side">
              ${m.status === 'done' && m.open_tasks ? `<span class="open">${fa(m.open_tasks)} وظیفه باز</span>` : ''}
              ${state(m.status, overall(m.progress) == null ? '' : ` · ${fa(Math.round(overall(m.progress) * 100))}٪`)}
            </span>
          </a>`).join('')}`).join('')
    : '<p class="empty">هنوز جلسه‌ای ندارید. <a class="inline-link" href="#/new">جلسه‌ی جدید بسازید</a> یا آن را با میکروفن ضبط کنید.</p>';
  if (meetings.some((m) => POLLED.has(m.status))) timer = setTimeout(loadList, POLL_MS);
});

function progressPanel(m, current) {
  const fraction = overall(m.progress);
  return `
    <section class="progress" aria-live="polite">
      <ol class="steps">${STEPS.map(([, label], i) => `<li class="${i < current ? 'done' : i === current ? 'current' : ''}">${label}</li>`).join('')}</ol>
      <div class="bar ${fraction == null ? 'indeterminate' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${fraction == null ? '' : Math.round(fraction * 100)}"><i style="width:${(fraction ?? 0) * 100}%"></i></div>
      <p class="line"><b>${describe(m.status, m.progress)}</b><span class="muted">${timing(m.progress)}</span></p>
      <p class="note muted">می‌توانید این صفحه یا مرورگر را ببندید؛ پردازش روی سرور ادامه پیدا می‌کند و نتیجه در فهرست جلسه‌ها می‌ماند.</p>
    </section>`;
}

async function meetingView(id) {
  const m = await api(`/meetings/${id}`);
  const open = m.tasks.filter((t) => !t.done).length;
  const current = STEPS.findIndex(([key]) => key === m.status);
  const minutes = m.minutes?.replace(/^#\s.*\n+/, '') ?? ''; // the title is already the page heading

  view.innerHTML = `
    <a class="crumb" href="#/meetings">جلسه‌ها</a>
    <header class="head">
      <div class="head-row">
        <h1 class="title" dir="auto">${esc(m.title)}</h1>
        <button class="icon-btn danger" data-action="delete" title="حذف جلسه" aria-label="حذف جلسه">${ICONS.trash}</button>
      </div>
      <p class="meta">
        <span>${fmtJalali(m.held_on ?? m.created_at.slice(0, 10))}<input type="date" id="held-input" class="pick" value="${m.held_on ?? ''}" aria-label="تاریخ جلسه"><button class="link" data-action="date">تغییر تاریخ</button></span>
        ${m.participants ? `<span>شرکت‌کنندگان: ${esc(m.participants)}</span>` : ''}
        ${m.model ? `<span>مدل: <bdi dir="ltr">${esc(m.model)}</bdi></span>` : ''}
        ${state(m.status)}
      </p>
    </header>
    ${m.status === 'recording' ? '<div class="notice"><b>این جلسه در حال ضبط است</b><p class="muted">بعد از پایان ضبط، پردازش خودکار شروع می‌شود. اگر ضبط رها شود، بعد از دو دقیقه همان مقدار ضبط‌شده پردازش می‌شود.</p></div>' : ''}
    ${ACTIVE.has(m.status) ? progressPanel(m, current) : ''}
    ${m.status === 'failed' ? `
      <div class="notice">
        <b>پردازش کامل نشد</b>
        <p dir="auto">${esc(m.error)}</p>
        ${m.minutes || m.tasks_extracted ? '<p class="muted">بخش‌های موفق ذخیره شده‌اند؛ «تلاش دوباره» فقط بخش ناموفق را از نو می‌سازد.</p>' : ''}
        <div class="retry"><span class="muted">مدل</span><div id="retry-model"></div><button class="btn" data-action="retry">تلاش دوباره</button></div>
      </div>` : ''}
    ${m.minutes || m.tasks_extracted ? `
      <div class="detail">
        <div class="sheet-wrap">
          <article class="sheet">${m.minutes ? `<div class="prose" dir="${textDir(minutes)}">${markdown(minutes)}</div>` : '<p class="muted">صورت‌جلسه ساخته نشد؛ «تلاش دوباره» را بزنید.</p>'}</article>
          ${m.minutes ? `<button class="icon-btn side" data-action="copy" title="کپی صورت‌جلسه" aria-label="کپی صورت‌جلسه">${ICONS.copy}</button>` : ''}
        </div>
        <aside class="rail">
          <h2 class="section-title">وظایف<small>${fa(open)} باز از ${fa(m.tasks.length)}</small></h2>
          ${m.tasks.some((t) => !t.jira_key) ? `<a class="link send-link" href="#/jira?meeting=${m.id}">ارسال به Jira…</a>` : ''}
          ${!m.tasks_extracted ? '<p class="muted">استخراج وظایف انجام نشد؛ «تلاش دوباره» را بزنید.</p>' : m.tasks.length ? taskGroups(m.tasks, false, true) : '<p class="muted">در این جلسه وظیفه‌ای مشخص نشد.</p>'}
        </aside>
      </div>` : ''}
    ${m.transcript ? `<details class="transcript"><summary>متن کامل رونویسی</summary><pre dir="rtl">${esc(m.transcript)}</pre></details>` : ''}
    ${m.events.length ? `
      <details class="transcript">
        <summary>گزارش پردازش</summary>
        <p class="muted">شناسه‌ی جلسه برای جست‌وجو در لاگ‌های سرور: <code>${m.id}</code></p>
        <ol class="events" dir="ltr">${m.events.map((e) => `<li class="${e.level.toLowerCase()}"><time>${toDate(e.at).toLocaleTimeString('en-GB')}</time><span>${esc(e.message)}</span></li>`).join('')}</ol>
      </details>` : ''}`;

  const retryPicker = m.status === 'failed' ? modelPicker($('#retry-model'), m.model) : null;
  view.onclick = guard(async (e) => {
    const action = e.target.closest('[data-action]')?.dataset.action;
    if (action === 'copy') {
      await navigator.clipboard.writeText(m.minutes);
      toast('صورت‌جلسه کپی شد.');
    } else if (action === 'retry') {
      if (!retryPicker.value) return toast('یک مدل انتخاب کنید.', 'err');
      rememberModel(retryPicker.value);
      await api(`/meetings/${id}`, { method: 'PATCH', body: { model: retryPicker.value } });
      await api(`/meetings/${id}/retry`, { method: 'POST' });
      route();
    } else if (action === 'date') {
      $('#held-input').showPicker();
    } else if (action === 'delete' && confirm('این جلسه و وظایفش حذف شود؟')) {
      await api(`/meetings/${id}`, { method: 'DELETE' });
      location.hash = '#/meetings';
    }
  });
  $('#held-input').addEventListener('change', guard(async (e) => {
    if (!e.target.value) return;
    await api(`/meetings/${id}`, { method: 'PATCH', body: { held_on: e.target.value } });
    toast('تاریخ جلسه تغییر کرد.');
    route();
  }));
  if (POLLED.has(m.status)) timer = setTimeout(route, POLL_MS);
}

/* ---------- calendar: Jalali first, Gregorian in the shade ---------- */

const PERSIAN_PARTS = new Intl.DateTimeFormat('en-u-ca-persian-nu-latn', { year: 'numeric', month: 'numeric', day: 'numeric', timeZone: 'UTC' });
const PERSIAN_MONTH = new Intl.DateTimeFormat('fa-IR-u-ca-persian', { month: 'long', timeZone: 'UTC' });
const WEEKDAYS = ['شنبه', 'یکشنبه', 'دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه'];
const WEEKDAY_INITIALS = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج'];
const MAX_ITEMS_PER_DAY = 3;
const MAX_AGENDA_ITEMS = 5;

// Dates are held at noon UTC so that no timezone or DST shift can move them to another day.
const noon = (iso) => new Date(`${iso}T12:00:00Z`);
const isoOf = (date) => date.toISOString().slice(0, 10);
const addDays = (date, n) => new Date(date.getTime() + n * 864e5);
const weekdayOf = (date) => (date.getUTCDay() + 1) % 7; // the Persian week starts on Saturday

function jalali(date) {
  const parts = Object.fromEntries(PERSIAN_PARTS.formatToParts(date).map(({ type, value }) => [type, +value]));
  return { y: parts.year, m: parts.month, d: parts.day };
}

function monthDays({ y, m }) {
  const rank = (date) => { const j = jalali(date); return j.y * 10000 + j.m * 100 + j.d; };
  const first = y * 10000 + m * 100 + 1;
  let day = new Date(Date.UTC(y + 621, 2, 21 + Math.round((m - 1) * 30.4), 12)); // a few days of slack, then walk to the 1st
  while (rank(day) > first) day = addDays(day, -1);
  while (rank(day) < first) day = addDays(day, 1);
  const days = [];
  for (; jalali(day).m === m; day = addDays(day, 1)) days.push(day);
  return days;
}

const cal = {}; // the month on screen, the selected day and which agenda groups are folded; kept while you move between pages
document.addEventListener('toggle', (e) => { // remember folds made by the user (toggle does not bubble, hence capture)
  const group = e.target.dataset?.group;
  if (group && cal.open) cal.open[group] = e.target.open;
}, true);

async function calendarView() {
  const [meetings, tasks] = await Promise.all([api('/meetings'), api('/tasks?include_done=true')]);
  const meetingsByDay = Object.groupBy(meetings.filter((m) => m.held_on), (m) => m.held_on);
  const tasksByDay = Object.groupBy(tasks.filter((t) => t.deadline), (t) => t.deadline);
  const today = todayIso();
  if (!cal.selected) Object.assign(cal, { ...jalali(noon(today)), selected: today });

  const gregorian = (date, options) => date.toLocaleDateString('en-GB', { ...options, timeZone: 'UTC' });
  const itemsOf = (iso) => [
    ...(meetingsByDay[iso] ?? []).map((m) => `<span class="item meeting"><i></i>${esc(m.title)}</span>`),
    ...(tasksByDay[iso] ?? []).map((t) => `<span class="item task ${t.done ? 'done' : isOverdue(t) ? 'late' : ''}"><i></i>${esc(t.title)}</span>`),
  ];

  const cell = (date) => {
    const iso = isoOf(date), items = itemsOf(iso), j = jalali(date);
    const label = `${faRaw(j.d)} ${PERSIAN_MONTH.format(date)}، ${fa(items.length)} مورد`;
    return `
      <button class="day ${iso === cal.selected ? 'selected' : ''} ${iso === today ? 'today' : ''} ${weekdayOf(date) === 6 ? 'friday' : ''}" data-date="${iso}" aria-label="${label}" aria-pressed="${iso === cal.selected}">
        <span class="num"><b>${faRaw(j.d)}</b><small dir="ltr">${date.getUTCDate() === 1 ? gregorian(date, { day: 'numeric', month: 'short' }) : date.getUTCDate()}</small></span>
        <span class="items">${items.slice(0, MAX_ITEMS_PER_DAY).join('')}${items.length > MAX_ITEMS_PER_DAY ? `<span class="more">${faRaw(items.length - MAX_ITEMS_PER_DAY)}+ مورد دیگر</span>` : ''}</span>
        <span class="marks">${'<i></i>'.repeat(Math.min(items.length, 4))}</span>
      </button>`;
  };

  // Groups can be folded and stay folded while you move around; only today's own items start open.
  const open = (cal.open ??= { day: true, due: true, late: false, undated: false });
  const agenda = () => {
    const date = noon(cal.selected), j = jalali(date);
    const dayMeetings = meetingsByDay[cal.selected] ?? [], dayTasks = tasksByDay[cal.selected] ?? [];
    const late = tasks.filter(isOverdue), undated = tasks.filter((t) => !t.done && !t.deadline);
    const group = (key, title, items, render, empty, moreHref, kind = '') => `
      <details class="agenda-group ${kind}" data-group="${key}" ${open[key] ? 'open' : ''}>
        <summary>${title}<small>${fa(items.length)}</small></summary>
        ${items.length ? items.slice(0, MAX_AGENDA_ITEMS).map(render).join('') : `<p class="muted">${empty}</p>`}
        ${items.length > MAX_AGENDA_ITEMS ? `<a class="more-link" href="${moreHref}">و ${fa(items.length - MAX_AGENDA_ITEMS)} مورد دیگر</a>` : ''}
      </details>`;
    const task = (t) => taskRow(t, false, true);
    return `
      <p class="overview">
        <span><b>${fa(tasks.filter((t) => !t.done).length)}</b> وظیفه‌ی باز</span>
        <button class="link ${late.length ? 'urgent' : ''}" data-open="late"><b>${fa(late.length)}</b> عقب‌افتاده</button>
        <button class="link" data-open="undated"><b>${fa(undated.length)}</b> بدون موعد</button>
      </p>
      <header class="agenda-head">
        <h2 class="section-title">${WEEKDAYS[weekdayOf(date)]} ${faRaw(j.d)} ${PERSIAN_MONTH.format(date)}</h2>
        <p class="muted" dir="ltr">${gregorian(date, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}</p>
      </header>
      ${group('day', 'جلسه‌ها', dayMeetings, (m) => `
        <a class="entry" href="#/meeting/${m.id}"><span class="entry-main"><b dir="auto">${esc(m.title)}</b></span>${state(m.status)}</a>`, 'جلسه‌ای در این روز ثبت نشده.', '#/meetings')}
      ${group('due', 'موعد وظایف', dayTasks, task, 'وظیفه‌ای در این روز موعد ندارد.', '#/tasks')}
      ${group('late', 'عقب‌افتاده', late, task, 'وظیفه‌ی عقب‌افتاده‌ای نیست.', '#/tasks', 'late')}
      ${group('undated', 'بدون موعد', undated, task, 'همه‌ی وظایف موعد دارند.', '#/tasks')}`;
  };

  const draw = () => {
    const days = monthDays(cal);
    const [first, last] = [days[0], days.at(-1)];
    const span = `${gregorian(first, { day: 'numeric', month: 'short' })} – ${gregorian(last, { day: 'numeric', month: 'short', year: 'numeric' })}`;
    view.innerHTML = `
      <div class="cal-layout">
        <section aria-label="تقویم">
          <header class="cal-head">
            <div><h1 class="title">${PERSIAN_MONTH.format(first)} ${faRaw(cal.y)}</h1><p class="cal-sub" dir="ltr">${span}</p></div>
            <div class="cal-nav">
              <button class="btn" data-nav="prev" aria-label="ماه قبل"><i class="chev prev"></i></button>
              <button class="btn" data-nav="today">امروز</button>
              <button class="btn" data-nav="next" aria-label="ماه بعد"><i class="chev next"></i></button>
            </div>
          </header>
          <div class="weekdays" aria-hidden="true">${WEEKDAY_INITIALS.map((w, i) => `<span class="${i === 6 ? 'friday' : ''}">${w}</span>`).join('')}</div>
          <div class="days">${'<span class="day blank" aria-hidden="true"></span>'.repeat(weekdayOf(first))}${days.map(cell).join('')}</div>
          <p class="legend muted"><span class="item meeting"><i></i>جلسه</span><span class="item task"><i></i>موعد وظیفه</span><span class="item task late"><i></i>عقب‌افتاده</span></p>
        </section>
        <aside class="agenda" aria-live="polite">${agenda()}</aside>
      </div>`;
  };

  const shiftMonth = (delta) => {
    const index = cal.y * 12 + (cal.m - 1) + delta;
    Object.assign(cal, { y: Math.floor(index / 12), m: (index % 12) + 1 });
    const days = monthDays(cal).map(isoOf);
    cal.selected = days.includes(today) ? today : days[0];
  };

  view.onclick = (e) => {
    const nav = e.target.closest('[data-nav]')?.dataset.nav, day = e.target.closest('[data-date]')?.dataset.date;
    const unfold = e.target.closest('[data-open]')?.dataset.open;
    if (nav === 'today') Object.assign(cal, { ...jalali(noon(today)), selected: today });
    else if (nav) shiftMonth(nav === 'next' ? 1 : -1);
    else if (day) cal.selected = day;
    else if (unfold) open[unfold] = true;
    else return;
    draw();
  };
  onTaskChange = (id, done) => {
    tasks.find((t) => t.id === id).done = done ? 1 : 0;
    draw();
  };
  draw();
}

/* ---------- tasks ---------- */

async function tasksView() {
  const showDone = tasksView.showDone ?? false;
  const tasks = await api(`/tasks?include_done=${showDone}`);
  view.innerHTML = `
    <h1 class="title">وظایف</h1>
    <p class="lead">همه‌ی کارهایی که از جلسه‌ها بیرون آمده، به تفکیک مسئول.</p>
    <div class="filter">
      <button aria-pressed="${!showDone}" data-done="false">باز</button>
      <button aria-pressed="${showDone}" data-done="true">همه</button>
    </div>
    ${tasks.length ? `<div class="columns">${taskGroups(tasks, true, true)}</div>` : `<p class="empty">${showDone ? 'هنوز وظیفه‌ای ثبت نشده.' : 'وظیفه‌ی بازی ندارید.'}</p>`}`;
  view.onclick = (e) => {
    const button = e.target.closest('[data-done]');
    if (button) { tasksView.showDone = button.dataset.done === 'true'; route(); }
  };
}

async function taskView(id) {
  const [t, directory] = await Promise.all([api(`/tasks/${id}`), api('/people')]);
  const known = directory.some((p) => p.name === t.owner);
  const editing = taskView.editing === id;
  view.innerHTML = `
    <a class="crumb" href="#/tasks">وظایف</a>
    <header class="head">
      <p class="key" dir="ltr">T-${t.id}</p>
      ${editing
        ? `<input id="edit-title" class="title-input" value="${esc(t.title)}" maxlength="255" dir="auto" aria-label="عنوان وظیفه">`
        : `<div class="head-row"><h1 class="title" dir="auto">${esc(t.title)}</h1><span class="head-tools"><button class="icon-btn" data-action="edit" title="ویرایش عنوان و توضیحات" aria-label="ویرایش عنوان و توضیحات">${ICONS.edit}</button><button class="icon-btn danger" data-delete-task="${t.id}" title="حذف وظیفه" aria-label="حذف وظیفه">${ICONS.trash}</button></span></div>`}
    </header>
    <div class="detail">
      ${editing ? `
        <article class="sheet editor">
          <div class="tabs" role="tablist"><button type="button" data-tab="write" aria-pressed="true">متن</button><button type="button" data-tab="preview" aria-pressed="false">پیش‌نمایش</button></div>
          <textarea id="edit-body" rows="20" dir="auto" aria-label="توضیحات (Markdown)">${esc(t.description)}</textarea>
          <div id="edit-preview" class="prose" hidden></div>
          <div class="form-actions"><button class="btn primary" data-action="save">ذخیره</button><button class="link" data-action="cancel">انصراف</button></div>
        </article>`
        : `<article class="sheet">${t.description ? `<div class="prose" dir="${textDir(t.description)}">${markdown(t.description)}</div>` : '<p class="muted">برای این وظیفه توضیحی ثبت نشده است.</p>'}</article>`}
      <aside class="rail">
        <h2 class="section-title">جزئیات</h2>
        <dl class="fields-list">
          <div><dt>وضعیت</dt><dd><label class="check inline"><input type="checkbox" id="task-done" ${t.done ? 'checked' : ''}><span class="box" aria-hidden="true"></span>${t.done ? 'انجام شد' : 'انجام نشده'}</label></dd></div>
          <div><dt>مسئول</dt><dd>
            <div class="select"><select id="task-owner" aria-label="مسئول">
              <option value="">تعیین نشده</option>
              ${t.owner && !known ? `<option value="${esc(t.owner)}" selected>${esc(t.owner)} (در فهرست افراد نیست)</option>` : ''}
              ${directory.map((p) => `<option value="${esc(p.name)}" ${p.name === t.owner ? 'selected' : ''}>${esc(p.name)}${p.role ? ` · ${esc(p.role)}` : ''}</option>`).join('')}
            </select></div>
            ${directory.length ? '' : '<small class="muted">برای انتخاب مسئول، افراد را در تنظیمات تعریف کنید.</small>'}
          </dd></div>
          <div><dt>اولویت</dt><dd><div class="select"><select id="task-priority" aria-label="اولویت">${Object.entries(PRIORITY).map(([key, label]) => `<option value="${key}" ${key === t.priority ? 'selected' : ''}>${label}</option>`).join('')}</select></div></dd></div>
          <div><dt>موعد</dt><dd><input type="date" id="task-deadline" value="${t.deadline ?? ''}" aria-label="موعد"><small class="${isOverdue(t) ? 'urgent' : 'muted'}">${t.deadline ? fmtJalali(t.deadline) : 'تعیین نشده؛ برای نمایش در تقویم یک تاریخ بگذارید'}</small></dd></div>
          <div><dt>جلسه</dt><dd><a href="#/meeting/${t.meeting_id}" dir="auto">${esc(t.meeting_title)}</a><small class="muted">${t.meeting_held_on ? fmtJalali(t.meeting_held_on) : ''}</small></dd></div>
          <div><dt>Jira</dt><dd>${t.jira_key
            ? `<a class="jira-key" href="${esc(t.jira_link ?? '#')}" target="_blank" rel="noopener" dir="ltr">${esc(t.jira_key)}</a><small class="muted">این وظیفه قبلاً به Jira ارسال شده است.</small>`
            : `<a class="btn" href="#/jira?tasks=${t.id}">ارسال به Jira</a>`}</dd></div>
        </dl>
      </aside>
    </div>`;
  const update = guard(async (body) => {
    await api(`/tasks/${id}`, { method: 'PATCH', body });
    route();
  });
  $('#task-done').addEventListener('change', (e) => update({ done: e.target.checked }));
  $('#task-deadline').addEventListener('change', (e) => update({ deadline: e.target.value }));
  $('#task-owner').addEventListener('change', (e) => update({ owner: e.target.value }));
  $('#task-priority').addEventListener('change', (e) => update({ priority: e.target.value }));
  view.onclick = guard(async (e) => {
    const action = e.target.closest('[data-action]')?.dataset.action, tab = e.target.closest('[data-tab]')?.dataset.tab;
    if (action === 'edit' || action === 'cancel') {
      taskView.editing = action === 'edit' ? id : null;
      route();
    } else if (action === 'save') {
      await api(`/tasks/${id}`, { method: 'PATCH', body: { title: $('#edit-title').value, description: $('#edit-body').value } });
      taskView.editing = null;
      toast('تغییرات ذخیره شد.');
      route();
    } else if (tab) {
      const preview = tab === 'preview';
      $('#edit-body').hidden = preview;
      $('#edit-preview').hidden = !preview;
      if (preview) { $('#edit-preview').dir = textDir($('#edit-body').value); $('#edit-preview').innerHTML = markdown($('#edit-body').value); }
      document.querySelectorAll('[data-tab]').forEach((button) => button.setAttribute('aria-pressed', button.dataset.tab === tab));
    }
  });
}

/* ---------- Jira: choose where the tasks go, review them, send ---------- */

const lastJiraProject = () => { try { return localStorage.getItem('lastJiraProject') ?? ''; } catch { return ''; } };
const rememberJiraProject = (key) => { try { localStorage.setItem('lastJiraProject', key); } catch { /* just not remembered */ } };

/** Jira's own priority list has no fixed names, so match by name and fall back to the top, middle and bottom entries. */
function jiraPriorityFor(priorities, level) {
  const exact = priorities.find((p) => p.name.toLowerCase() === level);
  return (exact ?? priorities[{ high: 0, medium: Math.floor(priorities.length / 2), low: priorities.length - 1 }[level]])?.id ?? '';
}

/**
 * A Jira-style combobox. `load(query)` returns [{ value, id?, text, hint? }]; the list loads by itself when it opens and
 * narrows as you type. With `free`, whatever you type can be used as the value when Jira has no match for it.
 */
function picker({ load, none, free = false, value = null, onChange }) {
  const el = document.createElement('div');
  el.className = 'picker';
  el.innerHTML = `
    <button type="button" class="picker-btn" aria-haspopup="listbox" aria-expanded="false"></button>
    <div class="picker-pop" hidden><input class="picker-q" autocomplete="off" placeholder="جست‌وجو…" aria-label="جست‌وجو"><ul class="picker-list" role="listbox"></ul></div>`;
  const button = $('.picker-btn', el), pop = $('.picker-pop', el), query = $('.picker-q', el), list = $('.picker-list', el);
  let chosen = value, shown = [], latest = 0, timer;

  const paint = () => {
    button.innerHTML = chosen ? `${chosen.id ? `<b class="pk-id" dir="ltr">${esc(chosen.id)}</b>` : ''}<span class="pk-text" dir="auto">${esc(chosen.text)}</span>` : `<span class="pk-none">${esc(none)}</span>`;
  };
  const close = () => { pop.hidden = true; button.setAttribute('aria-expanded', 'false'); };
  const choose = (item) => { chosen = item; paint(); close(); button.focus(); onChange?.(chosen); };
  const render = (items, text, problem = '') => {
    const typed = free && text && !items.some((i) => i.value === text) ? [{ value: text, text: `استفاده از «${text}»` }] : [];
    shown = [null, ...items, ...typed];
    list.innerHTML = shown.map((item, i) => `<li><button type="button" role="option" data-i="${i}" aria-selected="${(item?.value ?? '') === (chosen?.value ?? '')}">${
      item ? `${item.id ? `<b class="pk-id" dir="ltr">${esc(item.id)}</b>` : ''}<span class="pk-text" dir="auto">${esc(item.text)}</span>${item.hint ? `<small class="pk-hint" dir="auto">${esc(item.hint)}</small>` : ''}`
           : `<span class="pk-none">${esc(none)}</span>`}</button></li>`).join('')
      + (problem ? `<li class="pk-note err">${esc(problem)}</li>` : items.length || typed.length ? '' : '<li class="pk-note">موردی پیدا نشد.</li>');
  };
  const search = async () => {
    const mine = ++latest, text = query.value.trim();
    try {
      const items = await load(text);
      if (mine === latest) render(items, text);
    } catch (e) {
      if (mine === latest) render([], text, e.message);
    }
  };

  button.addEventListener('click', () => {
    if (!pop.hidden) return close();
    document.dispatchEvent(new CustomEvent('picker-open', { detail: el }));
    pop.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    query.value = '';
    list.innerHTML = '<li class="pk-note">در حال بارگیری…</li>';
    search();
    query.focus();
  });
  query.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 250); });
  list.addEventListener('click', (e) => {
    const i = e.target.closest('[data-i]')?.dataset.i;
    if (i !== undefined) choose(shown[i]);
  });
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pop.hidden) { e.stopPropagation(); close(); button.focus(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const options = [query, ...list.querySelectorAll('[data-i]')], at = options.indexOf(document.activeElement);
      if (at < 0) return;
      e.preventDefault();
      options[Math.max(0, Math.min(options.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)))].focus();
    }
  });
  document.addEventListener('click', (e) => { if (!el.isConnected) return; if (!el.contains(e.target)) close(); });
  document.addEventListener('picker-open', (e) => { if (e.detail !== el) close(); });

  paint();
  el.chosen = () => chosen;
  el.set = (item) => { chosen = item; paint(); };
  el.disable = (off) => { button.disabled = off; };
  return el;
}

const issueItem = (i) => ({ value: i.key, id: i.key, text: i.summary, hint: [i.type, i.status].filter(Boolean).join(' · ') });
const userItem = (u) => ({ value: u.name, text: u.display, hint: u.name });
const matches = (item, text) => !text || `${item.id ?? ''} ${item.text}`.toLowerCase().includes(text.toLowerCase());

async function jiraView(_, params) {
  const ids = (params.get('tasks') ?? '').split(',').filter(Boolean);
  const meetingId = params.get('meeting');
  const [directory, tasks] = await Promise.all([
    api('/people'),
    meetingId ? api(`/meetings/${meetingId}`).then((m) => m.tasks.map((t) => ({ ...t, meeting_title: m.title }))) : Promise.all(ids.map((id) => api(`/tasks/${id}`))),
  ]);
  let projects, priorities;
  try {
    [projects, priorities] = await Promise.all([api('/jira/projects'), api('/jira/priorities')]);
  } catch (e) {
    view.innerHTML = `
      <a class="crumb" href="#/tasks">وظایف</a>
      <h1 class="title">ارسال به Jira</h1>
      <div class="notice"><b>اتصال به Jira برقرار نشد</b><p dir="auto">${esc(e.message)}</p><p class="muted">آدرس و ورود را در <a class="inline-link" href="#/settings">تنظیمات</a> بررسی کنید.</p></div>`;
    return;
  }

  const card = (t) => {
    if (t.jira_key) {
      return `<article class="send-card sent"><a class="task-text" href="#/task/${t.id}" dir="auto">${esc(t.title)}</a><span class="muted">قبلاً ارسال شده: <a class="jira-key" href="${esc(t.jira_link ?? '#')}" target="_blank" rel="noopener" dir="ltr">${esc(t.jira_key)}</a></span></article>`;
    }
    return `
      <article class="send-card" data-task="${t.id}" data-owner="${esc(t.owner ?? '')}">
        <header>
          <label class="check"><input type="checkbox" checked aria-label="ارسال این وظیفه"><span class="box" aria-hidden="true"></span></label>
          <a class="task-text" href="#/task/${t.id}" dir="auto">${esc(t.title)}</a>
          ${t.owner ? `<span class="muted send-owner" dir="auto">مسئول در جلسه: ${esc(t.owner)}</span>` : ''}
        </header>
        <div class="send-fields">
          <div class="field"><span class="field-label">نوع کار</span><div class="select"><select class="type" aria-label="نوع کار" disabled></select></div></div>
          <div class="field"><span class="field-label link-label">اپیک</span><div class="link-host"></div></div>
          <div class="field"><span class="field-label">مسئول در Jira</span><div class="assignee-host"></div></div>
          <div class="field"><span class="field-label">اولویت</span><div class="select"><select class="priority" aria-label="اولویت">${priorities.map((p) => `<option value="${esc(p.id)}" ${p.id === jiraPriorityFor(priorities, t.priority) ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div></div>
          <div class="field"><span class="field-label">موعد</span><input class="due" type="date" value="${t.deadline ?? ''}" aria-label="موعد"></div>
        </div>
        <p class="send-result" hidden></p>
      </article>`;
  };

  view.innerHTML = `
    <a class="crumb" href="#/tasks">وظایف</a>
    <h1 class="title">ارسال به Jira</h1>
    <p class="lead">پروژه را انتخاب کنید؛ نوع کار، اپیک و کار مادر از خود Jira بارگیری می‌شوند. هر وظیفه را می‌توانید جدا تنظیم کنید.</p>
    <form id="jira-form" class="form">
      <section class="send-target">
        <div class="field"><span class="field-label">پروژه</span><div id="j-project-host"></div></div>
        <div class="field"><span class="field-label">نوع کار برای همه</span><div class="select"><select id="j-type" aria-label="نوع کار برای همه" disabled><option>ابتدا پروژه را انتخاب کنید</option></select></div></div>
        <div class="field"><span class="field-label" id="j-link-label">اپیک برای همه</span><div id="j-link-host"></div></div>
      </section>
      <section>
        <h2 class="section-title">وظایف<small>${fa(tasks.filter((t) => !t.jira_key).length)} وظیفه آماده‌ی ارسال</small></h2>
        ${tasks.length ? `<div class="send-cards">${tasks.map(card).join('')}</div>` : '<p class="empty">وظیفه‌ای برای ارسال انتخاب نشده است.</p>'}
      </section>
      <div class="form-actions"><button class="btn primary" type="submit" disabled>ارسال به Jira</button><span class="muted" id="j-hint"></span></div>
    </form>`;

  const form = $('#jira-form'), submit = $('button[type=submit]', form), defaultType = $('#j-type');
  let meta = { issue_types: [], epics: [] };
  const project = () => projectPicker.chosen()?.value ?? '';
  const cards = () => [...form.querySelectorAll('.send-card[data-task]:not(.sent)')];
  const isSub = (select) => select.selectedOptions[0]?.dataset.subtask === 'true';
  const linkOf = (c) => $('.link-host .picker', c);

  const types = () => [...meta.issue_types.filter((t) => !t.subtask), ...meta.issue_types.filter((t) => t.subtask)].filter((t) => t.name.toLowerCase() !== 'epic');
  const typeOptions = () => types().map((t) => `<option value="${esc(t.id)}" data-subtask="${t.subtask}">${esc(t.name)}</option>`).join('');
  const preferredType = () => (types().find((t) => t.name.toLowerCase() === 'task') ?? types().find((t) => !t.subtask))?.id ?? '';

  /** Epics are already loaded with the project; parents of a sub-task are searched in Jira as you type. */
  const linkPicker = (sub, value, onChange) => picker({
    none: sub ? 'انتخاب کار مادر…' : 'بدون اپیک', value, onChange,
    load: sub ? async (text) => (await api(`/jira/issues?project=${encodeURIComponent(project())}&q=${encodeURIComponent(text)}`)).map(issueItem)
              : async (text) => meta.epics.map(issueItem).filter((i) => matches(i, text)),
  });
  const mountLink = (c, value = null) => {
    const sub = isSub($('.type', c));
    $('.link-label', c).textContent = sub ? 'کار مادر' : 'اپیک';
    $('.link-host', c).replaceChildren(linkPicker(sub, value, refresh));
  };
  const mountDefaultLink = () => {
    const sub = isSub(defaultType);
    $('#j-link-label').textContent = sub ? 'کار مادر برای همه' : 'اپیک برای همه';
    $('#j-link-host').replaceChildren(linkPicker(sub, null, (item) => {
      cards().filter((c) => isSub($('.type', c)) === sub).forEach((c) => mountLink(c, item));
      refresh();
    }));
  };

  const refresh = () => {
    const picked = cards().filter((c) => $('input[type=checkbox]', c).checked);
    const missing = !project() ? 'پروژه را انتخاب کنید.'
      : !picked.length ? 'حداقل یک وظیفه را انتخاب کنید.'
      : picked.some((c) => !$('.type', c).value) ? 'نوع کار را انتخاب کنید.'
      : picked.some((c) => isSub($('.type', c)) && !linkOf(c).chosen()) ? 'برای زیروظیفه‌ها «کار مادر» را انتخاب کنید.' : '';
    submit.disabled = !!missing;
    submit.textContent = picked.length ? `ارسال ${fa(picked.length)} وظیفه به Jira` : 'ارسال به Jira';
    $('#j-hint').textContent = missing;
  };

  const loadProject = guard(async () => {
    const key = project();
    meta = key ? await api(`/jira/projects/${encodeURIComponent(key)}/meta`) : { issue_types: [], epics: [] };
    if (key) rememberJiraProject(key);
    defaultType.innerHTML = typeOptions() || '<option value="">ابتدا پروژه را انتخاب کنید</option>';
    defaultType.value = preferredType();
    defaultType.disabled = !key;
    for (const c of cards()) {
      const select = $('.type', c);
      select.innerHTML = defaultType.innerHTML;
      select.value = defaultType.value;
      select.disabled = !key;
      mountLink(c);
    }
    mountDefaultLink();
    refresh();
  });

  const projectPicker = picker({
    none: 'انتخاب پروژه…',
    load: async (text) => projects.map((p) => ({ value: p.key, id: p.key, text: p.name })).filter((i) => matches(i, text)),
    onChange: loadProject,
  });
  $('#j-project-host').append(projectPicker);

  for (const c of cards()) {
    const username = directory.find((p) => p.name === c.dataset.owner)?.jira_username ?? '';
    c.querySelector('.assignee-host').append(picker({
      none: 'بدون مسئول', free: true, value: username ? { value: username, text: c.dataset.owner, hint: username } : null,
      load: async (text) => (await api(`/jira/users?project=${encodeURIComponent(project())}&q=${encodeURIComponent(text)}`)).map(userItem),
    }));
    $('.type', c).addEventListener('change', () => { mountLink(c); refresh(); });
    $('input[type=checkbox]', c).addEventListener('change', refresh);
  }
  defaultType.addEventListener('change', () => {
    for (const c of cards()) { $('.type', c).value = defaultType.value; mountLink(c); }
    mountDefaultLink();
    refresh();
  });

  form.addEventListener('submit', guard(async (e) => {
    e.preventDefault();
    const rows = cards().filter((c) => $('input[type=checkbox]', c).checked);
    submit.disabled = true;
    submit.textContent = 'در حال ارسال…';
    try {
      const { results } = await api('/jira/send', { method: 'POST', body: {
        project: project(),
        items: rows.map((c) => {
          const sub = isSub($('.type', c)), link = linkOf(c).chosen()?.value ?? '';
          return { task_id: Number(c.dataset.task), issue_type_id: $('.type', c).value, epic_key: sub ? '' : link, parent_key: sub ? link : '',
                   assignee: $('.assignee-host .picker', c).chosen()?.value ?? '', priority_id: $('.priority', c).value, due: $('.due', c).value };
        }),
      } });
      for (const result of results) {
        const c = form.querySelector(`.send-card[data-task="${result.task_id}"]`);
        const out = $('.send-result', c);
        out.hidden = false;
        out.className = `send-result ${result.key ? 'ok' : 'err'}`;
        out.innerHTML = result.key
          ? `<a class="jira-key" href="${esc(result.link ?? '#')}" target="_blank" rel="noopener" dir="ltr">${esc(result.key)}</a>${result.warning ? ` · ${esc(result.warning)}` : ' · ساخته شد'}`
          : esc(result.error);
        if (result.key) {
          c.classList.add('sent');
          c.querySelectorAll('input, select, .picker-btn').forEach((el) => { el.disabled = true; });
          $('input[type=checkbox]', c).checked = false;
        }
      }
      const failed = results.filter((r) => !r.key).length;
      toast(failed ? `${fa(results.length - failed)} وظیفه ارسال شد و ${fa(failed)} وظیفه خطا داد.` : 'همه‌ی وظایف به Jira ارسال شد.', failed ? 'err' : 'ok');
    } finally {
      refresh();
    }
  }));

  const remembered = projects.find((p) => p.key === lastJiraProject());
  if (remembered) { projectPicker.set({ value: remembered.key, id: remembered.key, text: remembered.name }); loadProject(); }
  refresh();
}

/* ---------- settings ---------- */

function peopleSection(host) {
  host.innerHTML = `
    <h2 class="section-title">افراد و نقش‌ها</h2>
    <p class="muted">افرادی که در جلسه‌ها حاضرند یا از آن‌ها نام برده می‌شود. یک نفر ممکن است در جلسه با لقب یا نام کوتاه صدا زده شود؛ آن نام‌ها را هم بنویسید تا مدل بداند همه یک نفرند و صورت‌جلسه و وظایف را به نام کامل ثبت کند.</p>
    <ul class="people-list" id="people-list"></ul>
    <form class="person-form" id="person-form">
      <input name="name" placeholder="نام کامل" aria-label="نام کامل" required>
      <input name="role" placeholder="نقش (اختیاری)" aria-label="نقش">
      <input name="aliases" placeholder="نام‌های دیگر، با ویرگول جدا کنید (اختیاری)" aria-label="نام‌های دیگر">
      <input name="jira_username" dir="ltr" placeholder="نام کاربری Jira (اختیاری)؛ برای تعیین مسئول در Jira" aria-label="نام کاربری Jira" autocomplete="off">
      <div class="actions"><button class="btn primary" type="submit">افزودن فرد</button><button class="link" type="button" id="person-cancel" hidden>انصراف</button></div>
    </form>`;
  const list = $('#people-list', host), form = $('#person-form', host), fields = form.elements;
  let directory = [], editing = null;

  const draw = () => {
    list.innerHTML = directory.length ? directory.map((p) => `
      <li data-id="${p.id}">
        <div class="who"><b dir="auto">${esc(p.name)}</b><span class="muted" dir="auto">${esc(p.role)}</span>${p.jira_username ? `<span class="jira-user" dir="ltr">Jira: ${esc(p.jira_username)}</span>` : ''}</div>
        <div class="aka">${p.aliases.map((a) => `<span class="chip" dir="auto">${esc(a)}</span>`).join('')}</div>
        <div class="item-actions">
          <button class="icon-btn" data-edit title="ویرایش" aria-label="ویرایش ${esc(p.name)}">${ICONS.edit}</button>
          <button class="icon-btn danger" data-delete title="حذف" aria-label="حذف ${esc(p.name)}">${ICONS.trash}</button>
        </div>
      </li>`).join('') : '<li class="empty">هنوز کسی تعریف نشده است.</li>';
  };
  const load = async () => { directory = await api('/people'); draw(); };
  const stopEditing = () => {
    editing = null;
    form.reset();
    $('button[type=submit]', form).textContent = 'افزودن فرد';
    $('#person-cancel', form).hidden = true;
  };

  list.addEventListener('click', guard(async (e) => {
    const row = e.target.closest('li[data-id]');
    if (!row) return;
    const person = directory.find((p) => p.id === Number(row.dataset.id));
    if (e.target.closest('[data-edit]')) {
      editing = person.id;
      fields.name.value = person.name; // form.name would be the form's own name attribute, hence form.elements
      fields.role.value = person.role;
      fields.aliases.value = person.aliases.join('، ');
      fields.jira_username.value = person.jira_username;
      $('button[type=submit]', form).textContent = 'ذخیره تغییرات';
      $('#person-cancel', form).hidden = false;
      fields.name.focus();
    } else if (e.target.closest('[data-delete]') && confirm(`«${person.name}» از فهرست حذف شود؟ جلسه‌ها و وظایف قبلی نامش را نگه می‌دارند.`)) {
      await api(`/people/${person.id}`, { method: 'DELETE' });
      if (editing === person.id) stopEditing();
      await load();
    }
  }));
  $('#person-cancel', form).addEventListener('click', stopEditing);
  form.addEventListener('submit', guard(async (e) => {
    e.preventDefault();
    const body = { name: fields.name.value, role: fields.role.value, jira_username: fields.jira_username.value, aliases: fields.aliases.value.split(/[,،\n]/).map((a) => a.trim()).filter(Boolean) };
    await api(editing ? `/people/${editing}` : '/people', { method: editing ? 'PUT' : 'POST', body });
    toast(editing ? 'تغییرات ذخیره شد.' : 'فرد اضافه شد.');
    stopEditing();
    await load();
  }));
  guard(load)();
}

async function settingsView() {
  const s = await api('/settings');
  view.innerHTML = `
    <h1 class="title">تنظیمات</h1>
    <p class="lead">افراد، اتصال به مدل زبانی و سرویس رونویسی، و Jira. مدل هر جلسه هنگام ثبت همان جلسه انتخاب می‌شود.</p>
    <div class="form"><section id="people"></section></div>
    <form id="settings" class="form">
      <section>
        <h2 class="section-title">مدل زبانی (LLM)</h2>
        <div class="setting">
          <label for="base_url">آدرس API<span class="hint">هر سرویس سازگار با OpenAI، مثلاً https://api.openai.com/v1 یا آدرس OpenRouter، Groq، Ollama و ...</span></label>
          <input id="base_url" name="base_url" dir="ltr" value="${esc(s.base_url)}">
        </div>
        <div class="setting">
          <label for="api_key">کلید API<span class="hint">فقط روی همین سیستم ذخیره می‌شود. اگر خالی بگذارید، کلید ذخیره‌شده می‌ماند.</span></label>
          <input id="api_key" name="api_key" type="password" dir="ltr" autocomplete="off" placeholder="${s.api_key_set ? '•••••••• ذخیره شده' : 'sk-…'}">
        </div>
        <div class="setting">
          <div class="label">تست مدل<span class="hint">فقط برای اطمینان از اینکه یک مدل در دسترس است. انتخاب مدل جلسه‌ها جداگانه و هنگام آپلود یا پایان ضبط انجام می‌شود.</span></div>
          <div>
            <div id="test-model"></div>
            <div class="inline">
              <button class="btn" type="button" id="test">تست مدل</button>
              <button class="link" type="button" id="reload">به‌روزرسانی فهرست مدل‌ها</button>
              <span class="result" id="result" role="status"></span>
            </div>
          </div>
        </div>
      </section>
      <section>
        <h2 class="section-title">اتصال به Jira</h2>
        <div class="setting">
          <label for="jira_url">آدرس Jira<span class="hint">آدرس پورتال، مثلاً https://jira.example.com</span></label>
          <input id="jira_url" name="jira_url" dir="ltr" value="${esc(s.jira_url)}" autocomplete="off">
        </div>
        <div class="setting">
          <label for="jira_user">نام کاربری<span class="hint">همان نام کاربری ورود به Jira. اگر با توکن شخصی (PAT) وصل می‌شوید خالی بگذارید.</span></label>
          <input id="jira_user" name="jira_user" dir="ltr" value="${esc(s.jira_user)}" autocomplete="off">
        </div>
        <div class="setting">
          <label for="jira_password">رمز عبور<span class="hint">فقط روی همین سیستم ذخیره می‌شود. اگر خالی بگذارید، مقدار ذخیره‌شده می‌ماند. برای PAT، توکن را اینجا بگذارید.</span></label>
          <input id="jira_password" name="jira_password" type="password" dir="ltr" autocomplete="new-password" placeholder="${s.jira_password_set ? '•••••••• ذخیره شده' : ''}">
        </div>
        <div class="setting">
          <div class="label">بررسی گواهی TLS<span class="hint">اگر Jira شرکت گواهی داخلی دارد و اتصال خطای گواهی می‌دهد، خاموش کنید.</span></div>
          <label class="check inline"><input type="checkbox" name="jira_verify_tls" ${s.jira_verify_tls ? 'checked' : ''}><span class="box" aria-hidden="true"></span>فعال</label>
        </div>
        <div class="setting">
          <div class="label">تست اتصال</div>
          <div class="inline" style="margin-top:0"><button class="btn" type="button" id="jira-test">تست اتصال به Jira</button><span class="result" id="jira-result" role="status"></span></div>
        </div>
      </section>
      <section>
        <h2 class="section-title">رونویسی صدا</h2>
        <div class="setting">
          <label for="language">زبان گفتار<span class="hint">به انگلیسی بنویسید، مثلاً Persian یا English.</span></label>
          <input id="language" name="language" dir="ltr" value="${esc(s.language)}">
        </div>
        <div class="setting">
          <label for="stt_base_url">آدرس API رونویسی<span class="hint">سرویس سازگار با OpenAI برای تبدیل گفتار به متن (/audio/transcriptions). اگر خالی بگذارید از همان آدرس و کلید مدل زبانی استفاده می‌شود. صدا روی سرور این سرویس پردازش می‌شود.</span></label>
          <input id="stt_base_url" name="stt_base_url" dir="ltr" value="${esc(s.stt_base_url)}" placeholder="https://api.openai.com/v1" autocomplete="off">
        </div>
        <div class="setting">
          <label for="stt_api_key">کلید API رونویسی<span class="hint">فقط وقتی آدرس جداگانه‌ای وارد کرده‌اید لازم است. اگر خالی بگذارید، کلید ذخیره‌شده می‌ماند.</span></label>
          <input id="stt_api_key" name="stt_api_key" type="password" dir="ltr" autocomplete="off" placeholder="${s.stt_api_key_set ? '•••••••• ذخیره شده' : ''}">
        </div>
        <div class="setting">
          <label for="stt_model">مدل رونویسی<span class="hint">مثلاً whisper-1، یا whisper-large-v3 در Groq.</span></label>
          <input id="stt_model" name="stt_model" dir="ltr" value="${esc(s.stt_model)}" autocomplete="off">
        </div>
      </section>
      <button class="btn primary" type="submit">ذخیره تنظیمات</button>
    </form>`;

  peopleSection($('#people'));
  const form = $('#settings'), result = $('#result'), picker = modelPicker($('#test-model'));
  const say = (text, ok) => { result.textContent = text; result.className = `result ${ok === undefined ? '' : ok ? 'ok' : 'err'}`; };
  const saveConnection = () => api('/settings', { method: 'PUT', body: { base_url: form.base_url.value, api_key: form.api_key.value } });
  const jiraSettings = () => ({ jira_url: form.jira_url.value, jira_user: form.jira_user.value, jira_password: form.jira_password.value, jira_verify_tls: form.jira_verify_tls.checked });
  $('#jira-test').addEventListener('click', async () => {
    const out = $('#jira-result');
    out.className = 'result';
    out.textContent = 'در حال اتصال…';
    try {
      await api('/settings', { method: 'PUT', body: jiraSettings() });
      form.jira_password.value = '';
      const r = await api('/jira/status');
      out.className = `result ${r.ok ? 'ok' : 'err'}`;
      out.textContent = r.ok ? `متصل شد · ${r.user} · Jira ${r.version}` : r.error;
    } catch (e) {
      out.className = 'result err';
      out.textContent = e.message;
    }
  });
  $('#reload').addEventListener('click', async () => {
    say('در حال دریافت…');
    await guard(saveConnection)();
    await picker.reload();
    say('');
  });
  $('#test').addEventListener('click', async () => {
    if (!picker.value) return say('ابتدا یک مدل انتخاب کنید.', false);
    say('در حال تست…');
    try {
      await saveConnection();
      const r = await api('/settings/test', { method: 'POST', body: { model: picker.value } });
      say(r.ok ? `مدل فعال است · ${fa((r.latency_ms / 1000).toFixed(1))} ثانیه` : r.error, r.ok);
    } catch (e) {
      say(e.message, false);
    }
  });
  form.addEventListener('submit', guard(async (e) => {
    e.preventDefault();
    await api('/settings', { method: 'PUT', body: { ...Object.fromEntries(new FormData(form)), jira_verify_tls: form.jira_verify_tls.checked } });
    form.api_key.value = '';
    form.stt_api_key.value = '';
    form.jira_password.value = '';
    toast('تنظیمات ذخیره شد.');
  }));
}

/* ---------- in-person recording: the microphone of this computer ---------- */

// Audio is streamed to the server in slices while it is recorded, so a crash, a closed tab or a sleeping laptop
// loses at most the last few seconds; the server processes whatever it received if the recording is abandoned.
const CHUNK_MS = 10000;
const HEARTBEAT_MS = 20000; // keeps a paused recording alive on the server
const QUIET_MS = 15000;
const QUIET_LEVEL = 0.015;
const rec = { phase: 'idle' }; // idle | recording | paused | uploading | review
const recorderBar = $('#recorder');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clock = (ms) => {
  const s = Math.floor(ms / 1000);
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, '0')).join(':');
};
const elapsed = () => rec.elapsedBefore + (rec.since ? performance.now() - rec.since : 0);
const micError = (e) => ({
  NotAllowedError: 'دسترسی به میکروفن داده نشد. روی آیکون قفل کنار آدرس بزنید و میکروفن را مجاز کنید.',
  NotFoundError: 'میکروفنی پیدا نشد.',
}[e.name] ?? `باز کردن میکروفن ناموفق بود: ${e.message}`);

async function sendChunk(blob) {
  const response = await fetch(`/api/recordings/${rec.id}/chunks?offset=${rec.sent}`, { method: 'PUT', body: blob });
  if (!response.ok) throw Object.assign(new Error((await response.json().catch(() => ({}))).detail ?? response.statusText), { status: response.status });
}

async function pump() {
  if (rec.pumping) return;
  rec.pumping = true;
  while (rec.queue?.length) {
    const blob = rec.queue[0];
    try {
      await sendChunk(blob);
      rec.sent += blob.size;
      rec.queue.shift();
      rec.offline = false;
    } catch (e) {
      if ([404, 409].includes(e.status)) { rec.pumping = false; return abandonRecording(e.message); }
      rec.offline = true; // network or server hiccup: recording continues, the chunk is retried
      await sleep(2000);
    }
  }
  rec.pumping = false;
}

function level() {
  rec.analyser.getByteTimeDomainData(rec.samples);
  const power = rec.samples.reduce((sum, v) => sum + ((v - 128) / 128) ** 2, 0) / rec.samples.length;
  return Math.min(1, Math.sqrt(power) * 5);
}

function tick() {
  const now = level();
  rec.quietMs = rec.phase === 'recording' && now < QUIET_LEVEL ? rec.quietMs + 200 : 0;
  $('.rec-time', recorderBar).textContent = clock(elapsed());
  $('.meter i', recorderBar).style.width = `${now * 100}%`;
  $('.rec-warn', recorderBar).textContent = rec.offline
    ? 'اتصال به سرور قطع است؛ ضبط ادامه دارد و ارسال دوباره تلاش می‌شود.'
    : rec.quietMs > QUIET_MS ? 'صدایی از میکروفن دریافت نمی‌شود؛ میکروفن را بررسی کنید.' : '';
}

function renderRecorder() {
  recorderBar.hidden = rec.phase === 'idle';
  recorderBar.className = `recorder ${rec.phase}`;
  if (rec.phase === 'review') {
    recorderBar.innerHTML = `
      <div class="recorder-inner">
        <b>ضبط تمام شد · <span dir="ltr">${clock(rec.elapsedBefore)}</span></b>
        <div class="rec-form">
          <input id="rec-title" placeholder="عنوان جلسه (اختیاری)" aria-label="عنوان جلسه">
        </div>
        <div class="rec-people"><span class="muted">شرکت‌کنندگان</span><div id="rec-people"></div></div>
        <div class="rec-model"><span class="muted">مدل پردازش</span><div id="rec-model"></div></div>
        <div class="rec-actions"><button class="link danger" data-rec="discard">دور انداختن</button><button class="btn primary" data-rec="finish">پردازش جلسه</button></div>
      </div>`;
    rec.picker = modelPicker($('#rec-model'));
    rec.attendees = participantPicker($('#rec-people'));
    $('#rec-title').focus();
  } else if (rec.phase !== 'idle') {
    const sending = rec.phase === 'uploading';
    recorderBar.innerHTML = `
      <div class="recorder-inner">
        <i class="rec-dot"></i><b>${rec.phase === 'paused' ? 'ضبط متوقف شده' : sending ? 'در حال ارسال آخرین بخش…' : 'در حال ضبط'}</b>
        <span class="rec-time" dir="ltr">${clock(elapsed())}</span>
        <span class="meter" aria-hidden="true"><i></i></span>
        <span class="rec-warn" role="status"></span>
        <div class="rec-actions">
          <button class="btn" data-rec="pause" ${sending ? 'disabled' : ''}>${rec.phase === 'paused' ? 'ادامه' : 'مکث'}</button>
          <button class="btn primary" data-rec="stop" ${sending ? 'disabled' : ''}>پایان جلسه</button>
        </div>
      </div>`;
  }
}

function resetRecorder() {
  clearInterval(rec.ticker);
  clearInterval(rec.heartbeat);
  rec.stream?.getTracks().forEach((track) => track.stop());
  rec.context?.close();
  window.removeEventListener('beforeunload', warnBeforeUnload);
  document.body.classList.remove('recording');
  Object.keys(rec).forEach((key) => delete rec[key]);
  rec.phase = 'idle';
  renderRecorder();
}

function abandonRecording(message) {
  resetRecorder();
  toast(`ضبط متوقف شد: ${message}`, 'err');
}

async function startRecording() {
  if (rec.phase !== 'idle') return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return toast('این مرورگر ضبط صدا را پشتیبانی نمی‌کند.', 'err');
  let stream;
  try {
    // Room audio, not a call: no echo cancellation or noise suppression, which can swallow distant speakers.
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
  } catch (e) {
    return toast(micError(e), 'err');
  }
  const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'].find((type) => MediaRecorder.isTypeSupported(type)) ?? '';
  let id;
  try {
    ({ id } = await api('/recordings', { method: 'POST', body: { mime: mimeType || 'audio/webm', held_on: todayIso() } }));
  } catch (e) {
    stream.getTracks().forEach((track) => track.stop());
    return toast(e.message, 'err');
  }
  const context = new AudioContext();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  Object.assign(rec, { id, stream, context, analyser, samples: new Uint8Array(analyser.fftSize), queue: [], sent: 0, quietMs: 0, elapsedBefore: 0, since: performance.now(), phase: 'recording' });
  rec.recorder = new MediaRecorder(stream, { ...(mimeType && { mimeType }), audioBitsPerSecond: 64000 });
  rec.recorder.ondataavailable = (e) => { if (e.data.size) { rec.queue.push(e.data); pump(); } };
  rec.recorder.start(CHUNK_MS);
  rec.ticker = setInterval(tick, 200);
  rec.heartbeat = setInterval(() => { if (!rec.pumping && !rec.queue.length) sendChunk(new Blob([])).catch(() => {}); }, HEARTBEAT_MS);
  window.addEventListener('beforeunload', warnBeforeUnload);
  document.body.classList.add('recording');
  renderRecorder();
}

async function stopRecording() {
  rec.elapsedBefore = elapsed();
  rec.since = null;
  rec.phase = 'uploading';
  renderRecorder();
  const stopped = new Promise((resolve) => rec.recorder.addEventListener('stop', resolve, { once: true }));
  rec.recorder.stop(); // delivers the last slice, then fires "stop"
  await stopped;
  rec.stream.getTracks().forEach((track) => track.stop());
  clearInterval(rec.ticker);
  while (rec.phase === 'uploading' && (rec.queue.length || rec.pumping)) { // wait until the server has every byte
    pump();
    recorderBar.querySelector('.rec-warn').textContent = rec.offline ? 'اتصال به سرور قطع است؛ ارسال دوباره تلاش می‌شود…' : '';
    await sleep(300);
  }
  if (rec.phase === 'uploading') { rec.phase = 'review'; renderRecorder(); }
}

function togglePause() {
  if (rec.phase === 'recording') {
    rec.recorder.requestData(); // flush what exists so far, then pause
    rec.recorder.pause();
    rec.elapsedBefore = elapsed();
    rec.since = null;
    rec.phase = 'paused';
  } else {
    rec.recorder.resume();
    rec.since = performance.now();
    rec.phase = 'recording';
  }
  renderRecorder();
}

const recorderActions = {
  pause: togglePause,
  stop: stopRecording,
  async finish() {
    const id = rec.id, model = rec.picker.value;
    if (!model) return toast('یک مدل برای پردازش این جلسه انتخاب کنید.', 'err');
    rememberModel(model);
    try {
      await api(`/recordings/${id}/finish`, { method: 'POST', body: { title: $('#rec-title').value, participants: rec.attendees.value, model } });
    } catch (e) {
      resetRecorder();
      return toast(e.message, 'err');
    }
    resetRecorder();
    toast('ضبط ذخیره شد و پردازش شروع شد.');
    location.hash = `#/meeting/${id}`;
  },
  async discard() {
    if (!confirm('این ضبط برای همیشه دور انداخته شود؟')) return;
    await api(`/meetings/${rec.id}`, { method: 'DELETE' });
    resetRecorder();
    toast('ضبط دور انداخته شد.');
  },
};
recorderBar.addEventListener('click', guard(async (e) => recorderActions[e.target.closest('[data-rec]')?.dataset.rec]?.()));
$('#rec-start').addEventListener('click', guard(startRecording));

/* ---------- router ---------- */

let timer;
const routes = { '': calendarView, jira: jiraView, new: newMeetingView, meetings: meetingsView, meeting: meetingView, tasks: tasksView, task: taskView, settings: settingsView };
const NAV = { '': 'calendar', jira: 'tasks', new: 'new', meetings: 'meetings', meeting: 'meetings', tasks: 'tasks', task: 'tasks', settings: 'settings' };

function route() {
  clearTimeout(timer);
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  const [page, arg] = path.split('/');
  const params = new URLSearchParams(query);
  if (page !== 'task') taskView.editing = null; // an unfinished edit is dropped when you leave the task
  document.querySelectorAll('[data-page]').forEach((a) => {
    a.dataset.page === (NAV[page] ?? 'calendar') ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
  });
  view.onclick = null;
  onTaskChange = null;
  return guard(routes[page] ?? calendarView)(arg, params);
}

view.addEventListener('change', guard(async (e) => {
  const id = e.target.dataset.task;
  if (!id) return;
  await api(`/tasks/${id}`, { method: 'PATCH', body: { done: e.target.checked } });
  e.target.closest('.task').classList.toggle('done', e.target.checked);
  onTaskChange?.(Number(id), e.target.checked);
}));
view.addEventListener('click', guard(async (e) => {
  const button = e.target.closest('[data-delete-task]');
  if (!button) return;
  const id = Number(button.dataset.deleteTask);
  const { jira_key: key } = await api(`/tasks/${id}`, { method: 'DELETE' });
  if (location.hash.startsWith('#/task/')) location.hash = '#/tasks'; // the page of a deleted task has nothing left to show
  else route();
  toast(key ? `وظیفه حذف شد؛ تیکت ${key} در Jira می‌ماند.` : 'وظیفه حذف شد.', 'ok', {
    label: 'بازگردانی',
    run: async () => { await api(`/tasks/${id}/restore`, { method: 'POST' }); route(); },
  });
}));
window.addEventListener('hashchange', route);
route();
