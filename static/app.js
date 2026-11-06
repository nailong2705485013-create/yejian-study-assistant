'use strict';
const $ = id => document.getElementById(id);
let upload = null, inspected = null, currentId = null, currentResult = null, activeTab = 'notes', timer = null, revision = 0;
let savedConnections = {};
let concurrencyRevision = 0, concurrencySave = Promise.resolve(true);
const modelPickers = {text: {names: []}, vision: {names: []}};
function modelInput(kind) { return $(kind === 'text' ? 'model-name' : 'vision-model'); }
function modelChanged(kind) {
  $('visual-consent').checked = false;
  destinations();
}
function resetModelPicker(kind) {
  modelPickers[kind].names = [];
  $(kind + '-model-list').replaceChildren();
  $(kind + '-model-list').classList.add('hidden');
  $(kind + '-manual-model').classList.remove('hidden');
  $(kind + '-model-label').htmlFor = modelInput(kind).id;
}
function setModelOptions(kind, names) {
  const input = modelInput(kind), select = $(kind + '-model-list');
  const clean = [...new Set(names.filter(name => typeof name === 'string' && name.trim() && name.length <= 200))];
  resetModelPicker(kind);
  if (!clean.length) return false;
  modelPickers[kind].names = clean;
  const option = (value, text) => { const el = node('option', text); el.value = value; return el; };
  select.append(option('', '请选择模型'), ...clean.map((name, index) => option('model:' + index, name)), option('manual', '其他模型 / 手动填写…'));
  const current = clean.indexOf(input.value.trim());
  select.value = current >= 0 ? 'model:' + current : clean.length === 1 ? 'model:0' : '';
  input.value = select.value ? clean[Number(select.value.split(':')[1])] : '';
  select.classList.remove('hidden');
  $(kind + '-manual-model').classList.add('hidden');
  $(kind + '-model-label').htmlFor = select.id;
  modelChanged(kind);
  return true;
}
for (const kind of ['text', 'vision']) $(kind + '-model-list').addEventListener('change', () => {
  const value = $(kind + '-model-list').value;
  $(kind + '-manual-model').classList.toggle('hidden', value !== 'manual');
  if (value !== 'manual') modelInput(kind).value = value ? modelPickers[kind].names[Number(value.split(':')[1])] : '';
  modelChanged(kind);
  if (value === 'manual') modelInput(kind).focus();
});
const textProfile = () => ({base_url: $('base-url').value, model: $('model-name').value, api_key: $('api-key').value, json_mode: $('json-mode').checked});
const visionProfile = () => $('reuse-text').checked ? textProfile() : ({base_url: $('vision-url').value, model: $('vision-model').value, api_key: $('vision-key').value, json_mode: $('vision-json').checked});
const importOptions = () => ({page_range: $('page-range').value, request_budget: Number($('request-budget').value), questions: Number($('questions').value), cards: Number($('cards').value), concurrency: Number($('concurrency').value)});
$('concurrency').addEventListener('change', () => {
  const value = Number($('concurrency').value), version = ++concurrencyRevision;
  $('concurrency-status').textContent = '正在保存并发设置…';
  // Serialize writes so a slower earlier response cannot overwrite a newer choice.
  concurrencySave = concurrencySave.then(async () => {
    try {
      const result = await post('/api/settings', {concurrency: value});
      if (version === concurrencyRevision) $('concurrency-status').textContent = result.message;
      return true;
    } catch (error) {
      if (version === concurrencyRevision) $('concurrency-status').textContent = '未保存：' + error.message;
      return false;
    }
  });
});
function destinations() {
  $('vision-fields').classList.toggle('hidden', $('reuse-text').checked);
  $('privacy').textContent = `生成时，将选中的文字和学习目标发送至 ${$('base-url').value || '所填文字服务'}。原文件与结果保存在本机；Key仅在点击“加密保存连接”后加密存盘。`;
  $('visual-destination').textContent = `同意将选中幻灯片的整页图片发送至 ${visionProfile().base_url || '所填视觉API地址'}（${visionProfile().model || '尚未配置模型'}）。`;
  for (const [kind, field, url] of [['text','api-key','base-url'],['vision','vision-key','vision-url']]) {
    const saved = savedConnections[kind];
    $(field).placeholder = saved?.has_saved_key && saved.base_url === $(url).value.trim().replace(/\/+$/, '') ? '已保存此服务的Key；留空复用，输入可替换' : '输入此服务的Key，可选择加密保存';
  }
}
function applySaved(connections) {
  savedConnections = connections || {};
  for (const [kind, url, model, json] of [['text','base-url','model-name','json-mode'],['vision','vision-url','vision-model','vision-json']]) {
    const config = savedConnections[kind];
    if (config) {
      if ($(url).value.trim().replace(/\/+$/, '') !== config.base_url) resetModelPicker(kind);
      $(url).value = config.base_url; $(model).value = config.model; $(json).checked = config.json_mode;
      if (modelPickers[kind].names.length) {
        const index = modelPickers[kind].names.indexOf(config.model);
        $(kind + '-model-list').value = index >= 0 ? 'model:' + index : 'manual';
        $(kind + '-manual-model').classList.toggle('hidden', index >= 0);
      }
    }
  }
  $('reuse-text').checked = !!savedConnections.reuse_text_connection;
  if (savedConnections.text?.has_saved_key) $('key-summary').textContent = '文字模型 · 已保存本机加密连接';
  destinations();
}
$('save-connections').addEventListener('click', async () => {
  $('save-connections').disabled = true;
  try {
    const result = await post('/api/connections', {connection:textProfile(),vision_connection:visionProfile(),reuse_text_connection:$('reuse-text').checked});
    $('api-key').value = ''; $('vision-key').value = ''; applySaved(result.connections); $('save-status').textContent = result.message;
  } catch (error) { $('save-status').textContent = error.message; }
  finally { $('save-connections').disabled = false; }
});
$('clear-connections').addEventListener('click', async () => {
  try { const result = await post('/api/connections/clear', {}); savedConnections = {}; $('api-key').value=''; $('vision-key').value=''; $('key-summary').textContent='文字模型连接'; destinations(); $('save-status').textContent=result.message; }
  catch (error) { $('save-status').textContent=error.message; }
});
const busyStates = new Set(['queued', 'running']);
const statusNames = {queued: '排队中', running: '处理中', done: '已完成', failed: '未完成'};
function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function showError(message) { $('error').textContent = message || ''; $('error').classList.toggle('hidden', !message); }
async function request(url, options) { const res = await fetch(url, options); const data = await res.json(); if (!res.ok) throw new Error(data.error || '请求失败，请确认本地服务仍在运行。'); return data; }
function post(url, data) { return request(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)}); }
function encodeFile(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve({filename: file.name, content: reader.result.split(',')[1]}); reader.onerror = () => reject(new Error('无法读取文件。')); reader.readAsDataURL(file); }); }
async function chooseFile(file) {
  if (!file) return;
  const version = ++revision; upload = null; inspected = null; $('generate').disabled = true; showError('');
  $('file-info').classList.remove('hidden'); $('file-info').textContent = '正在本机读取资料…';
  try {
    if (file.size > 50 * 1024 * 1024) throw new Error('文件不能超过50 MiB。');
    const payload = await encodeFile(file);
    if (version !== revision) return;
    upload = payload; $('visual-consent').checked = false;
    await inspectUpload();
  } catch (error) { if (version !== revision) return; $('file-info').textContent = '资料未能导入'; showError(error.message); }
}
async function inspectUpload() {
  if (!upload) return;
  const version = ++revision; inspected = null; $('generate').disabled = true; showError('');
  try {
    const preview = await post('/api/inspect', {...upload, ...importOptions()});
    if (version !== revision) return;
    inspected = preview;
    $('file-info').textContent = `${preview.filename}\n${preview.pages ? preview.selected_pages.length + '/' + preview.pages + '页 · ' : ''}${preview.characters.toLocaleString()}字 · ${preview.segments}段\n预计${preview.normal_calls}次起；分批与核验修正时最多${preview.max_calls}次，达到预算会停止。` + (preview.warnings.length ? '\n' + preview.warnings.join('\n') : '');
    $('visual-consent-label').classList.toggle('hidden', !preview.vision_pages.length);
    if (preview.vision_pages.length) $('vision-settings').open = true;
    destinations();
    $('generate').disabled = false;
    $('stage-read').classList.add('active');
  } catch (error) { if (version !== revision) return; $('file-info').textContent = '资料未能导入，请调整范围后更新预览'; showError(error.message); }
}
$('reinspect').addEventListener('click', inspectUpload);
for (const id of ['page-range', 'request-budget', 'questions', 'cards']) $(id).addEventListener('change', () => { inspected = null; $('generate').disabled = true; if (upload) inspectUpload(); });
for (const id of ['base-url', 'model-name', 'vision-url', 'vision-model', 'reuse-text', 'api-key', 'vision-key', 'json-mode', 'vision-json']) $(id).addEventListener('input', () => { $('visual-consent').checked = false; destinations(); });
for (const [id, kind] of [['base-url','text'], ['api-key','text'], ['vision-url','vision'], ['vision-key','vision']]) $(id).addEventListener('input', () => {
  resetModelPicker(kind);
  if (kind === 'text' && $('reuse-text').checked) resetModelPicker('vision');
  $(kind + '-status').textContent = '连接信息已更改，可重新获取列表或手动填写模型名。';
});
for (const kind of ['text', 'vision']) for (const operation of ['models', 'probe']) $(kind + '-' + operation).addEventListener('click', async event => {
  const button = event.currentTarget, output = $(kind + '-status'); button.disabled = true;
  const connection = kind === 'text' ? textProfile() : visionProfile();
  const reuse = $('reuse-text').checked;
  const target = kind === 'vision' && reuse ? 'text' : kind;
  const stamp = JSON.stringify([connection.base_url, connection.api_key, reuse]);
  output.textContent = operation === 'models' ? '正在获取列表…' : '正在发送测试请求…';
  try {
    if (!await concurrencySave) throw new Error('并发设置尚未保存，请重新选择并发数后重试。');
    const result = await post('/api/' + operation, {connection: {...connection, ...(operation === 'models' ? {model: ''} : {})}, kind, profile_kind: kind, reuse_text_connection: reuse});
    const latest = kind === 'text' ? textProfile() : visionProfile();
    if (stamp !== JSON.stringify([latest.base_url, latest.api_key, $('reuse-text').checked])) return;
    if (operation === 'models') {
      if (!Array.isArray(result.models)) throw new Error('服务返回的模型列表格式不兼容。');
      const available = setModelOptions(target, result.models);
      output.textContent = available ? `已获取${modelPickers[target].names.length}个模型，可直接下拉选择，也可选“其他模型 / 手动填写”。列表不代表模型能力，请再测试连接。${target !== kind ? '复用模式下，请在上方文字模型中选择。' : ''}` : '服务没有返回可选模型，请手动填写模型名后测试连接。';
    } else output.textContent = result.message;
  } catch (error) {
    const latest = kind === 'text' ? textProfile() : visionProfile();
    if (stamp !== JSON.stringify([latest.base_url, latest.api_key, $('reuse-text').checked])) return;
    if (operation === 'models') resetModelPicker(target);
    output.textContent = error.message + (operation === 'models' ? ' 可直接手动填写模型名，再测试连接。' : '');
  }
  finally { button.disabled = false; }
});
$('file').addEventListener('change', event => chooseFile(event.target.files[0]));
$('dropzone').addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('file').click(); } });
for (const name of ['dragover', 'dragleave', 'drop']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.toggle('dragging', name === 'dragover'); if (name === 'drop') chooseFile(event.dataTransfer.files[0]); });
$('sample').addEventListener('click', async () => { try { const response = await fetch('/sample.txt'); if (!response.ok) throw new Error('示例暂时不可用。'); chooseFile(new File([await response.text()], '计算机网络-示例讲义.txt', {type: 'text/plain'})); } catch (error) { showError(error.message); } });
$('generate').addEventListener('click', async () => {
  if (!upload || !inspected) return;
  $('generate').disabled = true; showError('');
  try {
    if (!await concurrencySave) throw new Error('并发设置尚未保存，请重新选择并发数后重试。');
    const data = await post('/api/jobs', {...upload, ...importOptions(), connection: textProfile(), vision_connection: visionProfile(), reuse_text_connection: $('reuse-text').checked, visual_consent: $('visual-consent').checked, focus: $('focus').value});
    $('api-key').value = ''; $('vision-key').value = ''; $('visual-consent').checked = false; currentResult = null; $('results').classList.add('hidden'); $('empty').classList.add('hidden');
    await watch(data.id); await history();
  } catch (error) { showError(error.message); $('generate').disabled = !upload; }
});
async function history() {
  const jobs = await request('/api/jobs'); $('history').replaceChildren();
  if (!jobs.length) $('history').append(node('p', '生成后的学习资料会保存在这里。', 'micro'));
  for (const job of jobs) {
    const button = node('button', undefined, 'history-item'); button.type = 'button';
    button.append(node('span', job.filename, 'history-name'), node('span', `${statusNames[job.status]} · ${job.created_at.slice(5, 16).replace('T', ' ')}`, 'history-caption'));
    button.addEventListener('click', () => watch(job.id).catch(error => showError(error.message))); $('history').append(button);
  }
}
async function watch(id) {
  clearTimeout(timer); currentId = id;
  const job = await request('/api/jobs/' + id); if (currentId !== id) return;
  $('progress-panel').classList.remove('hidden'); $('empty').classList.add('hidden');
  $('progress-title').textContent = statusNames[job.status] + ' · ' + job.filename;
  $('progress').value = job.progress; $('progress-number').textContent = job.progress + '%';
  $('progress-message').textContent = job.error || job.events.at(-1)?.message || '任务已提交，正在等待处理。';
  $('events').replaceChildren(...job.events.map(event => node('li', `${event.time.slice(11, 19)}  ${event.message}`)));
  const percent = job.progress;
  for (const [id, threshold] of [['stage-read', 0], ['stage-extract', 8], ['stage-generate', 58], ['stage-review', 85]]) $(id).classList.toggle('active', percent >= threshold);
  $('generate').disabled = busyStates.has(job.status) || !upload;
  if (job.status === 'done') { showError(''); renderResult(job.result, id); history().catch(() => {}); }
  else if (job.status === 'failed') { $('results').classList.add('hidden'); showError(job.error); history().catch(() => {}); }
  else { $('results').classList.add('hidden'); timer = setTimeout(() => watch(id).catch(error => showError(error.message)), 1500); }
}
function citeButtons(item) {
  const box = node('div', undefined, 'citations');
  for (const cite of item.citations) {
    const source = currentResult.sources.find(source => source.id === cite.source_id);
    const button = node('button', '↗ ' + source.locator, 'citation');
    button.addEventListener('click', () => {
      $('source-title').textContent = currentResult.filename + ' · ' + source.locator;
      const visual = source.kind === 'visual';
      $('source-quote').textContent = (visual ? '模型图像识别（非原文逐字核验）：' : '引用原文：') + cite.quote;
      $('source-text').textContent = source.text + (source.limitations ? '\n识别限制：' + source.limitations : '');
      $('source-image').classList.toggle('hidden', !visual);
      if (visual && /^slides\/slide-\d{4}\.png$/.test(source.image || '')) $('source-image').src = `/api/jobs/${currentId}/${source.image}`;
      else $('source-image').removeAttribute('src');
      $('source-download').href = '/api/jobs/' + currentId + '/source'; $('source-dialog').showModal();
    }); box.append(button);
  } return box;
}
function renderResult(result, id) {
  currentResult = result; $('results').classList.remove('hidden');
  $('result-date').textContent = 'YOUR LEARNING EDIT / ' + result.completed_at.slice(0, 10);
  const hasVisual = result.sources.some(source => source.kind === 'visual');
  $('evidence-badge').textContent = hasVisual ? '含图像识别 · 请对照原图' : '✓ 文字引用已核验';
  $('result-name').textContent = result.filename; $('result-meta').textContent = '学习目标：' + result.focus + '\n页码为文件物理页序；文字引用逐字匹配，图像识别需点击出处对照原图复核。';
  $('warnings').textContent = [...result.warnings, ...(result.removed_count ? [`审核剔除了${result.removed_count}条缺乏证据或有错误的内容，详情见下方审核记录。`] : [])].join('\n');
  $('warnings').classList.toggle('hidden', !$('warnings').textContent);
  for (const kind of ['notes', 'questions', 'cards']) $('count-' + kind).textContent = result[kind].length;
  $('download-word').href = `/api/jobs/${id}/notes.docx`; $('download-md').href = `/api/jobs/${id}/notes.md`; $('download-cards').href = `/api/jobs/${id}/cards.tsv`; $('download-json').href = `/api/jobs/${id}/result.json`;
  $('audit').replaceChildren(node('p', result.audit.summary), node('p', `模型 ${result.model} · 请求 ${result.usage.calls} 次 · 输入 ${result.usage.prompt_tokens.toLocaleString()} tokens · 输出 ${result.usage.completion_tokens.toLocaleString()} tokens`), node('p', '引用核验检查原文是否匹配；模型审核检查语义支持关系。需要确认重要内容时，请打开出处复核。'));
  if (result.audit.issues.length) { const list = node('ul'); for (const issue of result.audit.issues) list.append(node('li', `${issue.item_id}：${issue.reason}`)); $('audit').append(list); }
  renderTab();
}
function renderTab() {
  if (!currentResult) return;
  for (const button of document.querySelectorAll('[data-tab]')) { const active = button.dataset.tab === activeTab; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); }
  const box = $('result-content'); box.replaceChildren(); box.className = activeTab === 'cards' ? 'flashcard-grid' : '';
  box.setAttribute('aria-labelledby', 'tab-' + activeTab);
  currentResult[activeTab].forEach((item, index) => {
    const card = node('article', undefined, 'content-card'); card.append(node('div', `${String(index + 1).padStart(2, '0')} / ${item.id}`, 'item-number'));
    if (activeTab === 'notes') card.append(node('h4', item.title), node('p', item.content));
    else if (activeTab === 'questions') { card.append(node('h4', item.question)); const answer = node('details'); answer.append(node('summary', '想好之后，查看答案与解析'), node('p', '答案：' + item.answer), node('p', '解析：' + item.explanation)); card.append(answer); }
    else { let flipped = false; const flip = node('button', undefined, 'flip-button'); const text = node('h4', item.front); const hint = node('span', '点击翻面 · 查看答案', 'flip-hint'); flip.append(text, hint); flip.addEventListener('click', () => { flipped = !flipped; text.textContent = flipped ? item.back : item.front; hint.textContent = flipped ? '点击翻面 · 返回问题' : '点击翻面 · 查看答案'; }); card.append(flip); }
    card.append(citeButtons(item)); box.append(card);
  });
}
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click', () => { activeTab = button.dataset.tab; renderTab(); });
$('close-source').addEventListener('click', () => $('source-dialog').close());
async function initialize() { const version = concurrencyRevision; try { const config = await request('/api/config'); if(version === concurrencyRevision) { $('concurrency').value = String(config.settings?.concurrency || 2); if(config.settings_error) $('concurrency-status').textContent = config.settings_error; } $('key-summary').textContent = config.has_key ? '文字模型 · 官方DeepSeek可用终端Key' : '文字模型连接 · 需要 API Key'; applySaved(config.connections); if (!config.has_key && !config.connections?.text?.has_saved_key) $('key-settings').open = true; if(config.storage_error) $('save-status').textContent=config.storage_error; await history(); } catch (error) { showError(error.message); } }
initialize();
