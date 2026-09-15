<?php
/**
 * PryMax Studio — Registration DB layer
 * SQLite via PDO so this runs with zero setup (php -S is enough to demo it).
 * Swap the DSN below for a mysql: DSN in production without touching callers.
 */

declare(strict_types=1);

function prymax_db(): PDO
{
    static $pdo = null;
    if ($pdo !== null) {
        return $pdo;
    }

    $dbPath = __DIR__ . '/prymax.sqlite';
    $pdo = new PDO('sqlite:' . $dbPath);
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

    $pdo->exec(
        'CREATE TABLE IF NOT EXISTS attendees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            magic_token TEXT NOT NULL UNIQUE,
            registered_at INTEGER NOT NULL,
            UNIQUE(event_slug, email)
        )'
    );

    $pdo->exec(
        'CREATE TABLE IF NOT EXISTS events (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT \'\'
        )'
    );

    // Seed one demo event if the table is empty, so index.php has something
    // to register against out of the box.
    $count = (int) $pdo->query('SELECT COUNT(*) FROM events')->fetchColumn();
    if ($count === 0) {
        $stmt = $pdo->prepare(
            'INSERT INTO events (slug, title, starts_at, description) VALUES (?, ?, ?, ?)'
        );
        $stmt->execute([
            'q3-product-briefing',
            'PryMax Studio — Q3 Product Briefing',
            date('Y-m-d H:i', strtotime('+3 days')),
            'A live walkthrough of the roadmap, open to press and partners.',
        ]);
    }

    return $pdo;
}

function prymax_find_event(string $slug): ?array
{
    $stmt = prymax_db()->prepare('SELECT * FROM events WHERE slug = ?');
    $stmt->execute([$slug]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);
    return $row === false ? null : $row;
}

function prymax_register_attendee(string $eventSlug, string $name, string $email): array
{
    $pdo = prymax_db();

    // Idempotent: re-registering with the same email returns the existing
    // magic link instead of erroring, since attendees often resubmit.
    $existing = $pdo->prepare(
        'SELECT * FROM attendees WHERE event_slug = ? AND email = ?'
    );
    $existing->execute([$eventSlug, $email]);
    $row = $existing->fetch(PDO::FETCH_ASSOC);
    if ($row !== false) {
        return $row;
    }

    $token = bin2hex(random_bytes(16));
    $stmt = $pdo->prepare(
        'INSERT INTO attendees (event_slug, name, email, magic_token, registered_at)
         VALUES (?, ?, ?, ?, ?)'
    );
    $stmt->execute([$eventSlug, $name, $email, $token, time()]);

    $id = (int) $pdo->lastInsertId();
    return [
        'id' => $id,
        'event_slug' => $eventSlug,
        'name' => $name,
        'email' => $email,
        'magic_token' => $token,
        'registered_at' => time(),
    ];
}

function prymax_find_attendee_by_token(string $token): ?array
{
    $stmt = prymax_db()->prepare('SELECT * FROM attendees WHERE magic_token = ?');
    $stmt->execute([$token]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);
    return $row === false ? null : $row;
}

/**
 * CSRF protection for the registration form. A session-bound random token
 * is embedded as a hidden field in index.php's form and checked in
 * register.php before anything is written to the database. This stops a
 * malicious third-party page from silently submitting registrations on a
 * visitor's behalf using their logged-in session/cookies.
 */
function prymax_start_session(): void
{
    if (session_status() === PHP_SESSION_NONE) {
        session_start();
    }
}

function prymax_csrf_token(): string
{
    prymax_start_session();
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

function prymax_verify_csrf(?string $submittedToken): bool
{
    prymax_start_session();
    return is_string($submittedToken)
        && isset($_SESSION['csrf_token'])
        && hash_equals($_SESSION['csrf_token'], $submittedToken);
}
