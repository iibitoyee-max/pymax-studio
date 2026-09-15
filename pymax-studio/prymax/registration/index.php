<?php
declare(strict_types=1);
require __DIR__ . '/db.php';

$slug = $_GET['event'] ?? 'q3-product-briefing';
$event = prymax_find_event($slug);

if ($event === null) {
    http_response_code(404);
    echo 'Event not found.';
    exit;
}

// Must happen before any HTML is echoed — session_start() (inside this
// call) sets a cookie header, which PHP can only do before output begins.
$csrfToken = prymax_csrf_token();

$error = null;
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title><?= htmlspecialchars($event['title']) ?> — Register</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500&display=swap');

  :root {
    --bg: #0D0E12;
    --panel: #1A1C23;
    --signal: #3E8FFF;
    --tally: #FF5B54;
    --text: #E8E9ED;
    --text-dim: #8A8F98;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    padding: 24px;
  }
  .card {
    width: 100%;
    max-width: 440px;
    background: var(--panel);
    border: 1px solid #262832;
    border-radius: 4px;
    padding: 40px;
  }
  .tally {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 12px;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin-bottom: 20px;
  }
  .tally-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--tally);
  }
  h1 {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 24px;
    line-height: 1.3;
    margin: 0 0 8px;
  }
  p.desc {
    color: var(--text-dim);
    font-size: 14px;
    line-height: 1.6;
    margin: 0 0 28px;
  }
  .starts-at {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 13px;
    color: var(--signal);
    margin-bottom: 28px;
  }
  label {
    display: block;
    font-size: 12px;
    color: var(--text-dim);
    margin-bottom: 6px;
  }
  input {
    width: 100%;
    background: #0D0E12;
    border: 1px solid #2C2F3A;
    border-radius: 3px;
    padding: 11px 12px;
    color: var(--text);
    font-size: 14px;
    margin-bottom: 18px;
    font-family: 'Inter', sans-serif;
  }
  input:focus {
    outline: none;
    border-color: var(--signal);
  }
  button {
    width: 100%;
    background: var(--signal);
    color: #0D0E12;
    border: none;
    border-radius: 3px;
    padding: 13px;
    font-size: 14px;
    font-weight: 500;
    font-family: 'Inter', sans-serif;
    cursor: pointer;
  }
  button:hover { opacity: 0.9; }
  .error {
    background: rgba(255,91,84,0.1);
    border: 1px solid var(--tally);
    color: var(--tally);
    font-size: 13px;
    padding: 10px 12px;
    border-radius: 3px;
    margin-bottom: 18px;
  }
</style>
</head>
<body>
  <div class="card">
    <div class="tally"><span class="tally-dot"></span>REGISTRATION OPEN</div>
    <h1><?= htmlspecialchars($event['title']) ?></h1>
    <p class="desc"><?= htmlspecialchars($event['description']) ?></p>
    <div class="starts-at">STARTS <?= htmlspecialchars($event['starts_at']) ?></div>

    <div id="error-slot"></div>

    <form method="POST" action="register.php">
      <input type="hidden" name="event" value="<?= htmlspecialchars($slug) ?>">
      <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken) ?>">
      <label for="name">Full name</label>
      <input type="text" id="name" name="name" required autocomplete="name">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required autocomplete="email">
      <button type="submit">Reserve your seat</button>
    </form>
  </div>
</body>
</html>
