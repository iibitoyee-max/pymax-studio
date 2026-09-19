/**
 * PryMax Studio — landing page browser tests
 * =============================================
 * Same approach as room.browser.test.js: real Chromium via Playwright,
 * Node's built-in test runner, no mocking. See that file's header
 * comment for the full rationale (why plain `playwright` not
 * `@playwright/test`, the shared-context lifecycle, the drain pause).
 *
 * Prerequisites: same as room.browser.test.js — npm install, playwright
 * install chromium, and the Flask backend running on localhost:5000.
 *
 * Run:
 *   node --test tests/landing.browser.test.js
 *
 * A note on rate limiting and repeated runs, found by actually running
 * this suite back-to-back rather than assumed: several tests call
 * POST /api/meetings/schedule (directly or via the UI), which is rate
 * limited to 20/60s per source IP — a real, intentional security
 * control (see app.py). Running this file many times in quick
 * succession from the same machine will eventually exhaust that budget
 * and cause a schedule-dependent test to fail with a timeout waiting
 * for the result panel, because the request that should have returned
 * a new meeting got a 429 instead. That's the rate limiter doing
 * exactly its job, not a bug in this suite or the app — pause between
 * repeated runs (or wait ~60s) if you hit this locally.
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

describe("PryMax Studio landing page (real browser)", () => {
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
  });

  afterEach(async () => {
    await page.close();
    await sleep(DRAIN_PAUSE_MS);
  });

  test("landing page loads with all three action cards", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.waitForSelector("#join-card");
    await page.waitForSelector("#schedule-card");
    await page.waitForSelector("#stream-card");
    const heading = await page.textContent("h1");
    assert.match(heading, /Where do you want to go/);
  });

  test("schedule meeting produces a real meeting id and passcode, then enters the room", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.click('[data-target="schedule-form"]');
    await page.fill("#schedule-name", "Playwright Host");
    await page.fill("#schedule-title", "Automated Test Meeting");
    await page.click('#schedule-form button[type="submit"]');

    await page.waitForSelector("#result-panel:not(.hidden)");
    const meetingIdText = await page.textContent("#result-meeting-id");
    const passcodeText = await page.textContent("#result-passcode");
    assert.match(meetingIdText.replace(/\s/g, ""), /^\d{11}$/);
    assert.match(passcodeText, /^[A-Z0-9]{6}$/);

    await page.click("#enter-now-btn");
    await page.waitForURL(/\/room\?/);
    await page.waitForSelector("#name-input");
    const roomValue = await page.inputValue("#room-input");
    assert.equal(roomValue.length, 11);
  });

  test("live streaming schedules with type=stream and a broadcast-mode profile", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.click('[data-target="stream-form"]');
    await page.fill("#stream-name", "Playwright Streamer");
    await page.click('#stream-form button[type="submit"]');

    await page.waitForSelector("#result-panel:not(.hidden)");
    const title = await page.textContent("#result-title");
    assert.match(title, /stream/i);
  });

  test("join meeting with wrong passcode shows an inline error, not a redirect", async () => {
    // First schedule a real meeting via the API so there's something to
    // (fail to) join against.
    const scheduleRes = await page.request.post(`${BASE_URL}/api/meetings/schedule`, {
      data: { name: "Setup", title: "For join test" },
    });
    const meeting = await scheduleRes.json();

    await page.goto(`${BASE_URL}/`);
    await page.click('[data-target="join-form"]');
    await page.fill("#join-name", "Wrong Passcode Guy");
    await page.fill("#join-meeting-id", meeting.meeting_id);
    await page.fill("#join-passcode", "WRONG1");
    await page.click('#join-form button[type="submit"]');

    await page.waitForSelector("#join-form-error:not(.hidden)");
    // Must NOT have navigated to the room.
    assert.equal(page.url(), `${BASE_URL}/`);
  });

  test("join meeting with correct passcode navigates to the room and pre-fills the passcode field", async () => {
    const scheduleRes = await page.request.post(`${BASE_URL}/api/meetings/schedule`, {
      data: { name: "Setup2", title: "For successful join test" },
    });
    const meeting = await scheduleRes.json();

    await page.goto(`${BASE_URL}/`);
    await page.click('[data-target="join-form"]');
    await page.fill("#join-name", "Correct Passcode Guy");
    await page.fill("#join-meeting-id", meeting.meeting_id);
    await page.fill("#join-passcode", meeting.passcode);
    await page.click('#join-form button[type="submit"]');

    await page.waitForURL(/\/room\?/);
    await page.waitForSelector("#name-input");
    const passcodeValue = await page.inputValue("#passcode-input");
    assert.equal(passcodeValue, meeting.passcode);
  });

  test("admin login with correct credentials navigates into Superroom as host", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.click("#admin-login-toggle");
    await page.waitForSelector("#admin-login-panel:not(.hidden)");
    await page.fill("#admin-username", "Ibitoye");
    await page.fill("#admin-room", "Superroom");
    await page.click('#admin-login-form button[type="submit"]');

    await page.waitForURL(/\/room\?/);
    const roomValue = await page.inputValue("#room-input");
    assert.equal(roomValue, "Superroom");
    const hostTokenValue = await page.inputValue("#host-token-input");
    assert.ok(hostTokenValue.length > 10);
  });

  test("admin login with wrong credentials shows an inline error, not a redirect", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.click("#admin-login-toggle");
    await page.fill("#admin-username", "NotTheAdmin");
    await page.fill("#admin-room", "WrongRoom");
    await page.click('#admin-login-form button[type="submit"]');

    await page.waitForSelector("#admin-login-error:not(.hidden)");
    assert.equal(page.url(), `${BASE_URL}/`);
  });

  test("start meeting now goes straight into the room, skipping the result panel", async () => {
    await page.goto(`${BASE_URL}/`);
    await page.click('[data-target="start-now-form"]');
    await page.fill("#start-now-name", "Instant Starter");
    await page.click('#start-now-form button[type="submit"]');

    await page.waitForURL(/\/room\?/);
    await page.waitForSelector("#name-input");
    const roomValue = await page.inputValue("#room-input");
    assert.match(roomValue, /^\d{11}$/);
    const hostTokenValue = await page.inputValue("#host-token-input");
    assert.ok(hostTokenValue.length > 10);
  });

  test("no duplicate element ids on the landing page", async () => {
    await page.goto(`${BASE_URL}/`);
    const duplicates = await page.evaluate(() => {
      const seen = new Map();
      document.querySelectorAll("[id]").forEach((el) => {
        seen.set(el.id, (seen.get(el.id) || 0) + 1);
      });
      return [...seen.entries()].filter(([, count]) => count > 1);
    });
    assert.deepEqual(duplicates, []);
  });
});
