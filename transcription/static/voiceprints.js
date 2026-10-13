const form = document.getElementById('enrollment');
const fileInput = document.getElementById('voiceprint-file');
const preview = document.getElementById('preview');
const status = document.getElementById('voiceprint-status');
const recordStatus = document.getElementById('recording-status');
const start = document.getElementById('start-recording');
const stop = document.getElementById('stop-recording');
const save = document.getElementById('save-voiceprint');
let recorder, stream, timer, recordedBlob, previewUrl;
let pendingMicrophone = false;

function showPreview(blob) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  preview.src = previewUrl;
  preview.hidden = false;
}
function releaseMicrophone() {
  clearInterval(timer);
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
}
fileInput.addEventListener('change', () => {
  recordedBlob = null;
  if (fileInput.files[0]) showPreview(fileInput.files[0]);
  else { preview.hidden = true; preview.removeAttribute('src'); }
});
document.querySelectorAll('.choose-speaker').forEach(button => {
  button.addEventListener('click', () => {
    const name = document.getElementById('speaker-name');
    name.value = button.dataset.name;
    name.focus();
  });
});
const language = document.getElementById('recording-language');
const regenerate = document.getElementById('prepare-recording');
const passage = document.getElementById('reading-passage');
const passages = {
  English: [
    'This morning I walked through a quiet park and listened to the birds. The sky was bright, and a gentle breeze moved through the trees. We talked about our plans for the week, asked a few questions, and agreed on the next steps. I enjoy learning something new and hearing different points of view.',
    'Today I am planning a short journey to visit some old friends. We will take the early train and meet near the station at ten. After lunch, we hope to explore the local market and choose fresh fruit for dinner. There is always something interesting to discover when we travel together.',
    'Yesterday we gathered around the table to share ideas for a new project. Everyone had a chance to speak and explain what mattered most to them. We wrote down our questions, compared several possible solutions, and decided to begin with a small experiment. Next Friday we will meet again to discuss what we learned.',
  ],
  Chinese: [
    '今天早上，我到附近的公園散步。陽光穿過樹葉，微風吹來，讓人覺得十分舒服。回家以後，我準備了一杯熱茶，開始安排這個星期的工作。我希望每天都能學到一些新知識，也能抽出時間，聽聽朋友分享生活中的有趣故事。',
    '這個週末，我打算和幾位朋友一起去旅行。我們約好早上十點在車站見面，先到市場看看，再找一家小餐廳吃午飯。下午如果天氣晴朗，就沿著河邊慢慢散步，欣賞風景。對我來說，和朋友聊天，一起發現新的事物，就是很開心的一天。',
    '昨天下午，我們坐在一起討論新的工作計劃。每個人都提出了自己的想法，也認真聆聽別人的意見。大家把問題寫下來，比較不同的方法，最後決定先做一個小實驗。下個星期五，我們會再次見面，分享結果，並且安排接下來的工作。',
  ],
};
function regeneratePassage() {
  const choices = passages[language.value].filter(text => text !== passage.textContent);
  passage.textContent = choices[Math.floor(Math.random() * choices.length)];
  passage.lang = language.value === 'Chinese' ? 'zh-Hant' : 'en';
}
regenerate.addEventListener('click', regeneratePassage);
language.addEventListener('change', regeneratePassage);
regeneratePassage();
if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
  start.disabled = true;
  recordStatus.textContent = 'Microphone recording requires a supported browser on HTTPS or localhost. In Chrome, to enable microphone access for an insecure host, open chrome://flags/#unsafely-treat-insecure-origin-as-secure, add the full origin (for example, http://your-host:8000) to the allowlist, enable the flag, and relaunch Chrome. You can upload an audio file instead.';
}
start.addEventListener('click', async () => {
  start.disabled = true;
  save.disabled = true;
  fileInput.disabled = true;
  pendingMicrophone = true;
  language.disabled = true;
  regenerate.disabled = true;
  recordStatus.textContent = 'Waiting for microphone permission…';
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1}, video: false});
    const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(type => MediaRecorder.isTypeSupported(type));
    recorder = new MediaRecorder(stream, mimeType ? {mimeType} : undefined);
    const chunks = [];
    let failed = false;
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onerror = () => { failed = true; recordStatus.textContent = 'Recording failed. Please try again.'; releaseMicrophone(); };
    recorder.onstop = () => {
      releaseMicrophone();
      if (!failed && chunks.length) {
        recordedBlob = new Blob(chunks, {type: recorder.mimeType || chunks[0].type});
        fileInput.value = '';
        showPreview(recordedBlob);
        recordStatus.textContent = 'Recording ready. Listen before saving, or record again.';
      }
      language.disabled = false;
      regenerate.disabled = false;
      start.disabled = false;
      stop.disabled = true;
      save.disabled = false;
      fileInput.disabled = false;
    };
    recorder.start();
    stop.disabled = false;
    const began = Date.now();
    recordStatus.textContent = 'Recording… 0s / 60s';
    timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - began) / 1000);
      recordStatus.textContent = `Recording… ${seconds}s / 60s`;
      // Leave room for the final audio chunk within the server's 60-second limit.
      if (seconds >= 59 && recorder.state === 'recording') recorder.stop();
    }, 250);
  } catch (error) {
    releaseMicrophone();
    recordStatus.textContent = `Microphone unavailable: ${error.message}. You can upload audio instead.`;
    language.disabled = false;
    regenerate.disabled = false;
    start.disabled = false;
    save.disabled = false;
    fileInput.disabled = false;
  } finally { pendingMicrophone = false; }
});
stop.addEventListener('click', () => {
  if (recorder?.state === 'recording') { stop.disabled = true; recorder.stop(); }
});
async function send(url, body) {
  const response = await fetch(url, {method: 'POST', body, headers: {Accept: 'application/json'}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (pendingMicrophone || recorder?.state === 'recording') return;
  if (!recordedBlob && !fileInput.files.length) { status.textContent = 'Upload audio or record a sample first.'; return; }
  const data = new FormData(form);
  if (recordedBlob) data.set('file', recordedBlob, 'microphone-recording');
  form.querySelector('fieldset').disabled = true;
  status.textContent = 'Extracting and saving voiceprint… This may take a while when loading the speaker model.';
  try { await send(form.action, data); window.location.reload(); }
  catch (error) { status.textContent = error.message; form.querySelector('fieldset').disabled = false; }
});
document.querySelectorAll('.delete-voiceprint').forEach(button => {
  button.addEventListener('click', async () => {
    if (!window.confirm('Delete this saved voiceprint? This cannot be undone.')) return;
    button.disabled = true;
    try { await send(button.dataset.url); window.location.reload(); }
    catch (error) { status.textContent = error.message; button.disabled = false; }
  });
});
window.addEventListener('pagehide', () => { releaseMicrophone(); if (previewUrl) URL.revokeObjectURL(previewUrl); });

document.querySelectorAll('.rename-speaker').forEach(renameForm => {
  renameForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = renameForm.querySelector('button');
    button.disabled = true;
    try { await send(renameForm.action, new FormData(renameForm)); window.location.reload(); }
    catch (error) { status.textContent = error.message; button.disabled = false; }
  });
});
