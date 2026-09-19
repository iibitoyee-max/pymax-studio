/**
 * PryMax Studio — room client
 * Talks to the Flask backend (app.py) for:
 *   - WebRTC signaling relay (mesh topology — fine for a handful of peers;
 *     a real 20k-attendee deployment would need an SFU, not this)
 *   - chat / polls / Q&A, via short-polling every ~1.2s
 *
 * No external libraries. Everything here is plain WebRTC + fetch.
 */

const API = ""; // same-origin, Flask serves both API and this page

const state = {
  roomId: null,
  peerId: null,
  peerSecret: null,
  name: null,
  hostToken: null,
  isHost: false,
  localStream: null,
  peerConnections: new Map(), // peer_id -> RTCPeerConnection
  chatSince: 0,
  qaVoted: new Set(),
  pollVoted: new Set(),
};

const ICE_SERVERS = [{ urls: "stun:stun.l.google.com:19302" }];

// ---------------------------------------------------------------------
// Join flow
// ---------------------------------------------------------------------

const params = new URLSearchParams(window.location.search);
if (params.get("room")) document.getElementById("room-input").value = params.get("room");
if (params.get("name")) document.getElementById("name-input").value = params.get("name");
if (params.get("host_token")) document.getElementById("host-token-input").value = params.get("host_token");
if (params.get("passcode")) document.getElementById("passcode-input").value = params.get("passcode");

document.getElementById("claim-host-btn").addEventListener("click", async () => {
  const roomId = document.getElementById("room-input").value.trim();
  const resultEl = document.getElementById("claim-result");
  if (!roomId) {
    resultEl.textContent = "Enter a room name first.";
    resultEl.classList.remove("hidden");
    resultEl.classList.add("error");
    return;
  }
  try {
    const res = await fetch(`${API}/api/room/${roomId}/host/claim`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      resultEl.textContent = data.error || "Could not claim host.";
      resultEl.classList.remove("hidden");
      resultEl.classList.add("error");
      return;
    }
    document.getElementById("host-token-input").value = data.host_token;
    const hostLink = `${window.location.origin}${window.location.pathname}?room=${encodeURIComponent(roomId)}&host_token=${encodeURIComponent(data.host_token)}`;
    resultEl.classList.remove("error", "hidden");
    resultEl.textContent = `You're the host. This token is shown once — save this link: ${hostLink}`;
  } catch (e) {
    resultEl.textContent = "Claim request failed — is the server running?";
    resultEl.classList.remove("hidden");
    resultEl.classList.add("error");
  }
});

document.getElementById("join-btn").addEventListener("click", async () => {
  const name = document.getElementById("name-input").value.trim();
  const roomId = document.getElementById("room-input").value.trim();
  const hostToken = document.getElementById("host-token-input").value.trim();
  const passcode = document.getElementById("passcode-input").value.trim();
  if (!name || !roomId) return;
  await enterStudio(roomId, name, hostToken || null, passcode || null);
});

async function enterStudio(roomId, name, hostToken, passcode) {
  const errorEl = document.getElementById("join-error");
  errorEl.classList.add("hidden");

  // Join first, before ever touching the camera/mic — no point prompting
  // for media permissions if the passcode is wrong or the room rejects
  // the join for some other reason. This also fixes a real pre-existing
  // gap: the previous version never checked whether the join call
  // succeeded at all, so a rejected join (403/404/anything but 200)
  // would silently try to proceed with an undefined peer_id.
  let res, data;
  try {
    res = await fetch(`${API}/api/room/${roomId}/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, host_token: hostToken, passcode }),
    });
    data = await res.json();
  } catch (e) {
    errorEl.textContent = "Couldn't reach the server. Check your connection and try again.";
    errorEl.classList.remove("hidden");
    return;
  }

  if (!res.ok) {
    errorEl.textContent = data.error || "Couldn't join that room.";
    errorEl.classList.remove("hidden");
    return;
  }

  state.roomId = roomId;
  state.name = name;
  state.hostToken = hostToken;

  try {
    state.localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch (e) {
    console.warn("Camera/mic unavailable, joining view-only:", e);
    state.localStream = new MediaStream(); // view-only fallback
  }

  addVideoTile("local", name + " (you)", state.localStream, true);

  state.peerId = data.peer_id;
  state.peerSecret = data.peer_secret;
  state.isHost = data.role === "host";

  if (state.isHost) {
    document.getElementById("host-badge").classList.remove("hidden");
  } else {
    document.querySelectorAll(".host-only").forEach((el) => el.classList.add("hidden"));
  }

  for (const peer of data.existing_peers) {
    await callPeer(peer.peer_id, peer.name);
  }

  document.getElementById("join-screen").classList.add("hidden");
  document.getElementById("studio").classList.remove("hidden");

  // Move focus into the new view for keyboard/screen-reader users — without
  // this, focus stays on the (now-hidden) join button, which is confusing
  // for anyone not visually watching the page change.
  document.getElementById("tab-chat").focus();
  announce(`Joined room ${roomId} as ${state.isHost ? "host" : "attendee"}`);

  startClock();
  refreshProfileDisplay();
  loadMeetingInfo();
  pollSignaling();
  pollChat();
  pollPolls();
  pollQA();
  pollReactions();
  pollRaffle();
  pollBreakout();
  pollWhiteboard();
  pollProfile();
  updatePeerCount();
}

// ---------------------------------------------------------------------
// WebRTC
// ---------------------------------------------------------------------

function createPeerConnection(remotePeerId, remoteName) {
  const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
  state.peerConnections.set(remotePeerId, pc);

  state.localStream.getTracks().forEach((track) => pc.addTrack(track, state.localStream));

  pc.ontrack = (event) => {
    addVideoTile(remotePeerId, remoteName, event.streams[0], false);
  };

  pc.onicecandidate = (event) => {
    if (event.candidate) {
      sendSignal(remotePeerId, "ice-candidate", event.candidate);
    }
  };

  pc.onconnectionstatechange = () => {
    if (["disconnected", "failed", "closed"].includes(pc.connectionState)) {
      removeVideoTile(remotePeerId);
      state.peerConnections.delete(remotePeerId);
      updatePeerCount();
    }
  };

  return pc;
}

async function callPeer(remotePeerId, remoteName) {
  const pc = createPeerConnection(remotePeerId, remoteName);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  sendSignal(remotePeerId, "offer", offer);
}

async function handleOffer(fromPeerId, offer) {
  const pc = createPeerConnection(fromPeerId, "Guest");
  await pc.setRemoteDescription(new RTCSessionDescription(offer));
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  sendSignal(fromPeerId, "answer", answer);
}

async function handleAnswer(fromPeerId, answer) {
  const pc = state.peerConnections.get(fromPeerId);
  if (pc) await pc.setRemoteDescription(new RTCSessionDescription(answer));
}

async function handleIceCandidate(fromPeerId, candidate) {
  const pc = state.peerConnections.get(fromPeerId);
  if (pc) {
    try {
      await pc.addIceCandidate(candidate);
    } catch (e) {
      console.warn("ICE add failed", e);
    }
  }
}

function sendSignal(to, type, payload) {
  fetch(`${API}/api/room/${state.roomId}/signal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ to, from: state.peerId, from_secret: state.peerSecret, type, payload }),
  });
}

async function pollSignaling() {
  while (state.roomId) {
    try {
      const res = await fetch(
        `${API}/api/room/${state.roomId}/signal/${state.peerId}?secret=${encodeURIComponent(state.peerSecret)}`
      );
      const messages = await res.json();
      for (const msg of messages) {
        if (msg.type === "peer-joined") {
          await callPeer(msg.from, msg.name);
          updatePeerCount();
        } else if (msg.type === "offer") {
          await handleOffer(msg.from, msg.payload);
        } else if (msg.type === "answer") {
          await handleAnswer(msg.from, msg.payload);
        } else if (msg.type === "ice-candidate") {
          await handleIceCandidate(msg.from, msg.payload);
        } else if (msg.type === "peer-left") {
          removeVideoTile(msg.from);
          state.peerConnections.get(msg.from)?.close();
          state.peerConnections.delete(msg.from);
          updatePeerCount();
        }
      }
    } catch (e) {
      console.warn("signal poll failed", e);
    }
    await sleep(1000);
  }
}

// ---------------------------------------------------------------------
// Video grid
// ---------------------------------------------------------------------

function addVideoTile(id, label, stream, muted) {
  const grid = document.getElementById("video-grid");
  let tile = document.getElementById(`tile-${id}`);
  if (!tile) {
    tile = document.createElement("div");
    tile.className = "video-tile";
    tile.id = `tile-${id}`;
    tile.setAttribute("role", "group");
    tile.innerHTML = `<video autoplay playsinline></video><div class="label"></div>`;
    grid.appendChild(tile);
  }
  const video = tile.querySelector("video");
  video.srcObject = stream;
  video.muted = muted;
  video.setAttribute("aria-label", `${label} video feed${muted ? " (muted)" : ""}`);
  tile.setAttribute("aria-label", label);
  tile.querySelector(".label").textContent = label;
}

function removeVideoTile(id) {
  document.getElementById(`tile-${id}`)?.remove();
}

function updatePeerCount() {
  const count = state.peerConnections.size + 1;
  document.getElementById("peer-count").textContent = `${count} in room`;
}

// ---------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------

document.getElementById("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  await fetch(`${API}/api/room/${state.roomId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name, message }),
  });
});

async function pollChat() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/chat?since=${state.chatSince}`);
      const messages = await res.json();
      const list = document.getElementById("chat-list");
      for (const m of messages) {
        state.chatSince = Math.max(state.chatSince, m.id);
        const el = document.createElement("div");
        el.className = "chat-msg";
        el.innerHTML = `<div class="who"></div><div class="body"></div>`;
        el.querySelector(".who").textContent = m.name;
        el.querySelector(".body").textContent = m.message;
        list.appendChild(el);
        list.scrollTop = list.scrollHeight;
      }
    } catch (e) {
      console.warn("chat poll failed", e);
    }
    await sleep(1200);
  }
}

// ---------------------------------------------------------------------
// Polls
// ---------------------------------------------------------------------

document.getElementById("poll-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = document.getElementById("poll-question").value.trim();
  const options = document.getElementById("poll-options").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!question || options.length < 2) return;
  await fetch(`${API}/api/room/${state.roomId}/poll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, options }),
  });
  document.getElementById("poll-question").value = "";
  document.getElementById("poll-options").value = "";
});

async function pollPolls() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/poll`);
      const polls = await res.json();
      renderPolls(polls);
    } catch (e) {
      console.warn("poll fetch failed", e);
    }
    await sleep(1500);
  }
}

function renderPolls(polls) {
  const list = document.getElementById("poll-list");
  list.innerHTML = "";
  for (const poll of polls.slice().reverse()) {
    const total = poll.votes.reduce((a, b) => a + b, 0) || 1;
    const card = document.createElement("div");
    card.className = "poll-card";
    const q = document.createElement("div");
    q.className = "q";
    q.textContent = poll.question;
    card.appendChild(q);
    poll.options.forEach((opt, i) => {
      const pct = Math.round((poll.votes[i] / total) * 100);
      const optEl = document.createElement("div");
      optEl.className = "poll-option";
      optEl.innerHTML = `<div class="fill" style="width:${pct}%"></div><div class="label"><span></span><span></span></div>`;
      optEl.querySelectorAll(".label span")[0].textContent = opt;
      optEl.querySelectorAll(".label span")[1].textContent = `${pct}%`;
      optEl.addEventListener("click", async () => {
        const key = `${poll.id}`;
        if (state.pollVoted.has(key)) return;
        state.pollVoted.add(key);
        await fetch(`${API}/api/room/${state.roomId}/poll/${poll.id}/vote`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: state.name, option_index: i }),
        });
      });
      card.appendChild(optEl);
    });
    list.appendChild(card);
  }
}

// ---------------------------------------------------------------------
// Q&A
// ---------------------------------------------------------------------

document.getElementById("qa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("qa-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  await fetch(`${API}/api/room/${state.roomId}/qa`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name, question }),
  });
});

async function pollQA() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/qa`);
      const questions = await res.json();
      renderQA(questions);
    } catch (e) {
      console.warn("qa fetch failed", e);
    }
    await sleep(1500);
  }
}

function renderQA(questions) {
  const list = document.getElementById("qa-list");
  list.innerHTML = "";
  for (const q of questions) {
    const card = document.createElement("div");
    card.className = "qa-card" + (q.answered ? " answered" : "");
    const upBtn = document.createElement("button");
    upBtn.className = "qa-upvote";
    upBtn.textContent = `▲ ${q.upvotes}`;
    upBtn.addEventListener("click", async () => {
      const key = `${q.id}`;
      if (state.qaVoted.has(key)) return;
      state.qaVoted.add(key);
      await fetch(`${API}/api/room/${state.roomId}/qa/${q.id}/upvote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: state.name }),
      });
    });
    const body = document.createElement("div");
    body.className = "qa-body";
    body.innerHTML = `<div class="who"></div><div class="q"></div>`;
    body.querySelector(".who").textContent = q.name;
    body.querySelector(".q").textContent = q.question;
    card.appendChild(upBtn);
    card.appendChild(body);
    list.appendChild(card);
  }
}

// ---------------------------------------------------------------------
// Tabs, transport controls, misc
// ---------------------------------------------------------------------

const tabList = document.querySelectorAll('[role="tab"]');

function activateTab(tab) {
  tabList.forEach((t) => {
    const selected = t === tab;
    t.classList.toggle("active", selected);
    t.setAttribute("aria-selected", String(selected));
    t.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
  document.getElementById(`panel-${tab.dataset.tab}`).classList.remove("hidden");
}

tabList.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab));

  // Standard ARIA APG tab pattern: Left/Right (and Home/End) move focus
  // and activate; roving tabindex above keeps only the active tab in the
  // normal Tab order.
  tab.addEventListener("keydown", (e) => {
    const tabs = Array.from(tabList);
    const currentIndex = tabs.indexOf(tab);
    let targetIndex = null;

    if (e.key === "ArrowRight") targetIndex = (currentIndex + 1) % tabs.length;
    else if (e.key === "ArrowLeft") targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") targetIndex = 0;
    else if (e.key === "End") targetIndex = tabs.length - 1;

    if (targetIndex !== null) {
      e.preventDefault();
      const target = tabs[targetIndex];
      activateTab(target);
      target.focus();
    }
  });
});

function announce(message) {
  document.getElementById("sr-announcer").textContent = message;
}

document.getElementById("mic-btn").addEventListener("click", (e) => {
  const track = state.localStream.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  e.target.classList.toggle("active", track.enabled);
  e.target.setAttribute("aria-pressed", String(track.enabled));
  announce(track.enabled ? "Microphone on" : "Microphone muted");
});

document.getElementById("cam-btn").addEventListener("click", (e) => {
  const track = state.localStream.getVideoTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  e.target.classList.toggle("active", track.enabled);
  e.target.setAttribute("aria-pressed", String(track.enabled));
  announce(track.enabled ? "Camera on" : "Camera off");
});

document.getElementById("leave-btn").addEventListener("click", async () => {
  await fetch(`${API}/api/room/${state.roomId}/leave`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peer_id: state.peerId, peer_secret: state.peerSecret }),
  });
  window.location.reload();
});

function startClock() {
  const start = Date.now();
  setInterval(() => {
    const elapsed = Math.floor((Date.now() - start) / 1000);
    const h = String(Math.floor(elapsed / 3600)).padStart(2, "0");
    const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");
    const s = String(elapsed % 60).padStart(2, "0");
    document.getElementById("timecode").textContent = `${h}:${m}:${s}`;
  }, 1000);
}

// ---------------------------------------------------------------------
// Session profile — intended delivery labels (mode/quality/audio). This
// never activates a real pipeline; it's metadata for the run-of-show.
// ---------------------------------------------------------------------

const PROFILE_MODES = ["webrtc", "rtmp", "srt", "ndi", "sdi", "smpte_2110"];
const PROFILE_QUALITIES = ["480p", "720p", "1080p", "2160p", "4320p"];
const PROFILE_AUDIO = ["pcm", "aac", "ac3", "dante", "aes67", "madi"];

function fillSelect(id, options) {
  const el = document.getElementById(id);
  el.innerHTML = options.map((o) => `<option value="${o}">${o}</option>`).join("");
}
fillSelect("profile-mode", PROFILE_MODES);
fillSelect("profile-quality", PROFILE_QUALITIES);
fillSelect("profile-audio", PROFILE_AUDIO);

document.getElementById("profile-save-btn").addEventListener("click", async () => {
  const res = await fetch(`${API}/api/room/${state.roomId}/profile`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host_token: state.hostToken,
      title: document.getElementById("profile-title").value.trim(),
      mode: document.getElementById("profile-mode").value,
      video_quality: document.getElementById("profile-quality").value,
      audio_format: document.getElementById("profile-audio").value,
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || "Failed to save profile.");
    return;
  }
  refreshProfileDisplay();
});

async function refreshProfileDisplay() {
  try {
    const res = await fetch(`${API}/api/room/${state.roomId}/profile`);
    const profile = await res.json();
    document.getElementById("session-title").textContent = profile.title || "";
    document.getElementById("mode-badge").textContent = profile.mode.replace("_", " ");
    document.getElementById("profile-title").value = profile.title || "";
    document.getElementById("profile-mode").value = profile.mode;
    document.getElementById("profile-quality").value = profile.video_quality;
    document.getElementById("profile-audio").value = profile.audio_format;
  } catch (e) {
    console.warn("profile fetch failed", e);
  }
}

async function pollProfile() {
  while (state.roomId) {
    await sleep(4000);
    refreshProfileDisplay();
  }
}

// ---------------------------------------------------------------------
// Meeting info — shareable during an ongoing meeting (Meeting ID,
// passcode, join link, and a QR code when the library loaded — see
// room.html's comment on why that's not guaranteed). Loaded once after
// joining; this data doesn't change during a session, so no need to poll.
// ---------------------------------------------------------------------

async function loadMeetingInfo() {
  try {
    const res = await fetch(
      `${API}/api/room/${state.roomId}/meeting-info/${state.peerId}?secret=${encodeURIComponent(state.peerSecret)}`
    );
    if (!res.ok) return;
    const info = await res.json();

    document.getElementById("info-meeting-id").textContent = info.meeting_id_display;
    document.getElementById("info-join-link").textContent = info.join_link;

    const passcodeRow = document.getElementById("info-passcode-row");
    if (info.passcode) {
      document.getElementById("info-passcode").textContent = info.passcode;
      passcodeRow.classList.remove("hidden");
    } else {
      passcodeRow.classList.add("hidden");
    }

    document.getElementById("copy-info-link-btn").onclick = async () => {
      try {
        await navigator.clipboard.writeText(info.join_link);
        announce("Join link copied");
      } catch (e) {
        announce("Couldn't copy automatically — select the link text instead");
      }
    };

    renderMeetingQr(info.join_link);
  } catch (e) {
    console.warn("meeting info fetch failed", e);
  }
}

function renderMeetingQr(joinLink) {
  const wrap = document.getElementById("info-qr-wrap");
  const target = document.getElementById("info-qr");
  // window.QRCode comes from the CDN script tag in room.html, which may
  // not have loaded (no internet access, a blocked CDN, an offline
  // deployment). Checking for it rather than assuming it's there is the
  // whole point — the rest of the sharing panel (link/ID/passcode) works
  // regardless, tested independently of this.
  if (typeof QRCode === "undefined" || window.__qrLoadFailed) {
    document.getElementById("info-qr-fallback").classList.remove("visually-hidden");
    wrap.classList.remove("qr-visible");
    return;
  }
  target.innerHTML = "";
  try {
    new QRCode(target, { text: joinLink, width: 140, height: 140 });
    wrap.classList.add("qr-visible");
  } catch (e) {
    console.warn("QR generation failed", e);
    document.getElementById("info-qr-fallback").classList.remove("visually-hidden");
    wrap.classList.remove("qr-visible");
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------
// Reactions — floating emoji, broadcast via polling like chat
// ---------------------------------------------------------------------

let reactionSince = 0;

document.querySelectorAll(".reaction-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    fetch(`${API}/api/room/${state.roomId}/reaction`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: state.name, emoji: btn.dataset.emoji }),
    });
  });
});

async function pollReactions() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/reaction?since=${reactionSince}`);
      const reactions = await res.json();
      for (const r of reactions) {
        reactionSince = Math.max(reactionSince, r.id);
        spawnFloatingEmoji(r.emoji);
      }
    } catch (e) {
      console.warn("reaction poll failed", e);
    }
    await sleep(1000);
  }
}

function spawnFloatingEmoji(emoji) {
  const layer = document.getElementById("reaction-layer");
  const el = document.createElement("div");
  el.className = "floating-emoji";
  el.textContent = emoji;
  el.style.left = `${20 + Math.random() * 60}%`;
  layer.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ---------------------------------------------------------------------
// Raffle
// ---------------------------------------------------------------------

document.getElementById("raffle-enter-btn").addEventListener("click", async () => {
  await fetch(`${API}/api/room/${state.roomId}/raffle/enter`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name }),
  });
});

document.getElementById("raffle-draw-btn").addEventListener("click", async () => {
  const res = await fetch(`${API}/api/room/${state.roomId}/raffle/draw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_token: state.hostToken }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || "Failed to draw.");
  }
});

async function pollRaffle() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/raffle`);
      const data = await res.json();
      const el = document.getElementById("raffle-status");
      if (data.winner) {
        el.innerHTML = `Winner: <span class="winner"></span> (${data.entries.length} entries)`;
        el.querySelector(".winner").textContent = data.winner;
      } else {
        el.textContent = `${data.entries.length} entered`;
      }
    } catch (e) {
      console.warn("raffle poll failed", e);
    }
    await sleep(2000);
  }
}

// ---------------------------------------------------------------------
// Breakout rooms — auto-recall by redirecting when assignment ends
// ---------------------------------------------------------------------

document.getElementById("breakout-start-btn").addEventListener("click", async () => {
  const count = parseInt(document.getElementById("breakout-count").value, 10) || 2;
  const res = await fetch(`${API}/api/room/${state.roomId}/breakout/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_token: state.hostToken, count, duration_seconds: 300 }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || "Failed to start breakout.");
  }
});

document.getElementById("breakout-end-btn").addEventListener("click", async () => {
  await fetch(`${API}/api/room/${state.roomId}/breakout/end`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_token: state.hostToken }),
  });
});

async function pollBreakout() {
  // Skip entirely if we're already inside a breakout room ourselves
  // (its room id contains "::bo") — breakouts don't nest.
  if (state.roomId.includes("::bo")) return;

  while (state.roomId) {
    try {
      const res = await fetch(
        `${API}/api/room/${state.roomId}/breakout/${state.peerId}?secret=${encodeURIComponent(state.peerSecret)}`
      );
      const data = await res.json();
      const el = document.getElementById("breakout-status");
      if (data.active) {
        el.textContent = `You're in breakout ${data.group_index + 1} — ${data.seconds_remaining}s left`;
        if (!state.inBreakout) {
          state.inBreakout = true;
          const url = new URL(window.location.href);
          url.searchParams.set("room", data.breakout_room_id);
          url.searchParams.set("name", state.name);
          window.location.href = url.toString();
          return;
        }
      } else {
        el.textContent = "Not active";
        state.inBreakout = false;
      }
    } catch (e) {
      console.warn("breakout poll failed", e);
    }
    await sleep(2000);
  }
}

// ---------------------------------------------------------------------
// Whiteboard — freehand drawing synced by polling completed strokes
// ---------------------------------------------------------------------

const wbCanvas = document.getElementById("whiteboard");
const wbCtx = wbCanvas.getContext("2d");
let wbDrawing = false;
let wbCurrentStroke = null;
let wbSince = 0;
const wbDrawnIds = new Set();

function wbPointFromEvent(e) {
  const rect = wbCanvas.getBoundingClientRect();
  const scaleX = wbCanvas.width / rect.width;
  const scaleY = wbCanvas.height / rect.height;
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;
  return [(clientX - rect.left) * scaleX, (clientY - rect.top) * scaleY];
}

function wbDrawStroke(stroke) {
  if (stroke.points.length < 2) return;
  wbCtx.strokeStyle = stroke.color;
  wbCtx.lineWidth = stroke.width;
  wbCtx.lineCap = "round";
  wbCtx.lineJoin = "round";
  wbCtx.beginPath();
  wbCtx.moveTo(stroke.points[0][0], stroke.points[0][1]);
  for (const [x, y] of stroke.points.slice(1)) wbCtx.lineTo(x, y);
  wbCtx.stroke();
}

wbCanvas.addEventListener("pointerdown", (e) => {
  wbDrawing = true;
  wbCurrentStroke = [wbPointFromEvent(e)];
});
wbCanvas.addEventListener("pointermove", (e) => {
  if (!wbDrawing) return;
  const pt = wbPointFromEvent(e);
  wbCurrentStroke.push(pt);
  wbDrawStroke({ points: wbCurrentStroke.slice(-2), color: document.getElementById("wb-color").value, width: 3 });
});
window.addEventListener("pointerup", async () => {
  if (!wbDrawing) return;
  wbDrawing = false;
  if (wbCurrentStroke && wbCurrentStroke.length > 1) {
    await fetch(`${API}/api/room/${state.roomId}/whiteboard/stroke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        points: wbCurrentStroke,
        color: document.getElementById("wb-color").value,
        width: 3,
      }),
    });
  }
  wbCurrentStroke = null;
});

document.getElementById("wb-clear").addEventListener("click", async () => {
  const res = await fetch(`${API}/api/room/${state.roomId}/whiteboard/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_token: state.hostToken }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || "Failed to clear.");
    return;
  }
  wbCtx.clearRect(0, 0, wbCanvas.width, wbCanvas.height);
  wbDrawnIds.clear();
  wbSince = 0;
});

async function pollWhiteboard() {
  while (state.roomId) {
    try {
      const res = await fetch(`${API}/api/room/${state.roomId}/whiteboard?since=${wbSince}`);
      const strokes = await res.json();
      for (const s of strokes) {
        wbSince = Math.max(wbSince, s.id);
        if (!wbDrawnIds.has(s.id)) {
          wbDrawnIds.add(s.id);
          wbDrawStroke(s);
        }
      }
    } catch (e) {
      console.warn("whiteboard poll failed", e);
    }
    await sleep(1000);
  }
}

// ---------------------------------------------------------------------
// Recording — real client-side capture via MediaRecorder, downloads a
// .webm of the local camera/mic. (Server-side mixing/export to broadcast
// mezzanine formats like ProRes needs an ffmpeg pipeline — not built here,
// see README.)
// ---------------------------------------------------------------------

let mediaRecorder = null;
let recordedChunks = [];

document.getElementById("record-btn").addEventListener("click", () => {
  const btn = document.getElementById("record-btn");
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    btn.classList.remove("recording");
    btn.textContent = "Record";
    btn.setAttribute("aria-pressed", "false");
    announce("Recording stopped, downloading file");
    return;
  }

  recordedChunks = [];
  try {
    mediaRecorder = new MediaRecorder(state.localStream, { mimeType: "video/webm" });
  } catch (e) {
    alert("Recording isn't supported for this stream in this browser.");
    return;
  }
  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) recordedChunks.push(e.data);
  };
  mediaRecorder.onstop = () => {
    const blob = new Blob(recordedChunks, { type: "video/webm" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `prymax-${state.roomId}-${Date.now()}.webm`;
    a.click();
    URL.revokeObjectURL(url);
  };
  mediaRecorder.start();
  btn.setAttribute("aria-pressed", "true");
  announce("Recording started");
  btn.classList.add("recording");
  btn.textContent = "Stop";
});
