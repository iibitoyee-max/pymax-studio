<?php
declare(strict_types=1);
require __DIR__ . '/db.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    header('Location: index.php');
    exit;
}

if (!prymax_verify_csrf($_POST['csrf_token'] ?? null)) {
    http_response_code(403);
    echo 'This form has expired or was submitted from an untrusted source. Please go back and try again.';
    exit;
}

$slug = trim($_POST['event'] ?? '');
$name = trim($_POST['name'] ?? '');
$email = trim($_POST['email'] ?? '');

$event = $slug !== '' ? prymax_find_event($slug) : null;

if ($event === null || $name === '' || strlen($name) > 200 || !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    http_response_code(422);
    echo 'Registration failed: please provide a valid name, email, and event.';
    exit;
}

$attendee = prymax_register_attendee($slug, $name, $email);

// The magic link drops the attendee straight into the video room with no
// login step — PRYMAX_ROOM_BASE points at the Python signaling app's
// front-end room page, keyed by the event slug as the room id.
$roomBase = getenv('PRYMAX_ROOM_BASE') ?: 'http://localhost:5000/room';
$joinUrl = $roomBase . '?room=' . urlencode($slug)
    . '&token=' . urlencode($attendee['magic_token'])
    . '&name=' . urlencode($attendee['name']);
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>You're registered — <?= htmlspecialchars($event['title']) ?></title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500&display=swap');
  :root { --bg:#0D0E12; --panel:#1A1C23; --signal:#3E8FFF; --text:#E8E9ED; --text-dim:#8A8F98; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; padding: 24px;
  }
  .card { width: 100%; max-width: 440px; background: var(--panel); border: 1px solid #262832; border-radius: 4px; padding: 40px; }
  h1 { font-family: 'Space Grotesk', sans-serif; font-size: 22px; margin: 0 0 12px; }
  p { color: var(--text-dim); font-size: 14px; line-height: 1.6; }
  .join-btn {
    display: inline-block; margin-top: 20px; background: var(--signal); color: #0D0E12;
    text-decoration: none; padding: 13px 22px; border-radius: 3px; font-size: 14px; font-weight: 500;
  }
  code { color: var(--signal); word-break: break-all; }
</style>
</head>
<body>
  <div class="card">
    <h1>You're in, <?= htmlspecialchars($attendee['name']) ?>.</h1>
    <p>Your seat for <strong><?= htmlspecialchars($event['title']) ?></strong> is confirmed. This link is your ticket — no password needed:</p>
    <p><code><?= htmlspecialchars($joinUrl) ?></code></p>
    <a class="join-btn" href="<?= htmlspecialchars($joinUrl) ?>">Enter the studio →</a>
  </div>
</body>
</html>
