"""
PryMax Studio — Core Signaling & Engagement Server
====================================================
This is the "Stack A / WebRTC" backend for the webinar MVP described in the
PryMax Studio product bible. It intentionally implements only the parts that
are real, testable software with no external hardware or paid vendor APIs:

  - WebRTC signaling relay (offer/answer/ICE exchange between browsers)
  - Room/peer presence
  - Live chat
  - Polls (create, vote, live results)
  - Q&A (submit + upvote, sorted by votes)

It does NOT implement (and does not pretend to implement) SMPTE 2110, SDI/NDI
ingest, satellite uplinks, MCR router control, Nielsen watermarking, or any
of the Pillar III/IV broadcast-hardware features from the product doc — those
require physical hardware and vendor SDKs that don't exist in this
environment. See ../README.md for the full "what's real vs. spec-only" map.

Design choice: signaling uses simple REST + short-poll rather than
websockets, so the whole thing runs on Flask alone (no extra services to
install/run), which keeps it trivially deployable behind plain PHP/Apache
or any WSGI host.

Run:
    pip install flask
    python3 app.py
Serves on http://localhost:5000
"""
from __future__ import annotations

import itertools
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import os

from flask import Flask, jsonify, request, send_from_directory
from flask.wrappers import Response
from werkzeug.exceptions import HTTPException

import store
from ratelimit import rate_limit, get_client_ip
from logging_config import setup_logging, log_event, log_warning

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # 256KB cap on any single request body

logger = setup_logging()


# ---------------------------------------------------------------------------
# Request lifecycle logging. Every request gets one structured log line
# with method/path/status/duration/client-ip; anything 400+ is logged at
# WARNING so a spike in rejections stands out in log aggregation without
# needing to grep for specific status codes.
# ---------------------------------------------------------------------------

@app.before_request
def _log_request_start():
    request._prymax_start_time = time.monotonic()
    # A short id correlating every log line produced while handling this
    # one request — the per-request log line, any business-event lines
    # (host claimed, raffle drawn, ...), a rate-limit rejection, and an
    # uncaught-exception line all carry the same value, so grepping one
    # request's full story out of a busy log is a single `grep <id>`
    # instead of reconstructing it from timestamps and guesswork.
    request.request_id = uuid.uuid4().hex[:12]


@app.after_request
def _log_request_end(response):
    duration_ms = None
    start = getattr(request, "_prymax_start_time", None)
    if start is not None:
        duration_ms = round((time.monotonic() - start) * 1000, 2)

    request_id = getattr(request, "request_id", None)
    response.headers["X-Request-ID"] = request_id

    fields = {
        "request_id": request_id,
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "duration_ms": duration_ms,
        "client_ip": get_client_ip(),
    }
    if response.status_code >= 400:
        log_warning(logger, "request rejected", **fields)
    else:
        log_event(logger, "request", **fields)
    return response


@app.errorhandler(Exception)
def _log_uncaught_exception(e):
    # HTTPException (404, 405, our own 400/403/409/413/429 responses, etc.)
    # is itself a subclass of Exception in Werkzeug — pass those through
    # unchanged so this handler only intercepts genuine bugs, not normal
    # routing/validation outcomes that already have the correct status code.
    if isinstance(e, HTTPException):
        return e
    logger.error(
        "unhandled exception",
        exc_info=e,
        extra={"fields": {"path": request.path, "request_id": getattr(request, "request_id", None)}},
    )
    return jsonify({"error": "internal server error"}), 500


# ---------------------------------------------------------------------------
# Front-end hosting. The PHP registration flow's magic link points here —
# it's the same Flask process serving both the API and the room page, so a
# single `python3 app.py` is enough to demo the whole video/chat/poll/Q&A
# experience end to end.
# ---------------------------------------------------------------------------

@app.get("/")
def landing_page():
    return send_from_directory(FRONTEND_DIR, "landing.html")


@app.get("/landing.js")
def landing_js():
    return send_from_directory(FRONTEND_DIR, "landing.js")


@app.get("/landing.css")
def landing_css():
    return send_from_directory(FRONTEND_DIR, "landing.css")


@app.get("/room")
def room_page():
    return send_from_directory(FRONTEND_DIR, "room.html")


@app.get("/room.js")
def room_js():
    return send_from_directory(FRONTEND_DIR, "room.js")


@app.get("/room.css")
def room_css():
    return send_from_directory(FRONTEND_DIR, "room.css")

# ---------------------------------------------------------------------------
# Storage. Chat/polls/Q&A/whiteboard/raffle/host-token/profile are durable —
# backed by SQLite (see store.py) so they survive a restart. Peer presence
# and signaling queues stay in-memory only; see store.py's module docstring
# for why that split is deliberate, not an oversight.
# ---------------------------------------------------------------------------

store.init_db()

_lock = Lock()
_id_counter = itertools.count(store.next_start_id())


def _next_id() -> int:
    return next(_id_counter)


import random
import secrets
import hashlib
import hmac
from enum import Enum


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_peer(room: "Room", peer_id: str, secret: str | None) -> bool:
    """Constant-time check that `secret` proves ownership of `peer_id` in
    this room. Added after a live test confirmed a real vulnerability:
    /peers exposes every peer_id to every other room member, and /leave
    and /signal originally trusted a bare peer_id with no proof at all —
    letting any attendee forcibly disconnect any other peer, spoof
    signaling messages as someone else, or read messages meant only for
    another peer's inbox. See the README's security section for the full
    writeup, including the live reproduction that found this."""
    info = room.peers.get(peer_id)
    if not info or not secret:
        return False
    return hmac.compare_digest(_hash_token(secret), info.get("secret_hash", ""))


# How long a claimed host token stays valid. Configurable, since "right"
# depends on deployment (a single webinar vs. a week-long virtual event).
# 0 or negative disables expiry entirely (token valid until revoked or
# the room's data is deleted) — the old, pre-Tier-A behavior, kept
# available for anyone who genuinely wants it rather than forcing it on
# everyone.
HOST_TOKEN_TTL_SECONDS = float(os.environ.get("PRYMAX_HOST_TOKEN_TTL_SECONDS", 7 * 24 * 3600))


def _check_host(room: "Room", token: str | None) -> bool:
    """Constant-time check of a supplied host_token against the room's
    stored hash. Returns False (never raises) if the room has no host yet,
    no token was supplied, or the token has expired — callers treat all
    of those as "not authorized"."""
    if not room.host_token_hash or not token:
        return False
    if room.host_token_expires_at is not None and time.time() >= room.host_token_expires_at:
        return False
    return hmac.compare_digest(_hash_token(token), room.host_token_hash)


def _issue_host_token(room: "Room", room_id: str) -> tuple[str, float | None]:
    """Generates a fresh host token, hashes it, stores it (in memory and
    durably), and returns (raw_token, expires_at). Shared by /host/claim
    (which only calls this when the room has no host yet) and admin login
    (which calls this unconditionally, deliberately overwriting any
    existing host claim on that one specific admin-owned room — a
    repeatable login, not a claim-once flow)."""
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + HOST_TOKEN_TTL_SECONDS if HOST_TOKEN_TTL_SECONDS > 0 else None
    room.host_token_hash = _hash_token(token)
    room.host_token_expires_at = expires_at
    store.save_host_token_hash(room_id, room.host_token_hash, expires_at)
    return token, expires_at


# ---------------------------------------------------------------------------
# Admin login — a fixed, single-account convenience login, NOT a general
# user-account system. Configurable via env vars so the literal values
# aren't force-hardcoded for every deployment, but the defaults match
# what was asked for. IMPORTANT, stated plainly rather than glossed over:
# this is a simple exact-string-match credential pair with no password
# hashing, no rate-limit-proof brute-force resistance beyond the rate
# limit below, and no real account system behind it. It's a convenience
# mechanism suitable for a single-operator demo/personal deployment —
# see the README's security section for what a real multi-admin system
# would need instead.
# ---------------------------------------------------------------------------

ADMIN_USERNAME = os.environ.get("PRYMAX_ADMIN_USERNAME", "Ibitoye")
ADMIN_ROOM = os.environ.get("PRYMAX_ADMIN_ROOM", "Superroom")


@app.post("/api/admin/login")
@rate_limit("admin_login", limit=5, window_seconds=60)
def admin_login():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    room_name = (body.get("room") or "").strip()

    valid = hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(
        room_name, ADMIN_ROOM
    )
    if not valid:
        log_warning(logger, "admin login failed", request_id=getattr(request, "request_id", None))
        return _err("invalid admin credentials", 401)

    with _lock:
        room = _get_room(ADMIN_ROOM)
        token, expires_at = _issue_host_token(room, ADMIN_ROOM)

    log_event(logger, "admin login succeeded", room_id=ADMIN_ROOM, request_id=getattr(request, "request_id", None))
    return jsonify(
        {
            "host_token": token,
            "expires_at": expires_at,
            "room_id": ADMIN_ROOM,
            "role": "admin",
        }
    )


# ---------------------------------------------------------------------------
# Scheduled meetings & live streams — generates a Meeting ID (used
# directly as the room_id) and a Passcode, from the landing page's
# "Schedule Meeting" / "Live Streaming" buttons. The scheduler
# automatically becomes host of the new meeting. A live stream is the
# same mechanism with meeting_type="stream" and the session profile's
# mode pre-set to a broadcast-flavored label (still just a label — see
# the Session Profile comment above; this does not open a real RTMP/SRT
# pipeline).
# ---------------------------------------------------------------------------

def _generate_meeting_id() -> str:
    """An 11-digit numeric id, Zoom-style, used directly as the room_id
    (matches _ROOM_ID_RE — digits only, no spaces). Retries on the
    astronomically unlikely chance of a collision with an existing room."""
    for _ in range(10):
        candidate = "".join(secrets.choice("0123456789") for _ in range(11))
        if candidate not in _rooms:
            return candidate
    raise RuntimeError("could not generate a unique meeting id")


def _generate_passcode() -> str:
    """A 6-character passcode, uppercase letters + digits, excluding
    visually ambiguous characters (0/O, 1/I) for readability when typed
    in by hand from a shared invite."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _format_meeting_id(meeting_id: str) -> str:
    """Display-only grouping, e.g. "123 4567 8901" — never used as the
    actual room_id/URL value, which stays the plain digit string. Only
    applies to real generated 11-digit meeting ids; an ad-hoc room name
    (e.g. someone typed "team-standup" directly) is returned unchanged
    rather than being sliced into nonsense."""
    if len(meeting_id) == 11 and meeting_id.isdigit():
        return f"{meeting_id[:3]} {meeting_id[3:7]} {meeting_id[7:]}"
    return meeting_id


@app.post("/api/meetings/schedule")
@rate_limit("schedule_meeting", limit=20, window_seconds=60)
def schedule_meeting():
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    title = (body.get("title") or "").strip()[:200] or None
    meeting_type = body.get("type") if body.get("type") in ("meeting", "stream") else "meeting"

    with _lock:
        meeting_id = _generate_meeting_id()
        passcode = _generate_passcode()
        room = _get_room(meeting_id)
        token, expires_at = _issue_host_token(room, meeting_id)
        room.meeting_passcode = passcode
        room.meeting_type = meeting_type
        store.save_meeting_details(meeting_id, passcode, meeting_type)
        if title:
            room.profile["title"] = title
        if meeting_type == "stream":
            room.profile["mode"] = BroadcastMode.RTMP.value
        store.save_profile(meeting_id, room.profile)

    join_link = f"{request.host_url.rstrip('/')}/room?room={meeting_id}&passcode={passcode}"
    log_event(
        logger,
        "meeting scheduled",
        room_id=meeting_id,
        meeting_type=meeting_type,
        request_id=getattr(request, "request_id", None),
    )
    return jsonify(
        {
            "meeting_id": meeting_id,
            "meeting_id_display": _format_meeting_id(meeting_id),
            "passcode": passcode,
            "host_token": token,
            "expires_at": expires_at,
            "join_link": join_link,
            "type": meeting_type,
        }
    )


# ---------------------------------------------------------------------------
# Session profile — labels for the *intended* delivery mode/quality of a
# room, adapted from the product doc's BroadcastMode/VideoQuality/AudioFormat
# concepts. This is metadata only: setting mode="sdi" does NOT open an SDI
# pipeline (no hardware here to open one on) — it just records what the
# operator intends this session to eventually be delivered as, e.g. for
# a run-of-show sheet or downstream tooling to read. Real backends per
# mode are the "not built" items in ../README.md.
# ---------------------------------------------------------------------------

class BroadcastMode(str, Enum):
    WEBRTC = "webrtc"
    RTMP = "rtmp"
    SRT = "srt"
    NDI = "ndi"
    SDI = "sdi"
    SMPTE_2110 = "smpte_2110"


class VideoQuality(str, Enum):
    SD = "480p"
    HD = "720p"
    FHD = "1080p"
    UHD = "2160p"
    UHD_8K = "4320p"


class AudioFormat(str, Enum):
    PCM = "pcm"
    AAC = "aac"
    AC3 = "ac3"
    DANTE = "dante"
    AES67 = "aes67"
    MADI = "madi"


@dataclass
class Room:
    room_id: str
    peers: dict[str, dict[str, Any]] = field(default_factory=dict)  # peer_id -> {name, joined_at, role}
    signal_queues: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    chat: list[dict] = field(default_factory=list)
    polls: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    whiteboard: list[dict] = field(default_factory=list)
    raffle_entries: list[dict] = field(default_factory=list)  # {name}
    raffle_winner: str | None = None
    breakout: dict | None = None  # {count, duration_seconds, started_at, groups: [[peer_id,...]]}
    host_token_hash: str | None = None  # SHA-256 hex digest; the raw token is only ever returned once, from /host/claim
    host_token_expires_at: float | None = None  # None = never expires (see HOST_TOKEN_TTL_SECONDS)
    meeting_passcode: str | None = None  # plain text, see store.save_meeting_details' docstring for why
    meeting_type: str | None = None  # "meeting" | "stream" | None (an ad-hoc/legacy room)
    profile: dict = field(
        default_factory=lambda: {
            "title": None,
            "mode": BroadcastMode.WEBRTC.value,
            "video_quality": VideoQuality.FHD.value,
            "audio_format": AudioFormat.AAC.value,
        }
    )
    created_at: float = field(default_factory=time.time)


_rooms: dict[str, Room] = {}


def _get_room(room_id: str) -> Room:
    if room_id not in _rooms:
        room = Room(room_id=room_id)
        durable = store.load_room(room_id, room.created_at)
        room.host_token_hash = durable["host_token_hash"]
        room.host_token_expires_at = durable["host_token_expires_at"]
        room.meeting_passcode = durable["meeting_passcode"]
        room.meeting_type = durable["meeting_type"]
        room.profile = durable["profile"]
        room.chat = durable["chat"]
        room.polls = durable["polls"]
        room.questions = durable["questions"]
        room.whiteboard = durable["whiteboard"]
        room.raffle_entries = durable["raffle_entries"]
        room.raffle_winner = durable["raffle_winner"]
        _rooms[room_id] = room
    return _rooms[room_id]


def _err(message: str, status: int = 400) -> tuple[Response, int]:
    return jsonify({"error": message}), status


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "request body too large"}), 413


import re

_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}(::bo\d{1,4})?$")


@app.before_request
def _validate_room_id():
    room_id = request.view_args.get("room_id") if request.view_args else None
    if room_id is not None and not _ROOM_ID_RE.match(room_id):
        return _err(
            "invalid room id — use up to 80 letters/digits/-/_ "
            "(breakout rooms use the automatically generated '::boN' suffix)"
        )


def _clean_str(value: Any, max_len: int, field_name: str) -> tuple[str | None, tuple | None]:
    """Strip and length-check a required string field. Returns
    (cleaned_value, None) on success or (None, error_response) on failure
    — callers do `cleaned, err = _clean_str(...); if err: return err`."""
    s = (value or "").strip() if isinstance(value, str) else ""
    if not s:
        return None, _err(f"{field_name} is required")
    if len(s) > max_len:
        return None, _err(f"{field_name} must be {max_len} characters or fewer")
    return s, None


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/join")
@rate_limit("join", limit=10, window_seconds=60)
def join_room(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    host_token = body.get("host_token")

    room = _get_room(room_id)
    is_host = _check_host(room, host_token)

    # A room created via "Schedule Meeting" / "Live Streaming" has a
    # passcode gate — enforced here, at the actual join point, not just
    # as a separate pre-check screen, so it can't be bypassed by calling
    # this endpoint directly. The host bypasses it (proven via host_token
    # above), matching how a meeting organizer doesn't need their own
    # meeting's passcode to (re)join it.
    if room.meeting_passcode and not is_host:
        supplied = (body.get("passcode") or "").strip()
        if not supplied or not hmac.compare_digest(supplied, room.meeting_passcode):
            return _err("meeting passcode required or incorrect", 403)

    peer_id = uuid.uuid4().hex[:12]
    peer_secret = secrets.token_urlsafe(24)
    with _lock:
        role = "host" if is_host else "attendee"
        room.peers[peer_id] = {
            "name": name,
            "joined_at": time.time(),
            "role": role,
            "secret_hash": _hash_token(peer_secret),
        }
        # Tell every *existing* peer that a new peer arrived, so they can
        # initiate an offer to it (simple mesh, new joiner is the answerer).
        existing_peers = [
            {"peer_id": pid, "name": info["name"], "role": info.get("role", "attendee")}
            for pid, info in room.peers.items()
            if pid != peer_id
        ]
        for pid in existing_peers:
            room.signal_queues[pid["peer_id"]].append(
                {"type": "peer-joined", "from": peer_id, "name": name}
            )

    return jsonify(
        {"peer_id": peer_id, "peer_secret": peer_secret, "role": role, "existing_peers": existing_peers}
    )


@app.post("/api/meetings/verify")
@rate_limit("verify_meeting", limit=20, window_seconds=60)
def verify_meeting():
    """Lets the landing page's 'Join Meeting' form check a meeting id +
    passcode before navigating to the room, for a better error message
    than 'joined the room, then immediately got bounced'. This is a
    convenience pre-check only — the actual, un-bypassable enforcement
    lives in join_room() above, which checks the passcode again
    regardless of what this endpoint says."""
    body = request.get_json(force=True, silent=True) or {}
    meeting_id = (body.get("meeting_id") or "").strip().replace(" ", "")
    passcode = (body.get("passcode") or "").strip()

    if not _ROOM_ID_RE.match(meeting_id):
        return _err("invalid meeting id")

    room = _get_room(meeting_id)
    if room.meeting_passcode is None and room.host_token_hash is None:
        # Never scheduled/claimed — nothing to join yet, rather than
        # silently creating an empty phantom room and calling it valid.
        return _err("no meeting found with that id", 404)
    if room.meeting_passcode and not hmac.compare_digest(passcode, room.meeting_passcode):
        return _err("incorrect passcode", 403)

    return jsonify(
        {
            "valid": True,
            "meeting_id": meeting_id,
            "title": room.profile.get("title"),
            "type": room.meeting_type or "meeting",
        }
    )


@app.post("/api/room/<room_id>/leave")
@rate_limit("leave", limit=20, window_seconds=60)
def leave_room(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    peer_id = body.get("peer_id")
    room = _get_room(room_id)
    if not peer_id or not _check_peer(room, peer_id, body.get("peer_secret")):
        return _err("peer authentication required", 403)
    with _lock:
        if peer_id in room.peers:
            del room.peers[peer_id]
            room.signal_queues.pop(peer_id, None)
            for pid in room.peers:
                room.signal_queues[pid].append({"type": "peer-left", "from": peer_id})
    return jsonify({"ok": True})


@app.get("/api/room/<room_id>/peers")
def list_peers(room_id: str):
    room = _get_room(room_id)
    return jsonify(
        [
            {"peer_id": pid, "name": info["name"], "role": info.get("role", "attendee")}
            for pid, info in room.peers.items()
        ]
    )


# ---------------------------------------------------------------------------
# Host authentication — claim-once model, like a magic link but for
# privileged actions. Whoever calls /host/claim first for a given room_id
# gets the only host token; there is no login/password system behind it,
# same trust model as the product doc's "Magic Link" registration concept.
# The token is returned exactly once and never stored in plaintext server
# side (only its SHA-256 hash) or logged.
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/host/claim")
@rate_limit("host_claim", limit=5, window_seconds=60)
def claim_host(room_id: str):
    with _lock:
        room = _get_room(room_id)
        if room.host_token_hash is not None:
            return _err("this room already has a host", 409)
        token, expires_at = _issue_host_token(room, room_id)
    log_event(
        logger,
        "host claimed",
        room_id=room_id,
        expires_at=expires_at,
        request_id=getattr(request, "request_id", None),
    )
    return jsonify({"host_token": token, "expires_at": expires_at})


@app.post("/api/room/<room_id>/host/revoke")
@rate_limit("host_revoke", limit=10, window_seconds=60)
def revoke_host(room_id: str):
    """Lets the current, still-valid host invalidate their own token —
    real revocation, not just waiting for expiry. Requires the current
    token as proof of possession (same bar as any other privileged
    action); the room returns to unclaimed and /host/claim is open again
    immediately afterward."""
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        room.host_token_hash = None
        room.host_token_expires_at = None
        store.revoke_host_token(room_id)
    log_event(logger, "host token revoked", room_id=room_id, request_id=getattr(request, "request_id", None))
    return jsonify({"ok": True})


@app.post("/api/room/<room_id>/host/verify")
@rate_limit("host_verify", limit=30, window_seconds=60)
def verify_host(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    is_host = _check_host(room, body.get("host_token"))
    return jsonify({"is_host": is_host, "expires_at": room.host_token_expires_at if is_host else None})


# ---------------------------------------------------------------------------
# Session profile (labels only — see the Room dataclass comment above)
# ---------------------------------------------------------------------------

@app.get("/api/room/<room_id>/profile")
def get_profile(room_id: str):
    room = _get_room(room_id)
    return jsonify(room.profile)


@app.put("/api/room/<room_id>/profile")
@rate_limit("profile_set", limit=10, window_seconds=60)
def set_profile(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)

    mode = body.get("mode")
    video_quality = body.get("video_quality")
    audio_format = body.get("audio_format")
    title = body.get("title")

    if mode is not None and mode not in {m.value for m in BroadcastMode}:
        return _err(f"invalid mode; choose one of {[m.value for m in BroadcastMode]}")
    if video_quality is not None and video_quality not in {q.value for q in VideoQuality}:
        return _err(f"invalid video_quality; choose one of {[q.value for q in VideoQuality]}")
    if audio_format is not None and audio_format not in {a.value for a in AudioFormat}:
        return _err(f"invalid audio_format; choose one of {[a.value for a in AudioFormat]}")

    with _lock:
        room = _get_room(room_id)
        if title is not None:
            room.profile["title"] = title[:200]
        if mode is not None:
            room.profile["mode"] = mode
        if video_quality is not None:
            room.profile["video_quality"] = video_quality
        if audio_format is not None:
            room.profile["audio_format"] = audio_format
        store.save_profile(room_id, room.profile)

    return jsonify(room.profile)


# ---------------------------------------------------------------------------
# WebRTC signaling relay — pure pass-through, server never inspects SDP/ICE
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/signal")
def send_signal(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    to_peer = body.get("to")
    from_peer = body.get("from")
    from_secret = body.get("from_secret")
    msg_type = body.get("type")  # "offer" | "answer" | "ice-candidate"
    payload = body.get("payload")

    if not all([to_peer, from_peer, msg_type]):
        return _err("to, from, and type are required")

    with _lock:
        room = _get_room(room_id)
        if not _check_peer(room, from_peer, from_secret):
            return _err("sender authentication required", 403)
        if to_peer not in room.peers:
            return _err("target peer not in room", 404)
        room.signal_queues[to_peer].append(
            {"type": msg_type, "from": from_peer, "payload": payload}
        )
    return jsonify({"ok": True})


@app.get("/api/room/<room_id>/signal/<peer_id>")
def poll_signal(room_id: str, peer_id: str):
    secret = request.args.get("secret")
    with _lock:
        room = _get_room(room_id)
        if not _check_peer(room, peer_id, secret):
            return _err("peer authentication required", 403)
        messages = room.signal_queues.get(peer_id, [])
        room.signal_queues[peer_id] = []
    return jsonify(messages)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/chat")
@rate_limit("chat", limit=20, window_seconds=10)
def post_chat(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    message, err = _clean_str(body.get("message"), 2000, "message")
    if err:
        return err

    entry = {"id": _next_id(), "name": name, "message": message, "ts": time.time()}
    with _lock:
        room = _get_room(room_id)
        room.chat.append(entry)
        store.add_chat(room_id, entry)
    return jsonify(entry)


@app.get("/api/room/<room_id>/chat")
def get_chat(room_id: str):
    since = int(request.args.get("since", 0))
    room = _get_room(room_id)
    return jsonify([m for m in room.chat if m["id"] > since])


# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/poll")
@rate_limit("poll_create", limit=5, window_seconds=60)
def create_poll(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    question, err = _clean_str(body.get("question"), 300, "question")
    if err:
        return err
    raw_options = body.get("options") or []
    if not isinstance(raw_options, list) or len(raw_options) > 10:
        return _err("options must be a list of at most 10 items")
    options = [o.strip() for o in raw_options if isinstance(o, str) and o.strip()]
    if len(options) < 2:
        return _err("at least 2 non-empty options are required")
    if any(len(o) > 100 for o in options):
        return _err("each option must be 100 characters or fewer")

    poll = {
        "id": _next_id(),
        "question": question,
        "options": options,
        "votes": [0] * len(options),
        "voters": [],  # peer names who've voted, to prevent double-voting
        "created_at": time.time(),
        "open": True,
    }
    with _lock:
        room = _get_room(room_id)
        room.polls.append(poll)
        store.add_poll(room_id, poll)
    return jsonify(poll)


@app.post("/api/room/<room_id>/poll/<int:poll_id>/vote")
@rate_limit("poll_vote", limit=30, window_seconds=60)
def vote_poll(room_id: str, poll_id: int):
    body = request.get_json(force=True, silent=True) or {}
    voter = (body.get("name") or "").strip()
    option_index = body.get("option_index")

    with _lock:
        room = _get_room(room_id)
        poll = next((p for p in room.polls if p["id"] == poll_id), None)
        if not poll:
            return _err("poll not found", 404)
        if not poll["open"]:
            return _err("poll is closed")
        if voter in poll["voters"]:
            return _err("already voted")
        if not isinstance(option_index, int) or not (0 <= option_index < len(poll["options"])):
            return _err("invalid option_index")
        poll["votes"][option_index] += 1
        poll["voters"].append(voter)
        store.update_poll(poll, room_id)
    return jsonify(poll)


@app.post("/api/room/<room_id>/poll/<int:poll_id>/close")
@rate_limit("poll_close", limit=20, window_seconds=60)
def close_poll(room_id: str, poll_id: int):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        poll = next((p for p in room.polls if p["id"] == poll_id), None)
        if not poll:
            return _err("poll not found", 404)
        poll["open"] = False
        store.update_poll(poll, room_id)
    return jsonify(poll)


@app.get("/api/room/<room_id>/poll")
def get_polls(room_id: str):
    room = _get_room(room_id)
    return jsonify(room.polls)


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/qa")
@rate_limit("qa_post", limit=10, window_seconds=60)
def post_question(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    question, err = _clean_str(body.get("question"), 1000, "question")
    if err:
        return err

    entry = {
        "id": _next_id(),
        "name": name,
        "question": question,
        "upvotes": 0,
        "upvoted_by": [],
        "answered": False,
        "ts": time.time(),
    }
    with _lock:
        room = _get_room(room_id)
        room.questions.append(entry)
        store.add_question(room_id, entry)
    return jsonify(entry)


@app.post("/api/room/<room_id>/qa/<int:qid>/upvote")
@rate_limit("qa_upvote", limit=30, window_seconds=60)
def upvote_question(room_id: str, qid: int):
    body = request.get_json(force=True, silent=True) or {}
    voter = (body.get("name") or "").strip()
    with _lock:
        room = _get_room(room_id)
        q = next((q for q in room.questions if q["id"] == qid), None)
        if not q:
            return _err("question not found", 404)
        if voter in q["upvoted_by"]:
            return _err("already upvoted")
        q["upvotes"] += 1
        q["upvoted_by"].append(voter)
        store.update_question(q, room_id)
    return jsonify(q)


@app.post("/api/room/<room_id>/qa/<int:qid>/answered")
@rate_limit("qa_answered", limit=20, window_seconds=60)
def mark_answered(room_id: str, qid: int):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        q = next((q for q in room.questions if q["id"] == qid), None)
        if not q:
            return _err("question not found", 404)
        q["answered"] = True
        store.update_question(q, room_id)
    return jsonify(q)


@app.get("/api/room/<room_id>/qa")
def get_questions(room_id: str):
    room = _get_room(room_id)
    ordered = sorted(room.questions, key=lambda q: (-q["upvotes"], q["ts"]))
    return jsonify(ordered)


# ---------------------------------------------------------------------------
# Reactions — ephemeral floating emoji, capped list, clients poll like chat
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/reaction")
@rate_limit("reaction", limit=30, window_seconds=10)
def post_reaction(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    emoji, err = _clean_str(body.get("emoji"), 8, "emoji")
    if err:
        return err

    entry = {"id": _next_id(), "name": name, "emoji": emoji, "ts": time.time()}
    with _lock:
        room = _get_room(room_id)
        room.reactions.append(entry)
        # Cap so this can't grow unbounded across a long session.
        room.reactions = room.reactions[-200:]
    return jsonify(entry)


@app.get("/api/room/<room_id>/reaction")
def get_reactions(room_id: str):
    since = int(request.args.get("since", 0))
    room = _get_room(room_id)
    return jsonify([r for r in room.reactions if r["id"] > since])


# ---------------------------------------------------------------------------
# Raffle / giveaway
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/raffle/enter")
@rate_limit("raffle_enter", limit=10, window_seconds=60)
def raffle_enter(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    name, err = _clean_str(body.get("name"), 100, "name")
    if err:
        return err
    with _lock:
        room = _get_room(room_id)
        if not any(e["name"] == name for e in room.raffle_entries):
            room.raffle_entries.append({"name": name})
            store.save_raffle(room_id, room.raffle_entries, room.raffle_winner)
    return jsonify({"entries": len(room.raffle_entries)})


@app.post("/api/room/<room_id>/raffle/draw")
@rate_limit("raffle_draw", limit=10, window_seconds=60)
def raffle_draw(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        if not room.raffle_entries:
            return _err("no entries yet")
        room.raffle_winner = random.choice(room.raffle_entries)["name"]
        store.save_raffle(room_id, room.raffle_entries, room.raffle_winner)
    log_event(
        logger,
        "raffle drawn",
        room_id=room_id,
        winner=room.raffle_winner,
        entry_count=len(room.raffle_entries),
        request_id=getattr(request, "request_id", None),
    )
    return jsonify({"winner": room.raffle_winner})


@app.post("/api/room/<room_id>/raffle/reset")
@rate_limit("raffle_reset", limit=10, window_seconds=60)
def raffle_reset(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        room.raffle_entries = []
        room.raffle_winner = None
        store.save_raffle(room_id, room.raffle_entries, room.raffle_winner)
    return jsonify({"ok": True})


@app.get("/api/room/<room_id>/raffle")
def get_raffle(room_id: str):
    room = _get_room(room_id)
    return jsonify(
        {
            "entries": [e["name"] for e in room.raffle_entries],
            "winner": room.raffle_winner,
        }
    )


# ---------------------------------------------------------------------------
# Whiteboard — infinite-canvas-lite: clients submit completed strokes, others
# poll and replay them. No layers/templates/stickies from the product doc —
# just shared freehand drawing, which is the real buildable core of it.
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/whiteboard/stroke")
@rate_limit("whiteboard_stroke", limit=60, window_seconds=10)
def add_stroke(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    points = body.get("points")
    color = (body.get("color") or "#3E8FFF")[:20]
    width = body.get("width", 3)
    if not points or not isinstance(points, list):
        return _err("points (a list of [x,y] pairs) is required")
    if len(points) > 2000:
        return _err("stroke has too many points (max 2000)")
    for p in points:
        if (
            not isinstance(p, list)
            or len(p) != 2
            or not all(isinstance(c, (int, float)) for c in p)
        ):
            return _err("each point must be a [x, y] pair of numbers")
    if not isinstance(width, (int, float)) or not (0 < width <= 50):
        return _err("width must be a number between 0 and 50")

    entry = {"id": _next_id(), "points": points, "color": color, "width": width}
    with _lock:
        room = _get_room(room_id)
        room.whiteboard.append(entry)
        store.add_stroke(room_id, entry)
    return jsonify(entry)


@app.get("/api/room/<room_id>/whiteboard")
def get_strokes(room_id: str):
    since = int(request.args.get("since", 0))
    room = _get_room(room_id)
    return jsonify([s for s in room.whiteboard if s["id"] > since])


@app.post("/api/room/<room_id>/whiteboard/clear")
@rate_limit("whiteboard_clear", limit=10, window_seconds=60)
def clear_whiteboard(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        room.whiteboard = []
        store.clear_strokes(room_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Breakout rooms — each breakout is just another room in this same system
# (its own video mesh + chat), so no new transport is needed. This endpoint
# assigns current peers round-robin into N groups and stamps a start time
# and duration; clients poll their own assignment and auto-recall when the
# timer runs out.
# ---------------------------------------------------------------------------

@app.post("/api/room/<room_id>/breakout/start")
@rate_limit("breakout_start", limit=5, window_seconds=60)
def start_breakout(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)

    count = int(body.get("count", 0))
    duration_seconds = int(body.get("duration_seconds", 300))
    if not (1 <= count <= 200):
        return _err("count must be between 1 and 200")

    with _lock:
        peer_ids = list(room.peers.keys())
        groups: list[list[str]] = [[] for _ in range(count)]
        for i, pid in enumerate(peer_ids):
            groups[i % count].append(pid)
        room.breakout = {
            "count": count,
            "duration_seconds": duration_seconds,
            "started_at": time.time(),
            "groups": groups,
        }
    log_event(
        logger,
        "breakout started",
        room_id=room_id,
        count=count,
        duration_seconds=duration_seconds,
        peer_count=len(peer_ids),
        request_id=getattr(request, "request_id", None),
    )
    return jsonify(room.breakout)


@app.post("/api/room/<room_id>/breakout/end")
@rate_limit("breakout_end", limit=10, window_seconds=60)
def end_breakout(room_id: str):
    body = request.get_json(force=True, silent=True) or {}
    room = _get_room(room_id)
    if not _check_host(room, body.get("host_token")):
        return _err("host authorization required", 403)
    with _lock:
        room.breakout = None
    return jsonify({"ok": True})


@app.get("/api/room/<room_id>/breakout/<peer_id>")
def get_breakout_assignment(room_id: str, peer_id: str):
    secret = request.args.get("secret")
    room = _get_room(room_id)
    if not _check_peer(room, peer_id, secret):
        return _err("peer authentication required", 403)
    bo = room.breakout
    if not bo:
        return jsonify({"active": False})

    elapsed = time.time() - bo["started_at"]
    remaining = max(0, bo["duration_seconds"] - elapsed)
    if remaining <= 0:
        with _lock:
            room.breakout = None
        return jsonify({"active": False})

    group_index = next(
        (i for i, g in enumerate(bo["groups"]) if peer_id in g), None
    )
    if group_index is None:
        return jsonify({"active": False})

    return jsonify(
        {
            "active": True,
            "group_index": group_index,
            "breakout_room_id": f"{room_id}::bo{group_index}",
            "seconds_remaining": int(remaining),
        }
    )


@app.get("/api/room/<room_id>/meeting-info/<peer_id>")
def get_meeting_info(room_id: str, peer_id: str):
    """Meeting ID, passcode, and a shareable join link for a room already
    in progress — so a host or attendee can invite more people without
    leaving the meeting. Gated the same way as the signaling/breakout
    endpoints: only someone who has actually joined this room (proven via
    their peer_secret) can fetch it. That's a deliberate choice, not the
    only reasonable one — see the README's security section for the
    tradeoff (anyone already in counts as trusted enough to invite more
    people; a stricter host-only version would be a one-line change to
    check _check_host instead)."""
    secret = request.args.get("secret")
    room = _get_room(room_id)
    if not _check_peer(room, peer_id, secret):
        return _err("peer authentication required", 403)

    join_link = f"{request.host_url.rstrip('/')}/room?room={room_id}"
    if room.meeting_passcode:
        join_link += f"&passcode={room.meeting_passcode}"

    return jsonify(
        {
            "meeting_id": room_id,
            "meeting_id_display": _format_meeting_id(room_id),
            "passcode": room.meeting_passcode,
            "join_link": join_link,
            "title": room.profile.get("title"),
            "type": room.meeting_type or "meeting",
        }
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "rooms": len(_rooms), "time": time.time()})


if __name__ == "__main__":
    # threaded=True: each connected browser client runs several concurrent
    # long-poll loops (chat, polls, Q&A, reactions, raffle, breakout,
    # whiteboard, profile, signaling — 9 in total). Without this, Flask's
    # single-threaded dev server can only service one of those at a time
    # across ALL connected clients, which starts causing real request
    # queuing with as few as 2 simultaneous users. Confirmed by writing a
    # real-browser test suite (frontend/tests/) that reproduced exactly
    # this queuing as failures until this was enabled. Still just the
    # development server underneath — see README for why that's not a
    # production WSGI server regardless of this flag.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
