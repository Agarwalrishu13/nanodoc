/* nanoDoc — the whole front end, no build step.
 *
 * Three ideas hold this together:
 *   1. Finding comes before writing. The paragraphs are on screen before the
 *      answer arrives, so the reader sees where it came from first.
 *   2. Numbers in an answer become buttons. "(page 4)" is clickable, and opens
 *      the page with the matched words highlighted.
 *   3. Nothing is rendered as HTML from the server. Text is placed with
 *      textContent and matches are wrapped in elements, so a document that
 *      contains angle brackets cannot turn into markup.
 */

'use strict';

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function toast(message, kind) {
  const element = node('div', 'toast' + (kind ? ' ' + kind : ''), message);
  $('toasts').appendChild(element);
  setTimeout(() => element.remove(), kind === 'bad' ? 6500 : 4200);
}

const api = {
  async get(path) {
    const response = await fetch(path);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'That did not work.');
    return data;
  },
  async post(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'That did not work.');
    return data;
  },
  async del(path) {
    const response = await fetch(path, { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'That did not work.');
    return data;
  },
};

/** Read a Server-Sent Events stream from a POST request. */
async function streamEvents(path, body, onEvent) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || 'That did not work.');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') return;
        try {
          onEvent(JSON.parse(payload));
        } catch (error) {
          /* a half-written line is not worth interrupting the answer for */
        }
      }
    }
  }
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Put text into `element`, wrapping any of `terms` in <mark>. */
function highlightInto(element, text, terms) {
  element.textContent = '';
  const words = (terms || []).filter((term) => term && term.length > 2).map(escapeRegExp);
  if (!words.length) {
    element.textContent = text;
    return;
  }
  const pattern = new RegExp('\\b(?:' + words.join('|') + ')\\w*', 'gi');
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) element.appendChild(document.createTextNode(text.slice(last, match.index)));
    element.appendChild(node('mark', null, match[0]));
    last = pattern.lastIndex;
    if (pattern.lastIndex === match.index) pattern.lastIndex += 1;
  }
  if (last < text.length) element.appendChild(document.createTextNode(text.slice(last)));
}

function fileEmoji(kind) {
  const text = (kind || '').toLowerCase();
  if (text.includes('pdf')) return '📕';
  if (text.includes('word')) return '📘';
  if (text.includes('spreadsheet')) return '📗';
  if (text.includes('web')) return '🌐';
  return '📄';
}

function commas(number) {
  return (number || 0).toLocaleString();
}

// ---------------------------------------------------------------- state
const state = {
  documents: [],       // what is in the library (no page text)
  engine: null,        // what the server found on this computer
  open: null,          // the document currently open, with its text
  about: null,         // keywords and opening lines for it
  turns: [],           // the conversation about it
  busy: false,
};

// ------------------------------------------------------------------ boot
async function refresh() {
  try {
    const data = await api.get('/api/state');
    state.documents = data.documents || [];
    state.engine = data.engine;
    renderLibrary();
    renderEngineChip();
    if (state.open && !state.documents.some((item) => item.id === state.open.id)) closeDocument();
  } catch (error) {
    toast('The app is not answering: ' + error.message, 'bad');
  }
}

function renderEngineChip() {
  const engine = state.engine || {};
  const dot = $('engine-dot');
  if (engine.ready) {
    dot.className = 'dot ok';
    $('engine-name').textContent = engine.engine_name;
    $('engine-sub').textContent = engine.model || 'ready';
  } else {
    dot.className = 'dot off';
    $('engine-name').textContent = 'No AI engine running';
    $('engine-sub').textContent = 'I can still find things';
  }
}

function renderLibrary() {
  const list = $('doc-list');
  list.textContent = '';
  $('doc-list-empty').hidden = state.documents.length > 0;

  for (const entry of state.documents) {
    const row = node('div', 'doc-row');
    const item = node('button', 'doc-item' + (state.open && state.open.id === entry.id ? ' active' : ''));
    // A stable hook for anything that needs to point at one document, and the
    // way the active row is tracked without rebuilding the whole list.
    item.dataset.docId = entry.id;
    item.appendChild(node('span', 'doc-icon', fileEmoji(entry.kind)));

    const body = node('span', 'doc-body');
    body.appendChild(node('span', 'doc-name', entry.name));
    body.appendChild(node('span', 'doc-meta',
      [entry.kind, commas(entry.pages) + ' ' + (entry.unit || 'page') + (entry.pages === 1 ? '' : 's'),
       commas(entry.words) + ' words'].join(' · ')));
    item.appendChild(body);
    item.addEventListener('click', () => openDocument(entry.id));
    row.appendChild(item);

    const remove = node('button', 'doc-remove', '✕');
    remove.title = 'Remove from this list (the file on your computer is not touched)';
    remove.setAttribute('aria-label', 'Remove ' + entry.name + ' from the list');
    remove.addEventListener('click', async () => {
      try {
        await api.del('/api/doc/' + entry.id);
        if (state.open && state.open.id === entry.id) closeDocument();
        await refresh();
        toast('Removed from the list. Your file was not touched.');
      } catch (error) {
        toast(error.message, 'bad');
      }
    });
    row.appendChild(remove);

    list.appendChild(row);
  }
}

// -------------------------------------------------------------- uploads
let dropDepth = 0;

function showDropOverlay(show) {
  $('drop-overlay').hidden = !show;
}

function wireDrops() {
  window.addEventListener('dragenter', (event) => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes('Files')) return;
    event.preventDefault();
    dropDepth += 1;
    showDropOverlay(true);
    $('dropzone').classList.add('over');
  });
  window.addEventListener('dragover', (event) => event.preventDefault());
  window.addEventListener('dragleave', () => {
    dropDepth = Math.max(0, dropDepth - 1);
    if (dropDepth === 0) {
      showDropOverlay(false);
      $('dropzone').classList.remove('over');
    }
  });
  window.addEventListener('drop', (event) => {
    event.preventDefault();
    dropDepth = 0;
    showDropOverlay(false);
    $('dropzone').classList.remove('over');
    const files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) ingest(files.length === 1 ? files[0] : files);
  });
}

/** Add one or more files, 8 MB at a time, reporting progress as they arrive. */
async function ingest(input) {
  if (state.busy) {
    toast('One at a time, please — a file is already arriving.');
    return;
  }
  const files = input instanceof FileList ? Array.from(input) : [input];
  state.busy = true;

  const card = node('div', 'progress-card');
  const label = node('div', 'progress-label');
  const name = node('span', null, '');
  const percent = node('span', null, '0%');
  label.appendChild(name);
  label.appendChild(percent);
  const track = node('div', 'progress-track');
  const fill = node('div', 'progress-fill');
  track.appendChild(fill);
  card.appendChild(label);
  card.appendChild(track);

  const host = state.open ? $('doc-notices') : $('read-notice');
  host.textContent = '';
  host.appendChild(card);

  try {
    for (const file of files) {
      name.textContent = 'Reading “' + file.name + '”…';
      fill.style.width = '0%';
      percent.textContent = '0%';

      const start = await api.post('/api/upload/start', { name: file.name });
      let offset = 0;
      while (offset < file.size) {
        const slice = file.slice(offset, offset + start.chunk_bytes);
        const response = await fetch(
          '/api/upload/chunk?id=' + encodeURIComponent(start.id) + '&offset=' + offset,
          { method: 'POST', body: slice, headers: { 'Content-Type': 'application/octet-stream' } }
        );
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || 'The file stopped arriving.');
        }
        offset += slice.size;
        const done = Math.round((offset / file.size) * 100);
        fill.style.width = done + '%';
        percent.textContent = done + '%';
      }

      name.textContent = 'Reading the text out of “' + file.name + '”…';
      percent.textContent = '';
      fill.style.width = '100%';

      try {
        const result = await api.post('/api/upload/finish', { id: start.id });
        card.remove();
        await refresh();
        await openDocument(result.document.id);
        toast(result.message, 'good');
      } catch (error) {
        // A file we cannot read is a normal, expected outcome — a scan, or a
        // format we do not handle — so it gets a full explanation rather than
        // a toast that disappears.
        card.remove();
        showReadNotice(file.name, error.message);
      }
    }
  } catch (error) {
    card.remove();
    toast(error.message, 'bad');
  } finally {
    state.busy = false;
  }
}

function showReadNotice(fileName, message) {
  const host = $('read-notice');
  host.textContent = '';
  const notice = node('div', 'notice warn');
  notice.appendChild(node('strong', null, 'I could not read “' + fileName + '”. '));
  notice.appendChild(document.createTextNode(message));
  const hint = node('p', null, '');
  hint.style.margin = '10px 0 0';
  hint.textContent = 'Nothing was added to your list, and the file itself was not changed. ';
  notice.appendChild(hint);
  host.appendChild(notice);
}

/** Move the highlight to one row without throwing the list away and rebuilding it. */
function markActive(id) {
  for (const item of document.querySelectorAll('.doc-item')) {
    item.classList.toggle('active', item.dataset.docId === id);
  }
}

// ------------------------------------------------------------- documents
async function openDocument(id) {
  try {
    const data = await api.get('/api/doc/' + id);
    state.open = { ...data.document, id };
    state.about = data.about;
    state.turns = [];
    markActive(id);
    renderDocument();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function closeDocument() {
  state.open = null;
  state.about = null;
  state.turns = [];
  $('doc-view').hidden = true;
  $('empty-view').hidden = false;
  renderLibrary();
}

function renderDocument() {
  const doc = state.open;
  if (!doc) return;
  $('empty-view').hidden = true;
  $('doc-view').hidden = false;

  // The heading is the file they dropped, because that is the name they know.
  // A PDF often carries its own idea of what it is called, which can be a
  // different person's name from a reused template — worth showing, never
  // worth replacing the file name with.
  $('doc-name').textContent = doc.name;
  $('doc-name').title = doc.title ? 'The document calls itself “' + doc.title + '”' : doc.name;

  const facts = $('doc-facts');
  facts.textContent = '';
  facts.appendChild(node('span', 'fact accent', doc.kind));
  facts.appendChild(node('span', 'fact',
    commas(doc.pages) + ' ' + (doc.unit || 'page') + (doc.pages === 1 ? '' : 's')));
  facts.appendChild(node('span', 'fact', commas(doc.words) + ' words'));
  if (doc.note) facts.appendChild(node('span', 'fact', doc.note));
  if (doc.title && doc.title !== doc.name) {
    facts.appendChild(node('span', 'fact', 'titled “' + doc.title + '”'));
  }

  const notices = $('doc-notices');
  notices.textContent = '';

  const about = $('doc-about');
  about.textContent = '';
  if (state.about && state.about.keywords && state.about.keywords.length) {
    const card = node('div', 'notice');
    card.appendChild(node('strong', null, 'It looks like this document is mostly about: '));
    card.appendChild(document.createTextNode(state.about.keywords.slice(0, 10).join(', ') + '.'));
    about.appendChild(card);
  }

  renderChips();
  renderTurns();
}

const GENERIC_QUESTIONS = [
  'What is this document about?',
  'What are the most important points?',
  'Are there any dates or deadlines?',
  'What does it ask of me?',
];

function renderChips() {
  const chips = $('chips');
  chips.textContent = '';
  const questions = ['What is this document about?'];
  for (const keyword of (state.about && state.about.keywords) || []) {
    if (questions.length >= 4) break;
    if (keyword.length < 4) continue;
    questions.push('What does it say about “' + keyword + '”?');
  }
  for (const question of GENERIC_QUESTIONS) {
    if (questions.length >= 5) break;
    if (!questions.includes(question)) questions.push(question);
  }

  for (const question of questions.slice(0, 5)) {
    const chip = node('button', 'chip', question);
    chip.addEventListener('click', () => {
      $('question').value = question;
      ask();
    });
    chips.appendChild(chip);
  }
}

// --------------------------------------------------------------- asking
function ask() {
  const box = $('question');
  const question = box.value.trim();
  if (!question || !state.open) return;
  box.value = '';
  autosize(box);

  const summary = question.toLowerCase().startsWith('what is this document about');
  const turn = {
    question,
    summary,
    hits: [],
    confidence: null,
    answer: '',
    notes: [],
    engine: '',
    streaming: true,
    done: false,
    failed: false,
  };
  state.turns.push(turn);
  renderTurns();
  $('export-button').hidden = false;

  const path = summary ? '/api/summary' : '/api/ask';
  streamEvents(path, { id: state.open.id, question }, (event) => handleEvent(turn, event))
    .then(() => {
      turn.streaming = false;
      turn.done = true;
      renderTurns();
    })
    .catch((error) => {
      turn.streaming = false;
      turn.done = true;
      turn.failed = true;
      turn.notes.push({ kind: 'bad', message: error.message });
      renderTurns();
    });
}

const answers = new Map();   // turn index → the element its text streams into

function handleEvent(turn, event) {
  switch (event.type) {
    case 'found':
      turn.hits = event.hits || [];
      turn.confidence = event.confidence;
      renderTurns();
      break;
    case 'engine':
      turn.engine = event.name + ' · ' + event.model;
      renderTurns();
      break;
    case 'answer': {
      turn.answer += event.text;
      const element = answers.get(state.turns.indexOf(turn));
      if (element) {
        element.textContent = turn.answer;
      } else {
        renderTurns();
      }
      break;
    }
    case 'note':
      turn.notes.push({ kind: 'warn', message: event.message });
      renderTurns();
      break;
    case 'unsupported':
      // The model wrote something the document does not say. That is worth
      // saying loudly: everything below it is still true.
      turn.notes.push({ kind: 'bad', message: event.message });
      renderTurns();
      break;
    case 'no_engine':
      turn.notes.push({ kind: 'warn', message: event.message, offerEngine: true });
      renderTurns();
      break;
    default:
      break;
  }
}

function renderTurns() {
  const host = $('turns');
  host.textContent = '';
  answers.clear();

  state.turns.forEach((turn, index) => {
    const card = node('div', 'turn');
    card.appendChild(node('div', 'question-line', turn.question));

    // 1. The honest bit, before the answer: did we actually find it?
    if (turn.confidence && !turn.summary && turn.hits.length) {
      if (!turn.confidence.found) {
        const notice = node('div', 'notice warn');
        notice.appendChild(node('strong', null, 'I am not sure this is answered in your document. '));
        notice.appendChild(document.createTextNode(
          'These are the closest parts I could find. Check them yourself before relying on them.'));
        card.appendChild(notice);
      }
    }
    if (turn.confidence && !turn.summary && !turn.hits.length && turn.done) {
      const notice = node('div', 'notice warn', 'Nothing in this document matches that question.');
      card.appendChild(notice);
    }

    for (const note of turn.notes) {
      const notice = node('div', 'notice ' + (note.kind || ''));
      notice.appendChild(document.createTextNode(note.message));
      if (note.offerEngine) {
        const button = node('button', 'button small', 'Set up an engine');
        button.style.marginTop = '11px';
        button.addEventListener('click', openEngineModal);
        notice.appendChild(node('div', null, '')).appendChild(button);
      }
      card.appendChild(notice);
    }

    // 2. The answer itself.
    if (turn.answer || turn.streaming) {
      const answer = node('div', 'answer' + (turn.streaming ? ' streaming' : ''));
      if (turn.streaming) {
        answer.textContent = turn.answer;
        answers.set(index, answer);
      } else {
        renderAnswer(answer, turn.answer);
      }
      card.appendChild(answer);
      if (turn.engine) card.appendChild(node('div', 'answer-meta', 'written by ' + turn.engine + ', on this computer'));
    } else if (turn.streaming) {
      const thinking = node('div', 'thinking');
      thinking.appendChild(node('span', 'spinner'));
      thinking.appendChild(document.createTextNode('Reading your document…'));
      card.appendChild(thinking);
    }

    // 3. Where it came from.
    if (turn.hits.length) {
      const sources = node('div', 'sources');
      sources.appendChild(node('h2', null, turn.hits.length === 1 ? 'Where this came from' : 'Where this came from'));
      turn.hits.forEach((hit, position) => sources.appendChild(sourceCard(hit, position, turn)));
      card.appendChild(sources);
    }

    host.appendChild(card);
  });
}

/** Turn "(page 4)" into a button that opens page 4. */
function renderAnswer(element, text) {
  element.textContent = '';
  const refusal = /^i could not find that in your document\.?$/i.test(text.trim());
  if (refusal) {
    element.className = 'answer notice warn';
    element.textContent = text;
    return;
  }
  const pattern = /\((page|part|section|sheet)\s+([^)"]{1,60})\)/gi;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) element.appendChild(document.createTextNode(text.slice(last, match.index)));
    const chip = node('button', 'cite', match[0]);
    chip.title = 'Read ' + match[1].toLowerCase() + ' ' + match[2];
    chip.addEventListener('click', () => openPageByLabel(match[2]));
    element.appendChild(chip);
    last = pattern.lastIndex;
  }
  if (last < text.length) element.appendChild(document.createTextNode(text.slice(last)));
}

function sourceCard(hit, position, turn) {
  const card = node('div', 'source');
  card.appendChild(node('div', 'source-num', String(position + 1)));

  const body = node('div', 'source-body');
  body.appendChild(node('div', 'source-label', hit.label));
  const text = node('div', 'source-text');
  const terms = (hit.matched && hit.matched.length ? hit.matched : (turn.question || '').split(/\s+/));
  highlightInto(text, hit.snippet || hit.text, terms);
  body.appendChild(text);

  const actions = node('div', 'source-actions');
  const read = node('button', 'button small ghost', 'Read ' + hit.label);
  read.addEventListener('click', () => openPage(hit.number, terms));
  actions.appendChild(read);
  body.appendChild(actions);

  const bar = node('div', 'score-bar');
  const fill = node('div', 'score-fill');
  fill.style.width = Math.min(100, Math.max(6, Math.round((hit.score / 8) * 100))) + '%';
  fill.title = 'How closely this matched your question';
  bar.appendChild(fill);
  body.appendChild(bar);

  card.appendChild(body);
  return card;
}

// --------------------------------------------------------------- reader
function openPageByLabel(label) {
  const wanted = String(label).trim().replace(/^“|”$/g, '').toLowerCase();
  const pages = (state.open && state.open.full_pages) || [];
  const numeric = /^\d+$/.test(wanted) ? parseInt(wanted, 10) : null;
  const page = pages.find((item) => numeric && item.number === numeric)
    || pages.find((item) => (item.label || '').toLowerCase().includes(wanted))
    || pages[0];
  if (page) openPage(page.number, []);
}

function openPage(number, terms) {
  const pages = (state.open && state.open.full_pages) || [];
  if (!pages.length) return;
  const page = pages.find((item) => item.number === number) || pages[0];

  $('reader-title').textContent = page.label + '  ·  ' + state.open.name;
  const select = $('reader-select');
  select.textContent = '';
  for (const item of pages) {
    const option = node('option', null, item.label);
    option.value = String(item.number);
    if (item.number === page.number) option.selected = true;
    select.appendChild(option);
  }
  highlightInto($('reader-text'), page.text, terms || []);

  $('reader').hidden = false;
  $('scrim').hidden = false;
  document.querySelector('.reader-body').scrollTop = 0;
}

function closeReader() {
  $('reader').hidden = true;
  $('scrim').hidden = true;
}

// --------------------------------------------------------- engine modal
function openEngineModal() {
  const engine = state.engine || {};
  const list = $('engine-list');
  list.textContent = '';

  for (const item of engine.engines || []) {
    const row = node('div', 'engine-row');
    row.appendChild(node('span', 'dot ' + (item.ok && item.models.length ? 'ok' : 'off')));
    const body = node('div', 'model-body');
    body.appendChild(node('div', 'engine-row-name', item.name));
    let detail = item.why || '';
    if (item.ok && item.models.length) {
      detail = item.models.length + ' model' + (item.models.length === 1 ? '' : 's') + ': ' + item.models.slice(0, 3).join(', ');
    } else if (item.ok) {
      detail = 'Running, but nothing downloaded yet.';
    } else {
      detail = item.error || 'Not running.';
    }
    body.appendChild(node('div', 'engine-row-why', detail));
    row.appendChild(body);
    list.appendChild(row);
  }

  const actions = $('engine-actions');
  actions.textContent = '';

  if (engine.ready) {
    const label = node('label', 'field', 'Which model should write the answers?');
    const select = node('select');
    const chosen = state.engine.engine_id;
    const current = (engine.engines || []).find((item) => item.id === chosen) || (engine.engines || [])[0];
    for (const model of (current && current.models) || []) {
      const option = node('option', null, model);
      option.value = model;
      if (model === engine.model) option.selected = true;
      select.appendChild(option);
    }
    select.addEventListener('change', async () => {
      await api.post('/api/settings', { engine_id: current.id, model: select.value });
      toast('This model will be used from now on.');
      refresh();
    });
    actions.appendChild(label);
    actions.appendChild(select);
  } else {
    const intro = node('p', null,
      'There is no engine running on this computer. nanoDoc still reads your documents and finds ' +
      'the paragraphs that answer your questions — it just cannot write them out as a sentence. ' +
      'Ollama is the easiest one to add, and it can be installed from here.');
    intro.style.color = 'var(--muted)';
    intro.style.fontSize = '14px';
    actions.appendChild(intro);

    if (engine.can_install) {
      const install = node('button', 'button primary', 'Install Ollama for me');
      install.style.marginTop = '12px';
      install.addEventListener('click', () => runEnginStep(install, '/api/engine/install', {}, 'Installing Ollama'));
      actions.appendChild(install);
      actions.appendChild(node('div', 'engine-row-why',
        'This runs: ' + (engine.install_command || '') + ' — the exact command is shown in the log before it starts.'));
    } else {
      actions.appendChild(node('p', null,
        'Go to ollama.com, download the installer and run it. Then press “Check again” below.'));
    }
  }

  const again = node('button', 'button small', 'Check again');
  again.style.marginTop = '16px';
  again.addEventListener('click', async () => {
    await refresh();
    openEngineModal();
    toast('Checked.');
  });
  actions.appendChild(again);

  // Suggested downloads, only offered when Ollama is actually there.
  const ollama = (engine.engines || []).find((item) => item.id === 'ollama' && item.ok);
  if (ollama && engine.suggested && engine.suggested.length) {
    actions.appendChild(node('h2', null, 'Models you can download'));
    for (const model of engine.suggested) {
      const row = node('div', 'model-row' + (model.fits ? '' : ' model-none'));
      const body = node('div', 'model-body');
      body.appendChild(node('div', 'model-name', model.title + ' — ' + model.name));
      body.appendChild(node('div', 'model-blurb',
        model.blurb + ' · ' + model.size_gb + ' GB to download'
        + (model.fits ? '' : ' · probably too big for this computer (' + model.ram_gb + ' GB memory)')));
      row.appendChild(body);
      const button = node('button', 'button small', 'Download');
      button.addEventListener('click', () =>
        runEnginStep(button, '/api/engine/pull', { model: model.name }, 'Downloading ' + model.name));
      row.appendChild(button);
      actions.appendChild(row);
    }
  }

  $('engine-modal').hidden = false;
  $('scrim').hidden = false;
}

/** Run a streaming engine step (install or download) into a visible log. */
async function runEnginStep(button, path, body, title) {
  button.disabled = true;
  const existing = $('engine-log');
  if (existing) existing.remove();

  const log = node('div', 'log');
  log.id = 'engine-log';
  log.appendChild(node('div', null, title + '…'));
  $('engine-actions').appendChild(log);

  try {
    await streamEvents(path, body, (event) => {
      const line = node('div', event.type === 'command' ? 'command' : null,
        (event.type === 'command' ? '$ ' : '') + event.message);
      log.appendChild(line);
      log.scrollTop = log.scrollHeight;
      if (event.type === 'done') {
        button.disabled = false;
        toast(event.message, 'good');
        refresh().then(() => setTimeout(openEngineModal, 700));
      }
      if (event.type === 'note') {
        button.disabled = false;
        toast(event.message, 'bad');
      }
    });
  } catch (error) {
    button.disabled = false;
    log.appendChild(node('div', null, error.message));
  }
}

// ---------------------------------------------------------------- wiring
function autosize(box) {
  box.style.height = 'auto';
  box.style.height = Math.min(160, box.scrollHeight) + 'px';
}

function main() {
  $('add-button').addEventListener('click', () => $('file-input').click());
  $('choose-button').addEventListener('click', () => $('file-input').click());
  $('dropzone').addEventListener('click', (event) => {
    if (event.target.tagName !== 'BUTTON') $('file-input').click();
  });
  $('file-input').addEventListener('change', (event) => {
    if (event.target.files.length) ingest(event.target.files);
    event.target.value = '';
  });

  $('close-doc').addEventListener('click', closeDocument);
  $('ask-button').addEventListener('click', ask);
  $('export-button').addEventListener('click', exportSession);

  const box = $('question');
  box.addEventListener('input', () => autosize(box));
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      ask();
    }
  });

  $('reader-close').addEventListener('click', closeReader);
  $('scrim').addEventListener('click', () => {
    closeReader();
    $('engine-modal').hidden = true;
    $('scrim').hidden = true;
  });
  $('reader-select').addEventListener('change', (event) => openPage(parseInt(event.target.value, 10), []));

  $('engine-chip').addEventListener('click', openEngineModal);
  $('engine-close').addEventListener('click', () => {
    $('engine-modal').hidden = true;
    $('scrim').hidden = true;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeReader();
      $('engine-modal').hidden = true;
      $('scrim').hidden = true;
    }
  });

  wireDrops();
  refresh();
  setInterval(refresh, 30000);
}

main();

// Save the whole conversation — questions, answers and their sources — as one file.
async function exportSession() {
  const turns = state.turns
    .filter((turn) => turn.question && (turn.answer || turn.done))
    .map((turn) => ({ question: turn.question, answer: turn.answer, hits: turn.hits || [] }));
  if (!turns.length) { toast('Ask something first — then there is something to save.', 'bad'); return; }
  try {
    const data = await api.post('/api/export', {
      title: (state.open && state.open.name) || 'nanoDoc conversation',
      turns,
    });
    if (data.ok) toast('Saved to ' + data.path, 'good');
  } catch (err) {
    toast(String(err.message || err), 'bad');
  }
}
