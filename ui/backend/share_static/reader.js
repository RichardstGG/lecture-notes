'use strict';
const $ = (id) => document.getElementById(id);
const invitation = location.hash.slice(1);
let joined = false;
let timer;
let polling = false;
let generation = 0;
let etag;

async function request(path, init) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(8000), ...init });
  if (!response.ok && response.status !== 304) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.error?.message || `請求失敗（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return response;
}
function disconnected(error) {
  $('status').textContent = error.status ? error.message : '分享服務無法連線，可能已關閉；正在重試…';
  etag = undefined;
  $('reader').hidden = true;
  $('transcript').textContent = '';
  $('secondary').textContent = '';
  if ([401, 403, 409, 410].includes(error.status)) {
    joined = false;
    clearTimeout(timer);
    $('join-form').hidden = false;
  }
}
async function poll() {
  if (polling) return;
  polling = true;
  const current = generation;
  try {
    const response = await request('/share/v1/snapshot', { headers: etag ? { 'If-None-Match': etag } : {} });
    if (response.status === 304) return;
    const data = await response.json();
    if (current !== generation) return;
    etag = response.headers.get('etag');
    joined = true;
    $('join-form').hidden = true;
    $('reader').hidden = false;
    const meeting = data.work_type === 'meeting';
    $('course').textContent = data.course || (meeting ? '會議逐字稿' : '課堂逐字稿與筆記');
    $('transcript-heading').textContent = meeting ? '原逐字稿' : '即時逐字稿';
    $('secondary-heading').textContent = meeting ? '帶發言者逐字稿' : '課堂筆記';
    $('secondary-download').href = meeting ? '/share/v1/download/speaker-transcript' : '/share/v1/download/notes';
    $('secondary-download').textContent = meeting ? '下載帶發言者逐字稿（目前版本）' : '下載筆記（目前版本）';
    $('version').textContent = `目前版本 · ${new Date(data.captured_at).toLocaleString()}`;
    $('status').textContent = '已連線 · 唯讀分享';
    const raw = data.transcript?.content || '等待內容產生…';
    const secondary = (meeting ? data.speaker_transcript : data.notes)?.content || '等待內容產生…';
    if ($('transcript').textContent !== raw) $('transcript').textContent = raw;
    if ($('secondary').textContent !== secondary) $('secondary').textContent = secondary;
  } catch (error) {
    if (current === generation) disconnected(error);
  } finally {
    polling = false;
    if (joined && current === generation) timer = setTimeout(poll, 2000);
  }
}
$('join-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    await request('/share/v1/join', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ invitation, nickname: $('nickname').value.trim() }),
    });
    joined = true;
    clearTimeout(timer);
    if (polling) timer = setTimeout(poll, 2000);
    else await poll();
  } catch (error) { disconnected(error); }
  finally { button.disabled = false; }
});
$('leave').addEventListener('click', async () => {
  try {
    await request('/share/v1/leave', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    generation++;
    disconnected({ status: 401, message: '已離開分享。' });
  } catch (error) { disconnected(error); }
});
// A refresh can resume the existing HttpOnly cookie without creating another seat.
void poll();
