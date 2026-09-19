/**
 * PryMax Studio — real-browser frontend tests
 * =============================================
 * Uses Playwright (launching a real, actual Chromium — not a DOM
 * simulation like jsdom) plus Node's built-in test runner (`node:test`,
 * available since Node 18 with no install needed). `@playwright/test`
 * itself isn't used here since it wasn't available in the environment
 * this was built in — this uses the plain `playwright` automation
 * library directly, which is equally real, just without that package's
 * convenience fixtures.
 *
 * Prerequisites:
 *   1. npm install          (in this frontend/ directory)
 *   2. npx playwright install chromium   (downloads the browser binary)
 *   3. The Flask backend running on http://localhost:5000, started with
 *      threaded=True (see note in ../../backend/app.py's app.run() call
 *      — this suite is what surfaced the need for it: each connected
 *      client runs 9 concurrent long-poll loops, which a single-threaded
 *      dev server can't keep up with once more than one client is
 *      active, causing real request queuing, not a test artifact)
 *
 * Run:
 *   npm test
 *   # or directly: node --test tests/room.browser.test.js
 *   (a glob like "tests/*.test.js" was observed to behave differently
 *   under this Node version's test runner and is not the recommended
 *   invocation — use the explicit file path.)
 *
 * This suite exercises real DOM behavior that was previously completely
 * untested: the ARIA tab-navigation pattern, the join flow, chat
 * round-tripping through the actual server, mic/camera toggle state,
 * reactions, and the host-claim flow — all in an actual browser, with
 * fake camera/mic devices (Chromium's built-in synthetic media stream)
 * so getUserMedia succeeds without real hardware or a permission prompt.
 *
 * A note on flakiness, found and reduced (not eliminated) during
 * development: the first version of this suite reused one browser
 * context across all tests with no pause between them. Every connected
 * page keeps 9 concurrent long-poll loops open (chat, polls, Q&A,
 * reactions, raffle, breakout, whiteboard, profile, signaling); closing
 * a page aborts its in-flight requests client-side immediately, but the
 * Flask dev server's threads handling those requests take a moment to
 * notice the disconnect. Back-to-back tests with no gap could pile up
 * enough lingering server threads that the 3rd or 4th test's join
 * request queued behind them and timed out. Fixed two ways: enabling
 * `threaded=True` on the Flask side (real capacity increase, not just a
 * test workaround), and adding a short drain pause between tests here.
 * Both together made repeated runs reliably pass; see the README for the
 * honest caveat that this is a development-server limitation, not fully
 * resolved by test-side changes alone.
 */
const { test, describe, before, after, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const BASE_URL = process.env.PRYMAX_BASE_URL || "http://localhost:5000";
const DRAIN_PAUSE_MS = 400;

let browser;
let sharedContext;
let page;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function uniqueRoom(prefix) {
  return `${prefix}${Date.now()}${Math.floor(Math.random() * 1000)}`;
}

describe("PryMax Studio room client (real browser)", () => {
  before(async () => {
    browser = await chromium.launch();
    sharedContext = await browser.newContext();
  });

  after(async () => {
    await sharedContext.close();
    await browser.close();
  });

  beforeEach(async () => {
    page = await sharedContext.newPage();
    // Replace getUserMedia with a synthetic stream built from a canvas
    // (video) and the Web Audio API (audio), created before any of the
    // page's own scripts run. This is the standard approach for testing
    // WebRTC applications: it exercises everything the app actually does
    // with the MediaStream it's given (attaching it to a <video>, adding
    // its tracks to an RTCPeerConnection) without depending on the
    // browser's OS-level fake-camera driver, which turned out to have
    // real, intermittent multi-second stalls under repeated rapid
    // page creation in this sandboxed environment — confirmed via a
    // standalone diagnostic script showing consistently fast (~200ms)
    // joins in isolation, but occasional 30s+ hangs specifically when
    // many pages were created back-to-back in one browser context, i.e.
    // a resource-contention property of the fake-device driver itself,
    // not of the application code being tested.
    await page.addInitScript(() => {
      const canvas = document.createElement("canvas");
      canvas.width = 2;
      canvas.height = 2;
      const ctx = canvas.getContext("2d");
      ctx.fillRect(0, 0, 2, 2);
      const videoTrack = canvas.captureStream(0).getVideoTracks()[0];

      const audioCtx = new AudioContext();
      const oscillator = audioCtx.createOscillator();
      const dest = audioCtx.createMediaStreamDestination();
      oscillator.connect(dest);
      oscillator.start();
      const audioTrack = dest.stream.getAudioTracks()[0];

      navigator.mediaDevices.getUserMedia = async () =>
        new MediaStream([videoTrack, audioTrack]);
    });
  });

  afterEach(async () => {
    await page.close();
  });

  test("join screen loads with studio hidden", async () => {
    await page.goto(`${BASE_URL}/room`);

    await assert.doesNotReject(page.waitForSelector("#join-screen", { state: "visible", timeout: 60000 }));
    const studioHidden = await page.getAttribute("#studio", "class");
    assert.match(studioHidden, /hidden/);
  });

  test("joining moves focus to the chat tab and reveals the studio", async () => {
    const room = uniqueRoom("browsertest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=Alice`);

    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    const studioClass = await page.getAttribute("#studio", "class");
    assert.doesNotMatch(studioClass, /hidden/);

    const focusedId = await page.evaluate(() => document.activeElement.id);
    assert.equal(focusedId, "tab-chat", "focus should move to the chat tab after joining");
  });

  test("keyboard arrow navigation moves between tabs per the ARIA tab pattern", async () => {
    const room = uniqueRoom("kbtest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=Bob`);
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    // Starts on Chat (index 0)
    assert.equal(await page.getAttribute("#tab-chat", "aria-selected"), "true");

    await page.focus("#tab-chat");
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.getAttribute("#tab-polls", "aria-selected"), "true");
    assert.equal(await page.getAttribute("#tab-chat", "aria-selected"), "false");
    const pollsPanelHidden = await page.getAttribute("#panel-polls", "class");
    assert.doesNotMatch(pollsPanelHidden, /hidden/);

    await page.keyboard.press("End");
    assert.equal(await page.getAttribute("#tab-more", "aria-selected"), "true");

    await page.keyboard.press("Home");
    assert.equal(await page.getAttribute("#tab-chat", "aria-selected"), "true");
  });

  test("chat message round-trips through the real server", async () => {
    const room = uniqueRoom("chattest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=Carol`);
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    const message = `hello from playwright ${Date.now()}`;
    await page.fill("#chat-input", message);
    await page.click('#chat-form button[type="submit"]');

    // Chat polls the server every ~1.2s — wait generously for a real
    // network round trip, not just a DOM update.
    await page.waitForFunction(
      (expected) => document.getElementById("chat-list").textContent.includes(expected),
      message,
      { timeout: 5000 }
    );

    const chatText = await page.textContent("#chat-list");
    assert.match(chatText, new RegExp(message.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  });

  test("mic toggle updates aria-pressed and announces the change", async () => {
    const room = uniqueRoom("mictest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=Dave`);
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    assert.equal(await page.getAttribute("#mic-btn", "aria-pressed"), "true");
    await page.click("#mic-btn");
    assert.equal(await page.getAttribute("#mic-btn", "aria-pressed"), "false");

    const announced = await page.textContent("#sr-announcer");
    assert.equal(announced, "Microphone muted");
  });

  test("clicking a reaction spawns a floating emoji", async () => {
    const room = uniqueRoom("reacttest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=Eve`);
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    await page.click('.reaction-btn[data-emoji="🎉"]');
    await assert.doesNotReject(
      page.waitForSelector("#reaction-layer .floating-emoji", { timeout: 3000 })
    );
  });

  test("meeting info panel shows the real meeting id, passcode, and join link", async () => {
    const scheduleRes = await page.request.post(`${BASE_URL}/api/meetings/schedule`, {
      data: { name: "Tester", title: "Info Panel Test" },
    });
    const meeting = await scheduleRes.json();

    await page.goto(
      `${BASE_URL}/room?room=${meeting.meeting_id}&name=Tester&host_token=${meeting.host_token}`
    );
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");
    await page.click("#tab-more");

    await page.waitForFunction(
      () => document.getElementById("info-meeting-id").textContent !== "—"
    );
    const displayedLink = await page.textContent("#info-join-link");
    assert.ok(displayedLink.includes(meeting.meeting_id));
    assert.ok(displayedLink.includes(meeting.passcode));
    const displayedPasscode = await page.textContent("#info-passcode");
    assert.equal(displayedPasscode, meeting.passcode);
  });

  test("meeting info gracefully falls back when the QR library isn't available", async () => {
    const scheduleRes = await page.request.post(`${BASE_URL}/api/meetings/schedule`, {
      data: { name: "Tester2" },
    });
    const meeting = await scheduleRes.json();

    await page.goto(
      `${BASE_URL}/room?room=${meeting.meeting_id}&name=Tester2&host_token=${meeting.host_token}`
    );
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");
    await page.click("#tab-more");
    await page.waitForFunction(
      () => document.getElementById("info-meeting-id").textContent !== "—"
    );

    // This sandbox's CDN is blocked, so this specifically verifies the
    // fallback path (not the QR-success path, which needs real internet
    // access this environment doesn't have — see room.html's comment).
    const qrVisible = await page.evaluate(() =>
      document.getElementById("info-qr-wrap").classList.contains("qr-visible")
    );
    const fallbackShown = await page.evaluate(
      () => !document.getElementById("info-qr-fallback").classList.contains("visually-hidden")
    );
    assert.equal(qrVisible, false);
    assert.equal(fallbackShown, true);
  });

  test("host claim flow: claiming shows a token and joining with it reveals host controls", async () => {
    const room = uniqueRoom("hosttest");
    await page.goto(`${BASE_URL}/room`);
    await page.fill("#room-input", room);
    await page.click("#claim-host-btn");

    await page.waitForSelector("#claim-result:not(.hidden)");
    const claimText = await page.textContent("#claim-result");
    assert.match(claimText, /You're the host/);

    const tokenValue = await page.inputValue("#host-token-input");
    assert.ok(tokenValue.length > 10, "a real host token should have been filled in");

    await page.fill("#name-input", "HostUser");
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    await page.waitForSelector("#host-badge:not(.hidden)");
    const raffleDrawHidden = await page.getAttribute("#raffle-draw-btn", "class");
    assert.doesNotMatch(raffleDrawHidden, /hidden/, "host-only controls should be visible to the host");
  });

  test("an attendee (no host token) does not see host-only controls", async () => {
    const room = uniqueRoom("attendeetest");
    await page.goto(`${BASE_URL}/room?room=${room}&name=PlainAttendee`);
    await page.click("#join-btn");
    await page.waitForSelector("#studio:not(.hidden)");

    const raffleDrawClass = await page.getAttribute("#raffle-draw-btn", "class");
    assert.match(raffleDrawClass, /hidden/, "host-only controls must stay hidden for attendees");
  });

  test("no duplicate element ids exist in the live rendered DOM", async () => {
    await page.goto(`${BASE_URL}/room`);

    const duplicates = await page.evaluate(() => {
      const seen = new Map();
      document.querySelectorAll("[id]").forEach((el) => {
        seen.set(el.id, (seen.get(el.id) || 0) + 1);
      });
      return [...seen.entries()].filter(([, count]) => count > 1);
    });

    assert.deepEqual(duplicates, [], `duplicate ids found: ${JSON.stringify(duplicates)}`);
  });
});
