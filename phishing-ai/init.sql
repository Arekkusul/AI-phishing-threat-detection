-- Phishing Detection Database Schema
-- This script runs on first container startup

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- Scans Table - Stores all email scan results
-- =============================================================================
CREATE TABLE IF NOT EXISTS scans (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email_hash VARCHAR(64) NOT NULL,

    -- Verdict and scores
    verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('SAFE', 'SUSPICIOUS', 'PHISHING')),
    confidence DECIMAL(5,2) NOT NULL,
    ai_score DECIMAL(5,2),
    sublime_score DECIMAL(5,2),

    -- Detection details (stored as JSONB for flexibility)
    reasons JSONB DEFAULT '[]'::jsonb,
    indicators JSONB DEFAULT '[]'::jsonb,
    check_results JSONB DEFAULT '{}'::jsonb,

    -- Email metadata (minimal retention)
    email_subject VARCHAR(500),
    email_from VARCHAR(255),
    email_to VARCHAR(255),

    -- Audit fields
    scanned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    scanned_by VARCHAR(255),
    ip_address INET,

    -- Indexes for common queries
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for lookups by email hash (deduplication)
CREATE INDEX IF NOT EXISTS idx_scans_email_hash ON scans(email_hash);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at DESC);

-- Index for verdict filtering
CREATE INDEX IF NOT EXISTS idx_scans_verdict ON scans(verdict);

-- =============================================================================
-- Reports Table - Tracks reported phishing emails
-- =============================================================================
CREATE TABLE IF NOT EXISTS reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scan_id UUID REFERENCES scans(id) ON DELETE SET NULL,
    email_hash VARCHAR(64) NOT NULL,

    -- Report details
    reporter_email VARCHAR(255),
    reporter_name VARCHAR(255),

    -- Verdict at time of report
    verdict VARCHAR(20) NOT NULL,
    confidence DECIMAL(5,2) NOT NULL,

    -- Notification status
    teams_notified BOOLEAN DEFAULT FALSE,
    telegram_notified BOOLEAN DEFAULT FALSE,
    whatsapp_notified BOOLEAN DEFAULT FALSE,

    -- Timestamps
    reported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    -- Additional notes
    notes TEXT
);

-- Index for reporter lookups
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_email);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_reports_reported_at ON reports(reported_at DESC);

-- =============================================================================
-- API Keys Table (for future multi-tenant support)
-- =============================================================================
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,

    -- Permissions
    can_scan BOOLEAN DEFAULT TRUE,
    can_report BOOLEAN DEFAULT TRUE,
    can_admin BOOLEAN DEFAULT FALSE,

    -- Rate limiting
    rate_limit_per_minute INTEGER DEFAULT 60,

    -- Status
    is_active BOOLEAN DEFAULT TRUE,

    -- Audit
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE
);

-- =============================================================================
-- Audit Log Table
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,
    event_data JSONB,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for event type filtering
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC);

-- =============================================================================
-- Views for Admin Panel
-- =============================================================================

-- Recent scans summary
CREATE OR REPLACE VIEW recent_scans_summary AS
SELECT
    DATE(scanned_at) as scan_date,
    COUNT(*) as total_scans,
    COUNT(*) FILTER (WHERE verdict = 'SAFE') as safe_count,
    COUNT(*) FILTER (WHERE verdict = 'SUSPICIOUS') as suspicious_count,
    COUNT(*) FILTER (WHERE verdict = 'PHISHING') as phishing_count,
    AVG(confidence) as avg_confidence
FROM scans
WHERE scanned_at > CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(scanned_at)
ORDER BY scan_date DESC;

-- Top reported domains (for threat intelligence)
CREATE OR REPLACE VIEW top_reported_domains AS
SELECT
    s.email_from,
    COUNT(*) as report_count,
    AVG(r.confidence) as avg_confidence
FROM reports r
JOIN scans s ON r.scan_id = s.id
WHERE r.reported_at > CURRENT_DATE - INTERVAL '30 days'
GROUP BY s.email_from
ORDER BY report_count DESC
LIMIT 50;

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO phishing;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO phishing;
