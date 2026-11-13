const $ = (id) => document.getElementById(id);

const localVideo = $('localVideo');
const remoteVideo = $('remoteVideo');
const localTile = $('localTile');
const remoteTile = $('remoteTile');
const state = {
  room: '', client: '', name: '', media: null, screen: null, pc: null,
  joined: false, cursor: 0, pollTask: null, audioTask: null, frameTask: null,
  callStartedAt: null, timerTask: null, toastTask: null, warningTotal: 0,
  pendingCandidates: [], remoteParticipant: false, moderationConfigured: false,
  localRemoteStream: null, reportingErrors: new Set(), currentScreenSender: null,
};

function displayName(name) {
  const value = String(name || '').trim();
  return value ? value.replace(/[<>\u0000-\u001f]/g, '').slice(0, 40) : 'Guest';
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  let body = {};
  try { body = await response.json(); } catch {}
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function postJson(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function setHint(message, isError = false) {
  $('mediaHint').textContent = message;
  $('mediaHint').classList.toggle('error', isError);
}

function updateCallTime() {
  if (!state.callStartedAt) return;
  const seconds = Math.floor((Date.now() - state.callStartedAt) / 1000);
  $('callTimer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function setCallState(label, connected = false) {
  $('callState').textContent = label;
  $('statusLight').classList.toggle('connected', connected);
  $('connectionPill').classList.toggle('connected', connected);
  $('connectionText').textContent = connected ? 'Connected' : label;
}

function initials(name) {
  return displayName(name).split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join('') || '?';
}

function setRemoteParticipant(name, connected = false) {
  state.remoteParticipant = Boolean(connected);
  const safeName = displayName(name);
  $('remoteName').textContent = connected ? safeName : 'Waiting for someone to join';
  $('remoteTileName').textContent = connected ? safeName : 'Guest';
  $('remoteAvatar').textContent = connected ? initials(safeName) : '?';
  $('remoteCaption').textContent = connected ? 'Participant in this room' : 'Share your invite link to connect a second person';
  $('participantCount').textContent = connected ? '2/2' : '1/2';
  $('waitingBanner').hidden = connected;
  remoteTile.classList.toggle('has-stream', Boolean(remoteVideo.srcObject));
}

function setMonitoring(configured, detail = '') {
  state.moderationConfigured = Boolean(configured);
  const status = $('monitoringStatus');
  status.classList.toggle('unavailable', !configured);
  status.classList.toggle('active', configured);
  status.innerHTML = `<i></i> ${configured ? 'Active' : 'Unavailable'}`;
  $('monitorStatusTitle').textContent = configured ? 'AI checks are ready' : 'AI checks unavailable';
  $('monitorStatusDetail').textContent = configured
    ? 'Chat, audio, and sampled frames are checked by the hosted service.'
    : detail || 'Add an OpenAI API key on the server to enable moderation.';
  $('monitorStatusCard').classList.toggle('monitor-enabled', configured);
}

function resetDashboard() {
  state.warningTotal = 0;
  state.reportingErrors.clear();
  $('warningCount').hidden = true;
  $('warningCount').textContent = '0';
  $('chatMessages').replaceChildren();
  const chatEmpty = document.createElement('div');
  chatEmpty.id = 'chatEmpty';
  chatEmpty.className = 'chat-empty';
  chatEmpty.innerHTML = '<div class="empty-icon">···</div><strong>Start the conversation</strong><span>Messages in this room appear here.</span>';
  $('chatMessages').append(chatEmpty);
  $('warningList').replaceChildren();
  const warningEmpty = document.createElement('div');
  warningEmpty.id = 'warningEmpty';
  warningEmpty.className = 'warning-empty';
  warningEmpty.innerHTML = '<span class="warning-empty-icon">✓</span><strong>No warnings</strong><span>Potential concerns will appear here as private reminders.</span>';
  $('warningList').append(warningEmpty);
  $('warningToast').hidden = true;
  showTab('chat');
}

async function loadConfig() {
  try {
    const config = await api('/api/config');
    setMonitoring(config.moderationConfigured);
  } catch {
    setMonitoring(false, 'The call server could not be reached. Refresh the page to retry.');
  }
}

function setMicState(enabled) {
  $('micButton').classList.toggle('enabled', enabled);
  $('micButton').setAttribute('aria-pressed', String(enabled));
  $('micLabel').textContent = enabled ? 'Mic on' : 'Muted';
  $('localMuteMark').hidden = enabled;
}

function setCameraState(enabled) {
  $('cameraButton').classList.toggle('enabled', enabled);
  $('cameraButton').setAttribute('aria-pressed', String(enabled));
  $('cameraLabel').textContent = enabled ? 'Camera on' : 'Camera off';
  $('localPlaceholder').classList.toggle('camera-muted', enabled);
  localTile.classList.toggle('has-stream', Boolean(enabled || state.screen));
  $('localPlaceholder').hidden = Boolean(enabled || state.screen);
}

function makePc() {
  if (state.pc) return state.pc;
  const pc = new RTCPeerConnection({
    iceServers: [{ urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] }],
  });
  state.pc = pc;
  state.localRemoteStream = new MediaStream();
  remoteVideo.srcObject = state.localRemoteStream;

  for (const track of state.media.getTracks()) {
    const usingScreen = track.kind === 'video' && state.screen?.getVideoTracks()[0];
    const sendTrack = usingScreen || track;
    const sender = pc.addTrack(sendTrack, usingScreen ? state.screen : state.media);
    if (track.kind === 'video') state.currentScreenSender = sender;
  }
  pc.ontrack = (event) => {
    const stream = event.streams[0];
    if (stream) remoteVideo.srcObject = stream;
    else state.localRemoteStream.addTrack(event.track);
    $('remotePlaceholder').hidden = true;
    remoteTile.classList.add('has-stream');
    remoteVideo.play().catch(() => {});
  };
  pc.onicecandidate = (event) => {
    if (event.candidate) sendSignal('ice', event.candidate.toJSON()).catch(() => {});
  };
  pc.onconnectionstatechange = () => {
    if (!state.joined) return;
    if (pc.connectionState === 'connected') {
      setCallState('In call', true);
      setHint('Live call active. Chat and short media samples are checked for possible concerns. Warnings are private to you.');
    } else if (pc.connectionState === 'failed') {
      setCallState('Connection issue');
      setHint('The peer connection could not be completed. A TURN relay may be needed on this network.', true);
    } else if (pc.connectionState === 'disconnected') {
      setCallState('Reconnecting');
    }
  };
  return pc;
}

async function sendSignal(type, payload) {
  await postJson('/api/signal', { room: state.room, client: state.client, type, payload });
}

async function makeOffer() {
  if (!state.joined || !state.remoteParticipant) return;
  const pc = makePc();
  if (pc.signalingState !== 'stable') return;
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await sendSignal('offer', pc.localDescription.toJSON());
  setCallState('Connecting');
}

async function setRemoteDescription(description) {
  const pc = makePc();
  await pc.setRemoteDescription(description);
  const candidates = state.pendingCandidates.splice(0);
  for (const candidate of candidates) await pc.addIceCandidate(candidate);
}

async function handleCallSignal(event) {
  if (event.type === 'peer_joined') {
    setRemoteParticipant(event.data.name, true);
    setCallState('Connecting');
    try { await makeOffer(); } catch { setHint('Could not start the call connection. Try leaving and rejoining.', true); }
    return;
  }
  if (event.type === 'peer_left') {
    state.remoteParticipant = false;
    if (state.pc) state.pc.close();
    state.pc = null;
    state.pendingCandidates = [];
    remoteVideo.srcObject = null;
    $('remotePlaceholder').hidden = false;
    setRemoteParticipant('', false);
    setCallState('Waiting for participant');
    setHint('The other person left. You can keep the invite link open for someone else.');
    return;
  }
  if (event.type === 'offer') {
    await setRemoteDescription(event.data);
    const pc = state.pc;
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    await sendSignal('answer', pc.localDescription.toJSON());
    setCallState('Connecting');
    return;
  }
  if (event.type === 'answer') {
    await setRemoteDescription(event.data);
    return;
  }
  if (event.type === 'ice') {
    const pc = makePc();
    if (!pc.remoteDescription) state.pendingCandidates.push(event.data);
    else await pc.addIceCandidate(event.data);
  }
}

function addChatMessage(name, message, mine, timeText) {
  $('chatEmpty')?.remove();
  const item = document.createElement('article');
  item.className = `chat-message${mine ? ' mine' : ''}`;
  const meta = document.createElement('div');
  meta.className = 'message-meta';
  const sender = document.createElement('strong');
  sender.textContent = mine ? 'You' : displayName(name);
  const time = document.createElement('time');
  time.textContent = timeText || new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const bubble = document.createElement('p');
  bubble.textContent = message;
  meta.append(sender, time);
  item.append(meta, bubble);
  $('chatMessages').append(item);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
}

function addWarning(data, createdAt) {
  $('warningEmpty')?.remove();
  state.warningTotal += 1;
  const counter = $('warningCount');
  counter.hidden = false;
  counter.textContent = String(state.warningTotal);
  const card = document.createElement('article');
  card.className = 'warning-item';
  const icon = document.createElement('span');
  icon.className = 'warning-item-icon';
  icon.textContent = '!';
  const copy = document.createElement('div');
  copy.className = 'warning-item-copy';
  const header = document.createElement('div');
  header.className = 'warning-item-header';
  const title = document.createElement('strong');
  const titles = {
    potentially_harmful_language: 'Possible language concern',
    chat_message: 'Possible chat concern',
    explicit_visual: 'Possible visual concern',
    visible_injury: 'Possible injury signal',
  };
  title.textContent = titles[data.kind] || 'Possible concern';
  const time = document.createElement('time');
  time.textContent = new Date(createdAt || Date.now()).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const message = document.createElement('p');
  message.textContent = data.message;
  header.append(title, time);
  copy.append(header, message);
  card.append(icon, copy);
  $('warningList').prepend(card);
  showWarningToast(data.message);
}

function showWarningToast(message) {
  $('toastMessage').textContent = message;
  $('warningToast').hidden = false;
  if (state.toastTask) window.clearTimeout(state.toastTask);
  state.toastTask = window.setTimeout(() => { $('warningToast').hidden = true; }, 9000);
}

async function handleEvent(event) {
  if (['peer_joined', 'peer_left', 'offer', 'answer', 'ice'].includes(event.type)) {
    try {
      await handleCallSignal(event);
    } catch {
      setHint('A call signaling message could not be processed. Rejoin the room to retry.', true);
    }
  } else if (event.type === 'chat') {
    addChatMessage(event.data.name, event.data.text, event.from === state.client, event.createdAt);
  } else if (event.type === 'warning') {
    addWarning(event.data, event.createdAt);
  } else if (event.type === 'monitoring_error') {
    setMonitoring(false, 'A hosted AI check failed. The video call and chat can continue.');
    setHint('A moderation request failed. The call is still connected; review the server key or provider status.', true);
  }
}

async function pollEvents() {
  while (state.joined) {
    try {
      const query = new URLSearchParams({ room: state.room, client: state.client, since: String(state.cursor) });
      const result = await api(`/api/events?${query}`);
      for (const event of result.events) await handleEvent(event);
      state.cursor = Math.max(state.cursor, result.cursor);
    } catch (error) {
      if (state.joined) {
        setCallState('Reconnecting');
        await wait(1400);
      }
    }
    if (state.joined) await wait(450);
  }
}

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function addRoomToUrl() {
  const url = new URL(window.location.href);
  url.searchParams.set('room', state.room);
  window.history.replaceState({}, '', url);
}

async function joinCall(event) {
  event.preventDefault();
  const submit = $('joinSubmit');
  const errorBox = $('joinError');
  errorBox.hidden = true;
  submit.disabled = true;
  submit.innerHTML = 'Joining…';
  let stream = null;
  let joinedClient = null;
  try {
    const name = displayName($('displayName').value);
    const room = $('roomInput').value.trim().toUpperCase();
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
    const result = await postJson('/api/join', { room, name, consent: $('consentCheck').checked });
    joinedClient = result.client;
    state.room = result.room;
    state.client = result.client;
    state.name = name;
    state.media = stream;
    state.joined = true;
    resetDashboard();
    $('localName').textContent = name;
    $('localTileName').textContent = name;
    $('youAvatar').textContent = initials(name);
    $('roomCodeLabel').textContent = `ROOM · ${state.room}`;
    $('participantCount').textContent = result.peerPresent ? '2/2' : '1/2';
    addRoomToUrl();
    setMonitoring(result.moderationConfigured);
    localVideo.srcObject = stream;
    localVideo.play().catch(() => {});
    $('joinOverlay').hidden = true;
    $('leaveButton').disabled = false;
    $('endCallButton').disabled = false;
    $('shareButton').disabled = false;
    $('micButton').disabled = false;
    $('cameraButton').disabled = false;
    $('screenButton').disabled = false;
    $('chatInput').disabled = false;
    $('sendChatButton').disabled = false;
    setMicState(true);
    setCameraState(true);
    if (result.peerPresent) {
      setRemoteParticipant(result.peerName, true);
      setCallState('Connecting');
    } else {
      setRemoteParticipant('', false);
      setCallState('Waiting for participant');
    }
    state.callStartedAt = Date.now();
    state.timerTask = window.setInterval(updateCallTime, 1000);
    state.pollTask = pollEvents();
    state.audioTask = audioSamplingLoop();
    state.frameTask = frameSamplingLoop();
    setHint(state.moderationConfigured
      ? 'Live call active. Short audio and video samples plus chat are sent to OpenAI for checks. Warnings are private to you.'
      : 'Call is live. Add OPENAI_API_KEY on the server to enable hosted AI checks and warning messages.');
  } catch (error) {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (joinedClient) {
      try { await postJson('/api/leave', { room: state.room, client: joinedClient }); } catch {}
    }
    errorBox.textContent = error?.name === 'NotAllowedError'
      ? 'Camera or microphone access was denied. Allow access, then try again.'
      : error?.name === 'NotFoundError'
        ? 'This device could not find a camera and microphone.'
        : error.message || 'Could not join the room.';
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
    submit.innerHTML = 'Join room <span>→</span>';
  }
}

function supportedRecorderType() {
  if (!window.MediaRecorder) return '';
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

async function audioSamplingLoop() {
  if (!state.moderationConfigured) return;
  const mimeType = supportedRecorderType();
  if (!mimeType) {
    reportSamplingError('audio', 'This browser cannot record audio samples for moderation.');
    return;
  }
  while (state.joined && state.moderationConfigured) {
    const track = state.media?.getAudioTracks()[0];
    if (!track?.enabled) {
      await wait(700);
      continue;
    }
    await new Promise((resolve) => {
      let recorder;
      let startedWithMic = Boolean(track.enabled);
      const chunks = [];
      try {
        recorder = new MediaRecorder(new MediaStream([track]), { mimeType });
        recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
        recorder.onerror = () => resolve();
        recorder.onstop = async () => {
          try {
            if (state.joined && state.moderationConfigured && startedWithMic && chunks.length) {
              const blob = new Blob(chunks, { type: mimeType.split(';')[0] });
              await postMedia('/api/audio', blob, blob.type || 'audio/webm');
            }
          } catch (error) {
            reportSamplingError('audio', error.message);
          } finally { resolve(); }
        };
        recorder.start();
        window.setTimeout(() => {
          if (recorder.state !== 'inactive') {
            startedWithMic = startedWithMic && Boolean(track.enabled);
            recorder.stop();
          }
        }, 5000);
      } catch (error) {
        reportSamplingError('audio', 'Audio sample could not be captured in this browser.');
        resolve();
      }
    });
  }
}

async function frameSamplingLoop() {
  if (!state.moderationConfigured) return;
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', { alpha: false });
  while (state.joined && state.moderationConfigured) {
    await wait(5000);
    if (!state.joined || !state.moderationConfigured) return;
    const track = state.media?.getVideoTracks()[0];
    if ((!track?.enabled && !state.screen) || localVideo.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) continue;
    const width = localVideo.videoWidth;
    const height = localVideo.videoHeight;
    if (!width || !height) continue;
    canvas.width = Math.min(width, 960);
    canvas.height = Math.round((canvas.width / width) * height);
    try {
      context.drawImage(localVideo, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.68));
      if (blob && state.joined) await postMedia('/api/frame', blob, 'image/jpeg');
    } catch (error) {
      reportSamplingError('video', error.message);
    }
  }
}

async function postMedia(path, blob, contentType) {
  const query = new URLSearchParams({ room: state.room, client: state.client });
  await api(`${path}?${query}`, {
    method: 'POST',
    headers: { 'Content-Type': contentType },
    body: blob,
  });
}

function reportSamplingError(kind, message) {
  if (state.reportingErrors.has(kind)) return;
  state.reportingErrors.add(kind);
  if (message?.includes('not configured')) {
    setMonitoring(false, 'Add an OpenAI API key on the server to enable moderation.');
    return;
  }
  setMonitoring(false, 'One or more hosted checks are not working. The call can continue.');
  setHint(`${kind === 'audio' ? 'Audio' : 'Video'} check unavailable. The call and chat can continue.`, true);
}

async function leaveCall() {
  if (!state.joined) return;
  const room = state.room;
  const client = state.client;
  state.joined = false;
  if (state.timerTask) window.clearInterval(state.timerTask);
  state.timerTask = null;
  if (state.pc) state.pc.close();
  state.pc = null;
  if (state.screen) state.screen.getTracks().forEach((track) => track.stop());
  state.screen = null;
  if (state.media) state.media.getTracks().forEach((track) => track.stop());
  state.media = null;
  localVideo.srcObject = null;
  remoteVideo.srcObject = null;
  $('joinOverlay').hidden = false;
  $('leaveButton').disabled = true;
  $('endCallButton').disabled = true;
  $('shareButton').disabled = true;
  $('micButton').disabled = true;
  $('cameraButton').disabled = true;
  $('screenButton').disabled = true;
  $('chatInput').disabled = true;
  $('sendChatButton').disabled = true;
  $('screenButton').classList.remove('enabled');
  $('screenButton').setAttribute('aria-pressed', 'false');
  $('screenLabel').textContent = 'Present';
  $('localPlaceholder').hidden = false;
  $('remotePlaceholder').hidden = false;
  localTile.classList.remove('has-stream');
  remoteTile.classList.remove('has-stream');
  setMicState(false);
  setCameraState(false);
  setRemoteParticipant('', false);
  state.callStartedAt = null;
  $('callTimer').textContent = '00:00';
  setCallState('Ready to join');
  setHint('Join the room to turn on your camera and microphone.');
  state.client = '';
  state.cursor = 0;
  state.remoteParticipant = false;
  try { await postJson('/api/leave', { room, client }); } catch {}
}

async function toggleMic() {
  const track = state.media?.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  setMicState(track.enabled);
}

async function toggleCamera() {
  const track = state.media?.getVideoTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  setCameraState(track.enabled);
}

async function toggleScreenShare() {
  if (!state.joined) return;
  if (state.screen) {
    const track = state.media?.getVideoTracks()[0];
    if (state.currentScreenSender && track) await state.currentScreenSender.replaceTrack(track);
    state.screen.getTracks().forEach((item) => item.stop());
    state.screen = null;
    localVideo.srcObject = state.media;
    $('screenButton').classList.remove('enabled');
    $('screenButton').setAttribute('aria-pressed', 'false');
    $('screenLabel').textContent = 'Present';
    setCameraState(Boolean(track?.enabled));
    return;
  }
  if (!navigator.mediaDevices?.getDisplayMedia) {
    setHint('Screen sharing is not available in this browser.', true);
    return;
  }
  try {
    state.screen = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
    const track = state.screen.getVideoTracks()[0];
    if (state.currentScreenSender) await state.currentScreenSender.replaceTrack(track);
    localVideo.srcObject = state.screen;
    localVideo.play().catch(() => {});
    $('localPlaceholder').hidden = true;
    localTile.classList.add('has-stream');
    $('screenButton').classList.add('enabled');
    $('screenButton').setAttribute('aria-pressed', 'true');
    $('screenLabel').textContent = 'Stop presenting';
    track.addEventListener('ended', () => {
      if (state.screen?.getVideoTracks()[0] === track) toggleScreenShare().catch(() => {});
    }, { once: true });
    setHint('Your shared screen is visible to the other participant and included in sampled video checks.');
  } catch (error) {
    if (error?.name !== 'NotAllowedError') setHint('Could not share your screen.', true);
  }
}

async function sendChat(event) {
  event.preventDefault();
  const input = $('chatInput');
  const text = input.value.trim();
  if (!text || !state.joined) return;
  $('sendChatButton').disabled = true;
  try {
    await postJson('/api/chat', { room: state.room, client: state.client, text });
    input.value = '';
  } catch (error) {
    setHint(error.message || 'Message could not be sent.', true);
  } finally {
    $('sendChatButton').disabled = !state.joined;
    input.focus();
  }
}

async function copyInvite() {
  const invite = new URL(window.location.href);
  invite.searchParams.set('room', state.room);
  try {
    await navigator.clipboard.writeText(invite.toString());
    $('shareButton').textContent = 'Link copied';
    window.setTimeout(() => { $('shareButton').textContent = 'Copy invite link'; }, 1800);
  } catch {
    window.prompt('Copy this invite link', invite.toString());
  }
}

function showTab(tab) {
  const chat = tab === 'chat';
  $('chatTab').classList.toggle('active', chat);
  $('warningsTab').classList.toggle('active', !chat);
  $('chatTab').setAttribute('aria-selected', String(chat));
  $('warningsTab').setAttribute('aria-selected', String(!chat));
  $('chatPanel').hidden = !chat;
  $('warningsPanel').hidden = chat;
}

function startWithUrlRoom() {
  const code = new URLSearchParams(window.location.search).get('room');
  if (code) $('roomInput').value = code.toUpperCase();
}

$('joinForm').addEventListener('submit', joinCall);
$('leaveButton').addEventListener('click', leaveCall);
$('endCallButton').addEventListener('click', leaveCall);
$('micButton').addEventListener('click', toggleMic);
$('cameraButton').addEventListener('click', toggleCamera);
$('screenButton').addEventListener('click', toggleScreenShare);
$('chatForm').addEventListener('submit', sendChat);
$('shareButton').addEventListener('click', copyInvite);
$('chatTab').addEventListener('click', () => showTab('chat'));
$('warningsTab').addEventListener('click', () => showTab('warnings'));
$('toastClose').addEventListener('click', () => {
  $('warningToast').hidden = true;
  if (state.toastTask) window.clearTimeout(state.toastTask);
});
$('roomInput').addEventListener('input', (event) => { event.target.value = event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); });
window.addEventListener('beforeunload', () => {
  if (state.joined && navigator.sendBeacon) {
    navigator.sendBeacon('/api/leave', new Blob([JSON.stringify({ room: state.room, client: state.client })], { type: 'application/json' }));
  }
});

startWithUrlRoom();
loadConfig();
