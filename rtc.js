(() => {
  const room = document.querySelector('[data-rtc-room]');
  if (!room) return;

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const signalUrl = room.dataset.signalUrl;
  const roomType = room.dataset.roomType;
  const role = room.dataset.role;
  const peerActor = room.dataset.peer;
  const liveStatusUrl = room.dataset.liveStatusUrl || '';
  const localVideo = document.getElementById('localVideo');
  const remoteVideo = document.getElementById('remoteVideo');
  const placeholder = document.getElementById('videoPlaceholder');
  const startButton = document.getElementById('startRtc');
  const micButton = document.getElementById('toggleMic');
  const camButton = document.getElementById('toggleCam');
  const statusEl = document.getElementById('rtcStatus');

  let localStream = null;
  let lastSignalId = 0;
  let pollTimer = null;
  let started = false;
  const peers = new Map();
  const pendingIce = new Map();

  const iceConfig = {
    iceServers: [
      { urls: 'stun:stun.l.google.com:19302' },
      { urls: 'stun:stun1.l.google.com:19302' }
    ]
  };

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  async function sendSignal(to, payload) {
    const response = await fetch(signalUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({to, payload})
    });
    if (!response.ok) throw new Error(`Signal failed (${response.status})`);
  }

  function attachRemote(stream) {
    if (!remoteVideo) return;
    remoteVideo.srcObject = stream;
    remoteVideo.classList.add('active');
    placeholder?.classList.add('hidden');
  }

  function buildPeer(remoteActor) {
    if (peers.has(remoteActor)) return peers.get(remoteActor);
    const pc = new RTCPeerConnection(iceConfig);
    peers.set(remoteActor, pc);

    if (localStream && (role === 'host' || roomType === 'call')) {
      localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));
    }

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        sendSignal(remoteActor, {type: 'ice', candidate: event.candidate}).catch(() => {});
      }
    };
    pc.ontrack = (event) => {
      const [stream] = event.streams;
      if (stream) attachRemote(stream);
    };
    pc.onconnectionstatechange = () => {
      const state = pc.connectionState;
      setStatus(`Connection: ${state}`);
      if (['failed', 'closed'].includes(state)) {
        pc.close();
        peers.delete(remoteActor);
      }
    };
    return pc;
  }

  async function flushIce(remoteActor, pc) {
    const items = pendingIce.get(remoteActor) || [];
    for (const candidate of items) {
      try { await pc.addIceCandidate(candidate); } catch (_) {}
    }
    pendingIce.delete(remoteActor);
  }

  async function hostOffer(remoteActor) {
    const pc = buildPeer(remoteActor);
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await sendSignal(remoteActor, {type: 'offer', sdp: pc.localDescription});
    setStatus(roomType === 'stream' ? 'Viewer connecting…' : 'Calling…');
  }

  async function processSignal(signal) {
    const from = signal.from;
    const payload = signal.payload || {};
    if (!from || !payload.type) return;

    if (payload.type === 'viewer-ready' && role === 'host') {
      await hostOffer(from);
      return;
    }

    if (payload.type === 'offer' && role === 'viewer') {
      const pc = buildPeer(from);
      await pc.setRemoteDescription(payload.sdp);
      await flushIce(from, pc);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await sendSignal(from, {type: 'answer', sdp: pc.localDescription});
      setStatus('Connected.');
      return;
    }

    if (payload.type === 'answer' && role === 'host') {
      const pc = buildPeer(from);
      await pc.setRemoteDescription(payload.sdp);
      await flushIce(from, pc);
      setStatus('Connected.');
      return;
    }

    if (payload.type === 'ice' && payload.candidate) {
      const pc = buildPeer(from);
      if (pc.remoteDescription) {
        try { await pc.addIceCandidate(payload.candidate); } catch (_) {}
      } else {
        const list = pendingIce.get(from) || [];
        list.push(payload.candidate);
        pendingIce.set(from, list);
      }
    }
  }

  async function pollSignals() {
    if (!started) return;
    try {
      const response = await fetch(`${signalUrl}?after=${lastSignalId}`, {credentials: 'same-origin'});
      if (!response.ok) throw new Error(`Polling failed (${response.status})`);
      const data = await response.json();
      for (const signal of (data.signals || [])) {
        lastSignalId = Math.max(lastSignalId, Number(signal.id) || 0);
        await processSignal(signal);
      }
    } catch (error) {
      setStatus(error.message || 'Signaling unavailable.');
    } finally {
      pollTimer = window.setTimeout(pollSignals, 900);
    }
  }

  async function start() {
    if (started) return;
    started = true;
    startButton.disabled = true;
    try {
      const configuration = await fetch('/api/rtc-config', {credentials: 'same-origin'});
      if (configuration.ok) {
        const received = await configuration.json();
        if (Array.isArray(received.iceServers)) iceConfig.iceServers = received.iceServers;
      }
      const needsLocal = role === 'host' || roomType === 'call';
      if (needsLocal) {
        localStream = await navigator.mediaDevices.getUserMedia({video: true, audio: true});
        if (localVideo) {
          localVideo.srcObject = localStream;
          localVideo.classList.add('active');
        }
        placeholder?.classList.add('hidden');
      } else {
        if (micButton) micButton.disabled = true;
        if (camButton) camButton.disabled = true;
      }
      if (role === 'host' && roomType === 'stream' && liveStatusUrl) {
        const body = new URLSearchParams({status: 'live'});
        await fetch(liveStatusUrl, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRF-Token': csrf, 'X-RTC-Request': '1'}, body});
      }
      setStatus(role === 'host' ? 'Live — ready for viewers.' : 'Connecting to creator…');
      if (role === 'viewer') await sendSignal(peerActor, {type: 'viewer-ready'});
      pollSignals();
    } catch (error) {
      started = false;
      startButton.disabled = false;
      setStatus(`Camera/microphone error: ${error.message}`);
    }
  }

  function toggleTrack(kind, button) {
    if (!localStream) return;
    const tracks = kind === 'audio' ? localStream.getAudioTracks() : localStream.getVideoTracks();
    tracks.forEach((track) => { track.enabled = !track.enabled; });
    const enabled = tracks.some((track) => track.enabled);
    button?.classList.toggle('off', !enabled);
    if (button) button.textContent = `${kind === 'audio' ? 'Mic' : 'Camera'} ${enabled ? 'on' : 'off'}`;
  }

  startButton?.addEventListener('click', start);
  micButton?.addEventListener('click', () => toggleTrack('audio', micButton));
  camButton?.addEventListener('click', () => toggleTrack('video', camButton));
  window.addEventListener('beforeunload', () => {
    if (pollTimer) clearTimeout(pollTimer);
    peers.forEach((pc) => pc.close());
    localStream?.getTracks().forEach((track) => track.stop());
  });
})();
