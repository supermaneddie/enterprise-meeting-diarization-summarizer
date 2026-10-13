import {pcm16, wavBlob} from './realtime-audio.js';
const root = document.getElementById('realtime');
const title = document.getElementById('recording-title');
const transcript = document.getElementById('recording-transcript');
const start = document.getElementById('record-start');
const pause = document.getElementById('record-pause');
const save = document.getElementById('record-save');
const status = document.getElementById('recording-status');
const error = document.getElementById('recording-error');
const preview = document.getElementById('recording-preview');
const finalPass = document.getElementById('record-final');
const canvas = document.getElementById('waveform');
const now = new Date();
const pad = value => String(value).padStart(2, '0');
const initialTitle = `Untitled_Recordings_${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
title.value = initialTitle;
let context, stream, capture, analyser, animation, pauseResolve, socket;
let recording = false, busy = false, saved = false, pausing = null;
let sampleCount = 0, pendingBytes = 0, liveText = '', edited = false, incomplete = false;
let partial = '', finishResolve, readyReject, readyResolve;
const audioParts = [];
const maxBytes = Math.min(128 * 1024 * 1024, Number(root.dataset.maxBytes) || Infinity) - 65536;

function dirty() {
  return !saved && (recording || pausing || sampleCount > 0 || title.value !== initialTitle || transcript.value !== '');
}
transcript.addEventListener('input', () => { edited = true; });
window.addEventListener('beforeunload', event => {
  if (dirty()) { event.preventDefault(); event.returnValue = ''; }
});
document.addEventListener('click', event => {
  const link = event.target.closest('a[href]');
  if (link && !link.target && !event.ctrlKey && !event.metaKey && dirty()) {
    if (!window.confirm('Leave this page? Unsaved audio, title, and transcript will be lost.')) event.preventDefault();
    else saved = true;
  }
});
window.addEventListener('pagehide', () => {
  stream?.getTracks().forEach(track => track.stop());
  socket?.close();
  if (context && context.state !== 'closed') void context.close();
  cancelAnimationFrame(animation);
});
function update() {
  start.disabled = recording || busy || Boolean(pausing);
  start.textContent = sampleCount ? 'Resume / reconnect' : 'Start';
  pause.disabled = !recording || busy;
  save.disabled = !sampleCount || busy;
  const seconds = Math.floor(sampleCount / 16000);
  status.textContent = `${recording ? 'Recording' : sampleCount ? 'Paused' : 'Ready'} · ${seconds}s${busy || pausing ? ' · Please wait…' : ''}`;
}
function appendText(text) {
  text = text.trim();
  if (!text) return;
  liveText += (liveText ? '\n' : '') + text;
  transcript.value += (transcript.value ? '\n' : '') + text;
}
function fail(message) {
  incomplete = true;
  error.textContent = message + ' Audio is retained. Save for final transcription or reconnect.';
  if (partial) appendText(partial);
  partial = ''; preview.textContent = '';
  readyReject?.(new Error(message)); readyReject = readyResolve = null;
  finishResolve?.(); finishResolve = null;
  const old = socket; socket = null; old?.close();
  if (recording) void pauseRecording();
  update();
}
function connect() {
  const url = new URL(root.dataset.stream, window.location.href);
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const current = new WebSocket(url);
  socket = current; pendingBytes = 0;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (socket === current) fail('Connection to the transcription server timed out.');
    }, 15000);
    readyResolve = () => { clearTimeout(timer); resolve(); };
    readyReject = problem => { clearTimeout(timer); reject(problem); };
    current.onmessage = ({data}) => {
      if (socket !== current) return;
      let event;
      try { event = JSON.parse(data); } catch (_) { fail('Invalid server response.'); return; }
      if (event.type === 'ready') {
        readyResolve?.(); readyResolve = readyReject = null;
      } else if (event.type === 'ack') {
        pendingBytes = Math.max(0, pendingBytes - event.bytes);
      } else if (event.type === 'partial') {
        partial = event.text; preview.textContent = partial;
      } else if (event.type === 'segment') {
        appendText(event.text); partial = ''; preview.textContent = '';
      } else if (event.type === 'done') {
        finishResolve?.(); finishResolve = null;
        socket = null; current.close();
      } else if (event.type === 'error') fail(event.message || 'Live transcription failed.');
    };
    current.onerror = () => { if (socket === current) fail('Unable to connect to live transcription.'); };
    current.onclose = () => { if (socket === current) fail('Live transcription disconnected.'); };
  });
}
function sendAudio(bytes) {
  if (!socket || socket.readyState !== WebSocket.OPEN) { incomplete = true; return; }
  try {
    pendingBytes += bytes.length;
    socket.send(bytes);
    if (pendingBytes > 256000 || socket.bufferedAmount > 256000) {
      error.textContent = 'Recording paused because transcription is falling behind. Wait for finalization before resuming.';
      void pauseRecording();
    }
  } catch (_) { fail('Audio delivery failed.'); }
}
function draw() {
  const values = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(values);
  const brush = canvas.getContext('2d');
  brush.clearRect(0, 0, canvas.width, canvas.height);
  brush.strokeStyle = '#167451'; brush.lineWidth = 2; brush.beginPath();
  values.forEach((value, index) => {
    const x = index / (values.length - 1) * canvas.width, y = value / 255 * canvas.height;
    if (index) brush.lineTo(x, y); else brush.moveTo(x, y);
  });
  brush.stroke(); animation = requestAnimationFrame(draw);
}
async function initialize() {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    throw new Error('Microphone recording requires AudioWorklet support on HTTPS or localhost.');
  }
  stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1}, video: false});
  try {
    // The browser's audio engine resamples device audio, including 44.1/48 kHz microphones.
    context = new AudioContext({sampleRate: 16000});
    if (context.sampleRate !== 16000) throw new Error('This browser cannot capture at the required 16 kHz sample rate.');
    await context.audioWorklet.addModule(root.dataset.worklet);
    analyser = context.createAnalyser(); analyser.fftSize = 2048;
    const source = context.createMediaStreamSource(stream);
    capture = new AudioWorkletNode(context, 'microphone-capture');
    capture.port.onmessage = ({data}) => {
      if (data.paused) { pauseResolve?.(); pauseResolve = null; return; }
      if (!data.samples) return;
      const bytes = pcm16(data.samples);
      audioParts.push(bytes); sampleCount += data.samples.length;
      sendAudio(bytes); update();
      if (recording && sampleCount * 2 >= maxBytes) {
        void pauseRecording(); error.textContent = 'Recording limit reached. Save before starting another recording.';
      }
    };
    source.connect(analyser); source.connect(capture); capture.connect(context.destination);
    stream.getAudioTracks().forEach(track => track.addEventListener('ended', () => {
      if (recording) void pauseRecording();
      error.textContent = 'Microphone disconnected. Save your recording and open a new session.';
    }));
    stream.getAudioTracks().forEach(track => { track.enabled = false; });
    draw();
  } catch (problem) {
    stream.getTracks().forEach(track => track.stop());
    if (context) await context.close();
    context = null; throw problem;
  }
}
function pauseRecording() {
  if (pausing) return pausing;
  if (!recording) return Promise.resolve();
  recording = false;
  pausing = (async () => {
    await new Promise(resolve => {
      const timer = setTimeout(() => {
        incomplete = true;
        error.textContent = 'Audio capture did not flush in time. Save the retained audio for final transcription.';
        pauseResolve = null; resolve();
      }, 3000);
      pauseResolve = () => { clearTimeout(timer); resolve(); };
      capture.port.postMessage('pause');
    });
    stream.getAudioTracks().forEach(track => { track.enabled = false; });
    if (socket?.readyState === WebSocket.OPEN) {
      await new Promise(resolve => {
        const timer = setTimeout(() => fail('Finalizing live transcription timed out.'), 65000);
        finishResolve = () => { clearTimeout(timer); resolve(); };
        try { socket.send(JSON.stringify({type: 'finish'})); }
        catch (_) { fail('Could not finalize live transcription.'); }
      });
    }
  })().finally(() => { pausing = null; update(); });
  update(); return pausing;
}
start.addEventListener('click', async () => {
  busy = true; error.textContent = ''; update();
  try {
    if (sampleCount * 2 >= maxBytes) throw new Error('Recording limit reached. Save first.');
    if (!context) await initialize();
    if (stream.getAudioTracks().some(track => track.readyState === 'ended')) throw new Error('Microphone disconnected. Save and open a new session.');
    await context.resume(); await connect();
    stream.getAudioTracks().forEach(track => { track.enabled = true; });
    recording = true; capture.port.postMessage('start');
  } catch (problem) { error.textContent = problem.message; }
  finally { busy = false; update(); }
});
pause.addEventListener('click', async () => {
  busy = true; update();
  try { await pauseRecording(); } finally { busy = false; update(); }
});
save.addEventListener('click', async () => {
  if (!title.reportValidity()) return;
  busy = true; title.disabled = true; transcript.readOnly = true; finalPass.disabled = true; update();
  try {
    await pauseRecording();
    const body = new FormData();
    body.append('name', title.value); body.append('text', transcript.value);
    body.append('live_text', liveText); body.append('edited', String(edited));
    body.append('incomplete', String(incomplete)); body.append('final_transcription', String(finalPass.checked));
    body.append('audio', wavBlob(audioParts, sampleCount), 'microphone.wav');
    const response = await fetch(root.dataset.save, {method: 'POST', body});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `Save failed (${response.status}). Your recording is still here.`);
    saved = true; window.location.assign(new URL(result.url, response.url).href);
  } catch (problem) { error.textContent = problem.message; }
  finally { busy = false; title.disabled = false; transcript.readOnly = false; finalPass.disabled = false; update(); }
});
