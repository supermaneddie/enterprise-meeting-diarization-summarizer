# enterprise-meeting-diarization-summarizer

A Flask portal and API that turns audio/video meeting recordings or pasted transcripts into a live transcript and structured meeting analysis. Jobs run through a LangGraph workflow and persist status in SQLite. Audio inspection is exposed as an MCP stdio server and uses the same tool implementations in the graph.

## Configuration

Copy the example configuration before starting the application:

```bash
cp config_example.yml config.yml
```

Edit `config.yml` with your service addresses, authorized LDAP usernames, and credentials. The example contains placeholders; `config.yml` is excluded from Git and Docker builds. Mount it at `/app/config.yml` when running Docker.

## Real-time microphone transcription

Choose **Start real-time transcript** on the home page. The page provides a title, microphone waveform, Start/Resume and Pause controls, an incoming-text preview, and an editable transcript. Use HTTPS or localhost and a browser supporting AudioWorklet with a 16 kHz AudioContext. The browser audio engine resamples microphone input before capture.

Audio travels through the application: **browser → Flask WebSocket relay → vLLM `/v1/realtime`**. The browser does not contact the model directly or receive its credentials. The relay uses the configured ASR URL, model, API key, and TLS verification setting. It derives the WebSocket URL from the ASR base URL (`https://host/v1` becomes `wss://host/v1/realtime`). There is no silent fallback to independent HTTP chunks.

The model must be served with realtime support. For Qwen3-ASR on compatible vLLM builds, add `--hf-overrides '{"architectures":["Qwen3ASRRealtimeGeneration"]}'` to the server launch command. Verify both `/v1/realtime` and `/v1/audio/transcriptions`, since saved recordings use the latter for final processing. The verified deployment is vLLM `0.22.1+7b9cb5b7.dev` with `Qwen/Qwen3-ASR-1.7B`. This backend can repeat its last audio chunk when an upstream session spans its internal five-second boundary. The relay limits each upstream session to 4.8 seconds, then closes it and opens a fresh session for subsequent audio. Text arrives after a pause or this limit; sending 100 ms audio frames does not guarantee subsecond transcription.

The relay uses WebRTC speech detection with 200 ms of pre-roll and 600 ms of trailing silence. It finalizes utterances at pauses or after 4.8 seconds of audio, including pre-roll and trailing silence. Continuous speech is split without overlapping or replaying audio; words at forced boundaries may lose context. This filters prolonged silence from the live model input while preserving the full recording independently in the browser. Detection can still miss quiet speech or mistake noise for speech. Incoming text is provisional until the utterance finishes, then it is appended to the editable transcript without replacing earlier corrections. Qwen language headers are removed from the display. Text is not deduplicated by sentence matching, so intentional repeated speech and manual corrections are preserved.

**Pause** flushes the capture buffer and waits for the remaining live text. **Resume** opens a new streaming connection. Network/model errors preserve captured audio and available text; the session is marked incomplete. Resume reconnects for new audio without replaying old text. Save with final transcription enabled to recover missing live intervals. Bounded queues automatically stop recording if processing falls behind. The recording limit is the smaller of the upload limit and 128 MiB (with room reserved for form fields).

**Save as new job** saves the title, complete WAV recording excluding paused intervals, live model transcript, and edited transcript. **Run final transcription after saving** is checked by default. Saving is durable before background work begins, so the page can be closed after a successful save. Leaving before saving warns about edits and audio. Unsaved recordings remain in browser memory and cannot survive a browser crash.

The final pass uses the existing silence-aware splitting configuration and longer audio segments (currently 30–60 seconds) to provide more context. It runs without automatic meeting analysis or speaker diarization. It writes into a temporary directory and publishes `results-final.txt` only on success. Failure or cancellation preserves the saved transcript and recording. An unedited transcript is replaced automatically with a nonempty final result; manual corrections are preserved and the job page offers **Review final transcript** and **Use final transcript**. The page also exposes the saved live text and archived corrections. **Run / retry final transcription** can retry or request this pass later. The existing general **Retry** action remains a full retranscription and should not be used to preserve the active transcript.

Artifacts are `audio/microphone.wav`, `results-live.txt`, `results-edited.txt`, `results-final.txt` (after final processing), and `results.txt` (the active transcript used for analysis). Final transcription costs an additional ASR pass and can improve context, but is not a guarantee of greater accuracy or whole-meeting model memory.

Install the updated `requirements.txt` and restart the app. Flask-Sock supports the threaded development server and threaded Gunicorn workers; allocate enough threads for concurrent recording sessions and normal requests. When using an external reverse proxy, enable WebSocket upgrades and preserve the Host header. For example, in the relevant nginx location:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_set_header Host $http_host;
proxy_read_timeout 120s;
```

The microphone endpoints are `GET /realtime`, WebSocket `/realtime/stream`, and `POST /realtime/save`. The WebSocket accepts binary mono PCM16 at 16 kHz (at most 8192 bytes per message) and a JSON `{"type":"finish"}` command. It returns `ready`, `ack`, `partial`, `segment`, `done`, or `error` events. Browser connections must have a same-origin Origin header. Save accepts multipart `name`, `audio`, `text`, `live_text`, `edited`, `incomplete`, and `final_transcription`; boolean fields use `true`/`false`. The former `/realtime/transcribe` chunk endpoint has been removed.

Focused regression checks:

```bash
PYTHONPATH=transcription python -m unittest discover -s transcription/tests -p 'test_realtime.py'
node transcription/tests/realtime.test.mjs
```

## Run

### LDAP authentication

All HTTP routes (including APIs, downloads, static files, and the WebSocket
handshake) require a signed login session, except `/login`.
Browser page requests redirect to the login form; unauthenticated API requests
return HTTP 401. Successful login grants access to the shared transcription
workspace; this does not introduce per-user job ownership.

Configure the directory in `config.yml` and restart the service:

```yaml
authorized_users:
  - alice
  - bob

ldap:
  server: ldap://ldap.abc.com
  port: 389
  base_dn: ou=People,dc=abc,dc=com
  user_dn: "uid={username},{base_dn}"
```

Replace the example usernames with the LDAP usernames allowed to access the
portal. Matching is exact and case-sensitive. An empty or missing
`authorized_users` list denies all portal logins. The allowlist is checked before
LDAP login and on every protected request, including existing sessions. Restart
the service after editing `config.yml` to apply changes. Saving model settings
preserves the allowlist.

Login performs an LDAP simple bind with the supplied password and formatted
user DN. Usernames are escaped using
[`ldap3.utils.dn.escape_rdn`](https://ldap3.readthedocs.io/en/latest/connection.html).
Empty passwords and failed/unavailable LDAP connections do not create sessions.
Passwords are never saved in the session. Select **Remember me** to stay signed
in across browser restarts. Set the top-level `auto_login_expiry` in `config.yml`
to a positive integer number of days (default: `7`) and restart the service to
apply it. Without **Remember me**, the cookie lasts for the browser session.
All session cookies are rejected after the configured expiry; activity does not
extend it. The navigation bar provides a **Sign out** button. JSON login clients
can opt in with `"remember_me": true`.

Set `TRANSCRIPTION_SECRET_KEY` to a strong random secret shared by all web
workers, or set `server.secret_key` in your private configuration. The environment
variable takes precedence. When neither is set, the app generates a random key
once and stores it in `.session-secret` inside `server.data_dir` (or `DATA_DIR`).
Remembered logins use signed client cookies and survive server restarts as long
as that key stays the same. Persist the data directory across container replacements;
workers sharing it use the same key. Separate hosts must use the same configured
secret. Changing the key invalidates existing cookies. Users must sign in once
after upgrading from the old randomly generated startup keys.
The old shipped key has been removed because it must not sign
authentication cookies. Serve the portal over HTTPS; HTTPS deployments can set
Flask's `SESSION_COOKIE_SECURE=True`. The requested `ldap://` configuration sends
the LDAP bind over plaintext; use a trusted network or `ldaps://` with the
appropriate port and a trusted server certificate.

API clients can log in using JSON, save the cookie, and send it on every request:

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  --data-binary @credentials.json https://transcription.example.com/login
curl -b cookies.txt https://transcription.example.com/api/jobs/JOB_ID
curl -b cookies.txt -H 'Content-Type: application/json' -d '{}' \
  https://transcription.example.com/logout
```

`credentials.json` contains `{"username":"your-user","password":"your-password"}`.
Keep that file and the cookie jar private.

The separate MCP server uses local stdio and has no HTTP login endpoint.

### Start the service

Python 3.11+, `ffmpeg`, and access to the configured OpenAI-compatible model endpoints are required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Application logs are written to stderr at `INFO` level by default. Set the
`DEBUG` environment variable to `true` (case insensitive, e.g. `DEBUG=TRUE python run.py`)
to enable `DEBUG` logging. Any other value keeps `INFO` logging. This also applies
to the standalone MCP server and does not enable Flask's debug mode.

Without speaker diarization, choose `asr.vad-strategy` in `config.yml`: `energy-based` (default) uses audio energy to detect silence; `silero-vad` uses Silero speech detection on CPU. Both preserve the full recording and respect `asr.max_chunk_seconds` (60 seconds by default), preferring pause boundaries after `asr.min_chunk_seconds`. Continuous speech or recordings without detected speech are split at the maximum duration when no suitable pause is available. This setting does not affect speaker diarization. Install the optional dependencies before selecting `silero-vad` (missing dependencies cause an explicit error):

```bash
pip install -r requirements-asr.txt
```

### Markdown analysis assets

Analysis renders Markdown using [Marked](https://marked.js.org/), with HTML sanitized by [DOMPurify](https://github.com/cure53/DOMPurify). Initial results, polling updates, and speaker-name changes share the renderer. Use the Analysis display selector to switch between **Markdown** and **Plain text**; updates retain the selected mode. **Copy** preserves the original Markdown in either mode; saved `summary.txt` files are unchanged.

The local browser bundle is included in `transcription/static`, so running Flask does not require Node.js or a CDN. To rebuild it, install Node.js 22.13+ (22.x) or 24+ and run from the repository root:

```bash
npm --prefix transcription/lib ci
npm --prefix transcription/lib run build
npm --prefix transcription/lib test
```

`transcription/lib/package.json` and `package-lock.json` pin dependencies downloaded into `transcription/lib/node_modules`. The build bundles `lib/markdown.js` into `static/markdown.min.js` and emits its third-party license notices alongside it. Commit both generated files after dependency or renderer changes. Presentation styles are in `static/markdown.css`.

### Optional speaker diarization and voiceprints

Standard vLLM Qwen3-ASR transcription does not assign speakers. To detect speakers locally, install the diarization extras:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-diarization.txt
```

Then accept the conditions for `pyannote/speaker-diarization-community-1`, create a Hugging Face access token, and configure the `diarization` section in `config.yml`:

```yaml
diarization:
  enabled: true
  model: pyannote/speaker-diarization-community-1
  huggingface_token: hf_your_token
  device: cpu
  min_speakers: null
  max_speakers: null
  merge_gap_seconds: 0.75
```

The model runs on CPU by default; no local GPU is required. Its first run downloads model files and can take considerably longer than VAD-only processing. Detected turns are written as `[Speaker 1] ...`. Once the job finishes, use **Name the speakers** on the detail page to map those labels to real names. Names are stored in `speakers.json`; raw transcript and summary files retain their original speaker labels.

For diarized jobs, **Show timeline** in the Transcript panel displays each chunk’s detected start time as `HH:MM:SS.mmm`, relative to its source audio file. Copy includes timestamps while the timeline is shown; hide it to edit the transcript. Timings are saved in `timeline.json` as chunks complete. Older jobs require a retry to generate timings. If transcript edits no longer match the saved chunks, the timeline is unavailable until the original text is restored or the job is retried.

The top-level `timeline_pattern` in `config.yml` controls timestamp formatting in
the portal. Its default is
`"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"`, using integer
Python format fields. For example, `"{hours:02d}:{minutes:02d}:{seconds:02d}"`
omits milliseconds. Each formatted timestamp appears in brackets before its
chunk text; source headings and whitespace between chunks are preserved.

The same optional requirements install SpeechBrain and FAISS for local voiceprints. The default `speechbrain/spkrec-ecapa-voxceleb` ECAPA-TDNN model extracts learned filterbank speaker embeddings. For every diarized speaker, the pipeline retains the turn with the most usable speech and strongest signal in `speaker-samples/`; temporary ASR chunks are still removed. Enter a speaker name and click **Add** to enroll that sample. A person can have voiceprints from multiple jobs. Embeddings and person metadata are stored in `voiceprints/voiceprints.sqlite3`, while FAISS stores a cosine-search index of the normalized average (centroid) for each person.

Open **Voiceprints** from the top navigation to manage stored samples, grouped in expandable speaker lists. Delete individual samples or add one for an existing or new speaker by uploading audio or using **Start microphone**. Choose a passage language (Chinese by default, or English) and click **Regenerate** for a different passage to read aloud. Recording asks for microphone access, and provides playback before saving. Browser recording requires HTTPS or localhost. Supply 3–60 seconds of clear speech from one speaker (15–30 seconds recommended); audio is converted to mono 16 kHz for enrollment and temporary uploads are removed. Only embeddings and metadata are stored by this page. Deleting a sample invalidates its model’s recognition index, which is rebuilt from remaining samples on the next search. Viewing and deleting remain available when enrollment is disabled.

Enable **Voiceprint recognition** together with **Speaker diarization** when submitting a job. After diarization and before chunk cleanup, each clearest speaker turn is compared with saved speakers using the selected recognition strategy. The highest match fills the editable speaker-name dropdown only when its score is greater than or equal to `voiceprint.match_threshold` (default `0.4`), and up to five candidates are offered. Set the automatic match threshold on the model settings page or configure it with:

```yaml
voiceprint:
  enabled: true
  model: speechbrain/spkrec-ecapa-voxceleb
  match_threshold: 0.4
  recording_languages: [English, Chinese]
  recording_default_language: Chinese
  recognition_strategy: centroid  # centroid, best_match, template_update
```

On the **Voiceprints** page, expand a speaker and use **Save name** to rename them. Names must be unique ignoring case. Samples remain attached to the same speaker; future recognition uses the new name, while existing meeting transcripts retain their saved names.

Choose a **Recognition strategy** and click **Save strategy**. The choice is persisted in `config.yml` and captured for new jobs:

- **Average / Centroid** (default): compare the input against each speaker’s normalized average embedding using the persisted FAISS index.
- **Best Match**: compare against every saved sample and rank each speaker by their highest cosine similarity. This handles varied samples but is more sensitive to outliers.
- **Template Update**: compare against a persistent adaptive template, initialized from the speaker’s centroid. After a match scores at least `max(0.80, match_threshold)` and leads the next speaker by at least `0.05`, update the template to the normalized blend of 90% previous template and 10% new embedding. Original samples are unchanged. Adding or deleting samples resets adaptive templates for that model. Confident incorrect matches can affect later recognition; repeated recognition of the same audio can adapt the template again.

All strategies return up to five distinct speakers and use `match_threshold` for assigning a name. Best Match and Template Update read model-specific embeddings from SQLite and score them with NumPy.

The first extraction downloads the SpeechBrain model. Speaker samples are decoded with the existing FFmpeg/pydub path and passed to SpeechBrain as in-memory tensors, so voiceprint extraction does not depend on TorchCodec's binary decoder. Treat the voiceprint database and retained speaker samples as sensitive biometric data and protect or delete the data volume according to your retention policy.

For GPU diarization, install a CUDA-enabled PyTorch build compatible with the host's NVIDIA driver and verify `torch.cuda.is_available()` is `True`. Set `device` to `cuda` or `cuda:0` for one GPU. For multiple GPUs, use a list such as `cuda:0,2`. Each job is assigned one available configured GPU; the process-local scheduler prevents concurrent jobs from sharing the same GPU and distributes jobs round-robin. A single job is not split across GPUs. The application does not automatically fall back to CPU: an unavailable device, invalid index, or CUDA runtime failure marks the job as failed with the reason in its log.

Diarization also supports passwordless SSH targets in the same `diarization.device` setting (or the model settings page):

| Device | Execution |
| --- | --- |
| `cpu` | Local CPU |
| `cuda` / `cuda:0` | Local GPU 0 |
| `cuda:0,2` | Local GPU pool (one GPU per job) |
| `remote-cpu:user@host` | Remote CPU |
| `remote-cuda:user@host` | Remote GPU 0 |
| `remote-cuda:0:user@host` | Remote selected GPU (any nonnegative index) |

`remote-cuda0:user@host` and the compatibility spelling `remove-cuda0:user:host` are also accepted. Usernames retain their case. A backslash before `@` is accepted, but is unnecessary in YAML; use plain or single-quoted values. Remote GPU leases are per host and index within this application process. Remote CPU jobs can run concurrently.

The application host needs OpenSSH `ssh` and `scp`, a trusted SSH host key, and passwordless access from the account running the app. The remote account's noninteractive `python3` must have `pyannote.audio>=4.0,<5`, NumPy, PyTorch, and pydub installed; FFmpeg must be on its PATH. For remote CUDA, install a compatible CUDA PyTorch build and NVIDIA driver on that host. Accept the configured Hugging Face model's conditions; the remote host needs access to download it on first use. The application does not install remote dependencies automatically. Local pyannote/PyTorch are unnecessary for remote diarization; optional voiceprint recognition/enrollment still requires its local dependencies and runs on local CPU when a remote target is selected.

To select a private key, set `diarization.ssh_identity_file` (also available on the model settings page):

```yaml
diarization:
  device: remote-cuda:user@host
  ssh_identity_file: /home/app/.ssh/id_rsa
```

The path is on the application host and is passed as `-i` to every SSH and SCP command, including cleanup. `~` expands to the application account's home; relative paths resolve from the application's working directory. The key stays local. Leave the setting empty to use SSH's default identities/configuration. RSA and other OpenSSH-supported key types work. Since connections use batch mode, encrypted keys must already be unlocked through an SSH agent accessible to the application. In Docker, mount the key and use its path inside the container.

On application startup, a configured remote diarization target is checked with `ssh -T -o BatchMode=yes ... true`, using the same identity file as jobs. The probe has a 30-second timeout and never prompts for a password. Success is logged at INFO level; authentication, host-key, or connection failures are logged as warnings with details. The app remains available so you can correct settings or view existing results. Local device configurations skip the check. This checks SSH connectivity only; it does not load a model or verify remote Python dependencies.

Each remote invocation creates a private `/tmp/transcription-diarization-<unique-id>` directory, copies the standalone `transcription/diarization_worker.py`, the recording, and a JSON request containing only diarization settings (including the Hugging Face token and chunk duration). It runs the worker through SSH and downloads `results.tar.gz`, containing WAV chunks and a manifest of speaker labels and original start times. The token is transferred in a private file, never as a command argument. Temporary request/audio/result files are cleaned up after success, failure, or cancellation; cleanup failures are logged if the remote host is unreachable. Downloaded model caches remain on the remote host. Closing the SSH connection stops the worker through its stdin watchdog.

The worker can also run independently, without the application package:

```bash
python3 diarization_worker.py --request request.json --source recording.mp3 --output output
```

`request.json` accepts `diarization_device` (local `cpu`/`cuda`/`cuda:N`), `diarization_model`, `diarization_hf_token`, `diarization_min_speakers`, `diarization_max_speakers`, `diarization_merge_gap_seconds`, and `asr_max_chunk_seconds`. All fields are optional; defaults match the application. For example: `{"diarization_device": "cpu", "diarization_hf_token": "hf_your_token"}`. Output includes `chunks/`, `manifest.json`, and `results.tar.gz`.


Audio is decoded through FFmpeg/pydub and passed to pyannote as an in-memory waveform. This avoids relying on the optional `torchcodec` decoder and its Torch/FFmpeg binary compatibility.

Set `server.worker_count` to at least the number of GPUs if you want that many jobs to run concurrently.

The Hugging Face token is a secret stored in `config.yml`, so restrict that file's permissions and do not commit a real token.

Open `http://localhost:8090` with the current `server.port` setting. Server, ASR, analysis, prompt, chunking, and storage settings live in `config.yml`. The Model settings page writes model changes back to that file. API callers can also supply per-job model fields alongside a multipart `/voice` request or JSON `/text` request. Set `TRANSCRIPTION_CONFIG` only when you need to load a YAML file from a different location; `DATA_DIR` can override the storage location in containers.

## API

```bash
curl -H 'Accept: application/json' -F 'name=Weekly product sync' -F 'files=@meeting.mp3' -F 'auto_analysis=true' http://localhost:8090/voice
curl -H 'Content-Type: application/json' -d '{"name":"Weekly product sync","url":"https://example.com/meeting.mp3"}' http://localhost:8090/voice
curl -H 'Content-Type: application/json' -d '{"name":"Weekly product sync","text":"Meeting transcript..."}' http://localhost:8090/text
curl http://localhost:8090/api/jobs/JOB_ID
curl -X POST -H 'Content-Type: application/json' -d '{"transcript":"Corrected transcript...","action":"regenerate"}' http://localhost:8090/jobs/JOB_ID/transcript
```

Both frontpage forms include a collapsed, optional **Brief Description** field (up to 4,000 characters). Describe participants, meeting purpose, project names, or terminology—for example, “This is a presales meeting between Client XX and ABC Company about the POC of the XXX project.” The description supplements the configured ASR and analysis prompts as background context; it is not added to the transcript. Edit and save it above **Files** in job details. The next retry or summary regeneration uses the saved description; a run already in progress keeps its original context.

**Auto analysis** is checked by default for recordings. Uncheck it to finish with a transcript, then use **Save & regenerate summary** after reviewing the text. JSON `/voice` requests default to automatic analysis and accept `auto_analysis: false`; form requests enable it with `auto_analysis=true`. Both `/voice` and `/text` accept `brief_description`. Update it with `POST /jobs/<job_id>/brief-description` using the same field (an empty string clears it). Older jobs retain automatic analysis and start with an empty description.

Accepted voice media extensions are configured by the comma-separated `asr.supported_format` string in `config.yml`, defaulting to `"WAV,MP3,FLAC,OGG,M4A,AAC,MP4,MPEG,MPGA,WEBM"` when omitted. Matching is case-insensitive; spaces and optional leading dots are accepted. `.zip`, `.tar`, and `.tar.gz` archives are always accepted in addition to the configured media formats. Inputs can be uploaded files or one HTTP/HTTPS URL. A request must use exactly one input method. URL downloads run asynchronously as a LangGraph step, are streamed and limited by `server.max_upload_mb`, and report integer-percentage progress in `langgraph.log`. Only archive entries matching the configured media extensions are extracted. Audio decoding (including audio tracks from video containers) requires FFmpeg; browser playback depends on browser codec support. Archive member paths, expanded bytes, and entry counts are validated. Inputs are staged only while they are prepared and are then removed; retained recordings live exclusively in `results/<job_id>/audio`. The voice form offers **Adjust volume** with **Auto** (peak normalization to −1 dBFS without diarization; with diarization, speaker chunks are adjusted after detection toward −20 dBFS RMS, with a maximum 20 dB boost and −1 dBFS peak ceiling) or +10% through +100% and −10% through −100%, in 10% steps. Percentages scale the original amplitude: +100% doubles it and −100% produces silence; boosting loud audio can clip. API requests use `adjust_volume=true` and `volume=auto` (default), or a signed percentage such as `volume=+20%`. With diarization and Auto, adjusted chunks are used for transcription and assembled into playback at their original timestamps, preserving pauses and trimming overlapping boundary padding. Peak limits may prevent very dynamic chunks from reaching the average target; this does not remove background noise or separate overlapping voices. Adjustment is off by default and creates `.volume-adjusted.mp3` copies. Volume adjustment runs asynchronously, so submission does not wait for FFmpeg. Both original and adjusted recordings remain playable, while ASR and diarization process only the adjusted copies. This feature requires FFmpeg. Speaker diarization can also be selected per voice job; the option is available only when `diarization.enabled` is `true` in `config.yml`. Voiceprint recognition becomes selectable only after diarization is selected and when `voiceprint.enabled` is true. Results are written incrementally to:

```text
results/<job_id>/results.txt
results/<job_id>/summary.txt
results/<job_id>/langgraph.log
```

The job detail page allows the transcript to be corrected after processing finishes. **Save transcript** replaces `results.txt` without changing the existing analysis. **Save & regenerate summary** saves it and queues a new analysis-only LangGraph run using the updated transcript.

**Remove audio file after completed** is unchecked by default in the UI. `/voice` accepts `remove_audio_after_completed=true` (JSON boolean or form field), defaulting to `false`. After successful processing, this option removes job-local originals and adjusted copies for both file and URL inputs. Staging uploads and processing chunks are always removed when the pipeline exits, including on failure or cancellation, regardless of this option. Failed or stopped runs retain their recordings. Transcripts, analysis, logs, and speaker samples remain available. Remote files are never deleted. With removal disabled, originals and adjusted copies are retained. URL originals play from the remote URL, and URL retries download again. Uploaded recordings removed after completion must be uploaded again for retranscription; summary regeneration still works.

Set `asr.ignore_duration_seconds` in `config.yml` to skip chunks shorter than that duration before calling ASR (default: `1` second; `0` disables filtering). Fractional seconds are supported, and chunks exactly at the threshold are transcribed. Skipped chunks produce no transcript or timeline entry. If every chunk is skipped, the transcript is empty.

The ASR API is called through `POST /audio/transcriptions`; the analysis API uses streaming chat completions. `asr.timeout_seconds` limits an individual HTTP attempt, while `asr.chunk_deadline_seconds` limits the total wall-clock time for a chunk including retries. `verify_ssl=false` is supported for both, though TLS verification should remain enabled for production HTTPS services.

Run the MCP audio reader separately when an external MCP client needs it:

```bash
python -m transcription.mcp_server
```

## Production notes

The included runner uses an in-process thread pool, which is appropriate for a single Flask process. For multiple web workers or crash-resumable production jobs, replace it with a durable worker queue. Stop is cooperative: an in-flight HTTP request cannot be forcibly interrupted, but processing stops before the next chunk/token. A chunk that fails after retries or exceeds its hard deadline is recorded as `[Transcription Failed]`, and processing continues; its underlying request thread may remain until the network operation returns or reaches its HTTP timeout. Keep the data volume private because transcripts and recordings can contain sensitive information.

## Docker

```bash
docker build -t enterprise-meeting-diarization-summarizer .
docker run --gpus all --shm-size=4g \
  -p 8090:8090 \
  -v meeting-data:/data \
  -v huggingface-cache:/root/.cache/huggingface \
  -v "$(pwd)/config.yml:/app/config.yml" \
  enterprise-meeting-diarization-summarizer
```

The image inherits from the official `qwenllm/qwen3-asr:latest` CUDA image and installs both `requirements-asr.txt` and `requirements-diarization.txt`. NVIDIA Container Toolkit is required when `diarization.device` uses CUDA. For CPU diarization, omit `--gpus all`; the same image still runs, although it is substantially larger than a CPU-only image.


The container also starts OpenSSH on port 22, with root login through public-key authentication. Mount an existing file containing your public key(s) into `/root/.ssh/authorized_keys` at runtime:

```bash
docker build -t enterprise-meeting-diarization-summarizer .
docker run --gpus all --shm-size=4g \
  -p 10022:22 -p 8090:8090 \
  -v /absolute/path/authorized_keys:/root/.ssh/authorized_keys:ro \
  -v meeting-data:/data \
  -v huggingface-cache:/root/.cache/huggingface \
  enterprise-meeting-diarization-summarizer
```

No authorized keys are embedded during the build. Use an absolute source path to an existing file. At startup, the container copies `/root/.ssh/authorized_keys` to `/run/sshd/root-authorized_keys`, owned by root with mode 0600, and SSH uses that copy. The mounted source stays untouched, so read-only mounts and host ownership such as UID 1013 work without changing host permissions. Restart the container after updating the mounted public keys to refresh the copy. SSH host keys are generated on first container startup. Only root SSH login is allowed, and password authentication is disabled.

For a container used only as a remote worker, append `sleep infinity` to the `docker run` command to keep SSH running without starting the web application. On the application host, configure:

```yaml
diarization:
  enabled: true
  device: remote-cuda:root@192.168.130.33
  ssh_port: 10022
  ssh_identity_file: /home/app/.ssh/id_rsa
```

The SSH port defaults to 22 and is used by the startup check, worker execution, SCP transfers, and cleanup. Establish trust in the container's host key from the application account, for example with `ssh -p 10022 -i /home/app/.ssh/id_rsa root@192.168.130.33 true`. Recreating the container generates a new host key unless `/etc/ssh` is persisted.

### Serving under a reverse-proxy path

Application links, redirects, forms, static resources, and real-time endpoints
use request-relative URLs. The proxy must strip the mount prefix (such as
`/transcription/`) before forwarding requests to Flask. Redirect the bare mount to its
trailing-slash URL so browsers resolve relative links inside the mount:

```nginx
location = /transcription {
    return 308 /transcription/$is_args$args;
}
location /transcription/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 120s;
}
```

For HTTPS terminated at nginx, enable `server.trust_proxy_proto: true` in
`config.yml` and restart transcription (enabled in the supplied configuration).
This trusts one proxy's `X-Forwarded-Proto` value so login and other origin checks
use the browser-facing scheme. nginx must overwrite that header as shown above,
and the upstream should only be reachable through that trusted proxy. The
setting defaults to `false` when omitted; use `false` for direct deployments.
Other forwarded headers are not trusted by this setting.

The trailing `/` in `proxy_pass http://127.0.0.1:8090/;` is required:
nginx replaces the matching `/transcription/` prefix with `/`. Without that
trailing slash, Flask receives `/transcription/`, which is not an application
route. A login redirect such as `./../login?next=/transcription/` indicates
that the prefix reached Flask. With the configuration above, the redirect is
`./login?next=/`, which the browser resolves to `/transcription/login?next=/`.
The same prefix replacement is required for login, static assets, APIs, and
WebSocket requests; changing only the login redirect does not fix routing.

Adjust the upstream port to your server. JSON response URLs (including
`login_url` and newly created job URLs) are relative to the **API request URL**;
clients should resolve them against that URL, as with
`new URL(result.url, response.url)` in JavaScript.
