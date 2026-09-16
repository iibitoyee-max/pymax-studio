-- PryMax Studio — reference schema for a future durable-storage backend
-- ============================================================================
-- STATUS: NOT CONNECTED. Nothing in backend/app.py reads or writes this.
-- The running app stores everything in an in-memory Python dict (see
-- backend/app.py) which is simple, fully tested, and fine for a demo — but
-- wiped on restart and not shared across multiple server processes.
--
-- This file is a legitimate, well-formed Postgres schema (works on plain
-- Postgres or Supabase, which is Postgres underneath) for whoever wants to
-- move chat/polls/Q&A/sessions to durable storage later. It has NOT been
-- run against a real database in this environment — there's no Postgres
-- instance or network access here to test it against. Review and test it
-- yourself before relying on it. A few things to check before adopting it:
--   - The RLS policies assume Supabase Auth's auth.uid() — replace with your
--     own auth scheme if you're not using Supabase.
--   - Table names/columns don't yet match the JSON shapes app.py returns
--     (e.g. app.py's chat entries are {id, name, message, ts}) — you'd need
--     a thin adapter layer either way, not a drop-in swap.
-- ============================================================================

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    settings JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    organization_id UUID REFERENCES organizations(id),
    role TEXT DEFAULT 'member',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE broadcast_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_slug TEXT UNIQUE NOT NULL,             -- matches app.py's room_id
    title TEXT,
    mode VARCHAR(20) NOT NULL DEFAULT 'webrtc', -- see BroadcastMode in app.py
    video_quality VARCHAR(10) NOT NULL DEFAULT '1080p',
    audio_format VARCHAR(10) NOT NULL DEFAULT 'aac',
    organization_id UUID REFERENCES organizations(id),
    status VARCHAR(20) DEFAULT 'active',
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ended_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES broadcast_sessions(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE polls (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES broadcast_sessions(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    options JSONB NOT NULL,        -- e.g. ["Yes", "No"]
    votes JSONB NOT NULL DEFAULT '[]'::jsonb,   -- parallel int array
    is_open BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE poll_votes (
    id BIGSERIAL PRIMARY KEY,
    poll_id BIGINT REFERENCES polls(id) ON DELETE CASCADE,
    voter_name TEXT NOT NULL,
    option_index INT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (poll_id, voter_name)
);

CREATE TABLE qa_questions (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES broadcast_sessions(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    upvotes INT DEFAULT 0,
    answered BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE qa_upvotes (
    id BIGSERIAL PRIMARY KEY,
    question_id BIGINT REFERENCES qa_questions(id) ON DELETE CASCADE,
    voter_name TEXT NOT NULL,
    UNIQUE (question_id, voter_name)
);

CREATE TABLE attendees (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES broadcast_sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    magic_token TEXT UNIQUE NOT NULL,   -- matches registration/db.php's attendees table
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (session_id, email)
);

CREATE INDEX idx_chat_session ON chat_messages(session_id);
CREATE INDEX idx_polls_session ON polls(session_id);
CREATE INDEX idx_qa_session ON qa_questions(session_id);
CREATE INDEX idx_attendees_session ON attendees(session_id);

-- Row Level Security — only meaningful if you're using Supabase Auth.
-- Delete these if you're running plain Postgres with your own auth.
ALTER TABLE broadcast_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "org members can see their sessions" ON broadcast_sessions
    FOR SELECT USING (
        organization_id IN (SELECT organization_id FROM users WHERE id = auth.uid())
    );
