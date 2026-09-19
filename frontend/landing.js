/**
 * PryMax Studio — landing page client
 * ======================================
 * Vanilla JS, same pattern as room.js: no build step, no framework.
 * Talks to the real Flask backend endpoints added for this feature:
 *   POST /api/meetings/schedule
 *   POST /api/meetings/verify
 *   POST /api/admin/login
 * and hands off to the room page via URL params (room, name, host_token,
 * passcode) for the actual join, reusing all of room.js's existing,
 * tested join logic rather than duplicating it here.
 */

const API = ""; // same-origin

function announce(message) {
  document.getElementById("sr-announcer").textContent = message;
}

// ---------------------------------------------------------------------
// Card expand/collapse — clicking "Join a meeting" etc. reveals that
// card's form and hides the others, so only one form is open at a time.
// ---------------------------------------------------------------------

document.querySelectorAll(".card-open-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const targetId = btn.dataset.target;
    document.querySelectorAll(".card-form").forEach((form) => {
      form.classList.toggle("hidden", form.id !== targetId);
    });
    btn.classList.add("hidden");
    document.getElementById(targetId).querySelector("input").focus();
  });
});

function showFormError(formId, message) {
  const err = document.querySelector(`#${formId} .form-error`);
  err.textContent = message;
  err.classList.remove("hidden");
}

function clearFormError(formId) {
  document.querySelector(`#${formId} .form-error`).classList.add("hidden");
}

// ---------------------------------------------------------------------
// Start Meeting Now — instant one-click flow, skips the result panel
// and goes straight into the room. The meeting's ID/passcode/link don't
// disappear, though — they're available inside the room itself via the
// new in-room "Meeting Info" panel (see room.js), which is the whole
// point of exposing that endpoint: sharing isn't a one-time screen you
// have to catch before it scrolls away.
// ---------------------------------------------------------------------

document.getElementById("start-now-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearFormError("start-now-form");
  const name = document.getElementById("start-now-name").value.trim();
  if (!name) return;

  try {
    const res = await fetch(`${API}/api/meetings/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type: "meeting" }),
    });
    const data = await res.json();
    if (!res.ok) {
      showFormError("start-now-form", data.error || "Couldn't start a meeting.");
      return;
    }
    window.location.href = `/room?room=${encodeURIComponent(data.meeting_id)}&name=${encodeURIComponent(name)}&host_token=${encodeURIComponent(data.host_token)}`;
  } catch (err) {
    showFormError("start-now-form", "Couldn't reach the server. Check your connection.");
  }
});

// ---------------------------------------------------------------------
// Join Meeting
// ---------------------------------------------------------------------

document.getElementById("join-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearFormError("join-form");

  const name = document.getElementById("join-name").value.trim();
  const meetingId = document.getElementById("join-meeting-id").value.trim();
  const passcode = document.getElementById("join-passcode").value.trim();
  if (!name || !meetingId || !passcode) return;

  try {
    const res = await fetch(`${API}/api/meetings/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meeting_id: meetingId, passcode }),
    });
    const data = await res.json();
    if (!res.ok) {
      showFormError("join-form", data.error || "Couldn't join that meeting.");
      return;
    }
    const cleanId = meetingId.replace(/\s+/g, "");
    window.location.href = `/room?room=${encodeURIComponent(cleanId)}&name=${encodeURIComponent(name)}&passcode=${encodeURIComponent(passcode)}`;
  } catch (err) {
    showFormError("join-form", "Couldn't reach the server. Check your connection.");
  }
});

// ---------------------------------------------------------------------
// Schedule Meeting / Live Streaming — share the same result-panel flow,
// differing only in the endpoint payload's `type` and which form/button
// triggered it.
// ---------------------------------------------------------------------

async function scheduleSession(formId, nameFieldId, titleFieldId, type) {
  clearFormError(formId);
  const name = document.getElementById(nameFieldId).value.trim();
  const title = document.getElementById(titleFieldId).value.trim();
  if (!name) return;

  try {
    const res = await fetch(`${API}/api/meetings/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, title, type }),
    });
    const data = await res.json();
    if (!res.ok) {
      showFormError(formId, data.error || "Couldn't schedule that.");
      return;
    }
    showResultPanel(data, name);
  } catch (err) {
    showFormError(formId, "Couldn't reach the server. Check your connection.");
  }
}

document.getElementById("schedule-form").addEventListener("submit", (e) => {
  e.preventDefault();
  scheduleSession("schedule-form", "schedule-name", "schedule-title", "meeting");
});

document.getElementById("stream-form").addEventListener("submit", (e) => {
  e.preventDefault();
  scheduleSession("stream-form", "stream-name", "stream-title", "stream");
});

function showResultPanel(data, name) {
  const panel = document.getElementById("result-panel");
  document.getElementById("result-title").textContent =
    data.type === "stream" ? "Your stream is ready" : "Your meeting is ready";
  document.getElementById("result-meeting-id").textContent = data.meeting_id_display;
  document.getElementById("result-passcode").textContent = data.passcode;
  document.getElementById("result-link").textContent = data.join_link;
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "center" });
  announce(data.type === "stream" ? "Stream created" : "Meeting scheduled");

  document.getElementById("copy-link-btn").onclick = async () => {
    try {
      await navigator.clipboard.writeText(data.join_link);
      announce("Join link copied");
    } catch (e) {
      // Clipboard API can be unavailable (e.g. non-HTTPS); the link text
      // is already visible and selectable, so this isn't a dead end.
      announce("Couldn't copy automatically — select the link text instead");
    }
  };

  document.getElementById("enter-now-btn").onclick = () => {
    window.location.href = `/room?room=${encodeURIComponent(data.meeting_id)}&name=${encodeURIComponent(name)}&host_token=${encodeURIComponent(data.host_token)}`;
  };
}

// ---------------------------------------------------------------------
// Admin login
// ---------------------------------------------------------------------

const adminPanel = document.getElementById("admin-login-panel");

document.getElementById("admin-login-toggle").addEventListener("click", () => {
  adminPanel.classList.remove("hidden");
  document.getElementById("admin-username").focus();
});

document.getElementById("admin-panel-close").addEventListener("click", () => {
  adminPanel.classList.add("hidden");
});

adminPanel.addEventListener("click", (e) => {
  if (e.target === adminPanel) adminPanel.classList.add("hidden");
});

document.getElementById("admin-login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("admin-login-error");
  errorEl.classList.add("hidden");

  const username = document.getElementById("admin-username").value.trim();
  const room = document.getElementById("admin-room").value.trim();

  try {
    const res = await fetch(`${API}/api/admin/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, room }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || "Login failed.";
      errorEl.classList.remove("hidden");
      return;
    }
    window.location.href = `/room?room=${encodeURIComponent(data.room_id)}&name=${encodeURIComponent(username)}&host_token=${encodeURIComponent(data.host_token)}`;
  } catch (err) {
    errorEl.textContent = "Couldn't reach the server. Check your connection.";
    errorEl.classList.remove("hidden");
  }
});
