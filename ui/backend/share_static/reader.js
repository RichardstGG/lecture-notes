'use strict';
const $ = (id) => document.getElementById(id);
const invitation = location.hash.slice(1);
let joined = false;
let timer;
let polling = false;
let generation = 0;
let etag;
const audio = $('audio');
const rendered = new Map();
let audioAvailable = false;
let audioVersion = '';
let audioReload = 0;
let pendingSentence;
let stopAt;

function resetAudio() {
  audioAvailable = false;
  pendingSentence = undefined;
  stopAt = undefined;
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  $('audio-player').hidden = true;
  $('audio-status').textContent = '';
}
function startPendingSentence() {
  if (!pendingSentence || !audioAvailable) return;
  const { start, end } = pendingSentence;
  pendingSentence = undefined;
  if (Number.isFinite(audio.duration) && start >= audio.duration) {
    $('audio-status').textContent = '錄音尚未包含這個時間點，請稍後再試。';
    return;
  }
  try {
    audio.currentTime = start;
    stopAt = end;
    void audio.play().catch((error) => {
      if (error.name !== 'AbortError' && audioAvailable) {
        $('audio-status').textContent = '無法播放原始錄音，瀏覽器可能不支援此音訊格式。';
      }
    });
  } catch {
    $('audio-status').textContent = '無法跳到這個時間點，請稍後再試。';
  }
}
function playSentence(start, end) {
  audio.pause();
  stopAt = undefined;
  pendingSentence = { start, end };
  $('audio-status').textContent = '';
  if (audio.readyState < 1 || audio.error || (Number.isFinite(audio.duration) && start >= audio.duration)) {
    // A growing recording may have newer bytes than this media resource's duration.
    // Include a reload counter as text version may not change between attempts.
    audio.src = `/share/v1/audio?v=${encodeURIComponent(audioVersion)}-${Date.now()}-${++audioReload}`;
    $('audio-status').textContent = '正在載入錄音…';
    audio.load();
  } else {
    startPendingSentence();
  }
}
audio.addEventListener('loadedmetadata', () => {
  $('audio-status').textContent = '';
  startPendingSentence();
});
audio.addEventListener('error', () => {
  pendingSentence = undefined;
  if (audioAvailable) $('audio-status').textContent = '無法播放原始錄音，瀏覽器可能不支援此音訊格式，或分享已無法存取。';
});
audio.addEventListener('timeupdate', () => {
  if (stopAt !== undefined && audio.currentTime >= stopAt) {
    audio.pause();
    stopAt = undefined;
  }
});
function sentenceTime(line) {
  const match = /^`(\d{2,}):([0-5]\d):([0-5]\d)`(?:\s|$)/.exec(line);
  return match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) : undefined;
}
function renderDocument(id, content, playable) {
  const previous = rendered.get(id);
  if (previous?.content === content && previous.playable === playable) return;
  const lines = content.split('\n');
  const times = lines.map(sentenceTime);
  const ends = [];
  let next;
  for (let index = lines.length - 1; index >= 0; index--) {
    ends[index] = next;
    if (times[index] !== undefined) next = times[index];
  }
  const fragment = document.createDocumentFragment();
  lines.forEach((line, index) => {
    if (playable && times[index] !== undefined) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sentence-play';
      button.textContent = '▶';
      button.setAttribute('aria-label', `播放 ${line.slice(1, line.indexOf('`', 1))} 這一句`);
      button.addEventListener('click', () => playSentence(times[index], ends[index]));
      fragment.append(button);
    }
    fragment.append(document.createTextNode(line + (index < lines.length - 1 ? '\n' : '')));
  });
  $(id).replaceChildren(fragment);
  rendered.set(id, { content, playable });
}

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
  rendered.clear();
  resetAudio();
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
    document.body.classList.toggle('meeting', meeting);
    $('course').textContent = data.course || (meeting ? '會議逐字稿' : '課堂逐字稿與筆記');
    $('transcript-heading').textContent = meeting ? '原逐字稿' : '即時逐字稿';
    $('secondary-heading').textContent = meeting ? '帶發言者逐字稿' : '課堂筆記';
    $('secondary-download').href = meeting ? '/share/v1/download/speaker-transcript' : '/share/v1/download/notes';
    $('secondary-download').textContent = meeting ? '下載帶發言者逐字稿（目前版本）' : '下載筆記（目前版本）';
    $('version').textContent = `目前版本 · ${new Date(data.captured_at).toLocaleString()}`;
    $('status').textContent = '已連線 · 唯讀分享';
    const raw = data.transcript?.content || '等待內容產生…';
    const secondary = (meeting ? data.speaker_transcript : data.notes)?.content || '等待內容產生…';
    audioVersion = data.version;
    if (data.audio && !audioAvailable) {
      audio.src = '/share/v1/audio';
      audioAvailable = true;
      $('audio-player').hidden = false;
    } else if (!data.audio && audioAvailable) {
      resetAudio();
    }
    renderDocument('transcript', raw, audioAvailable);
    renderDocument('secondary', secondary, audioAvailable && meeting);
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
