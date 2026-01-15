"""
Database integration module for Phishing Detection API.

Provides connection pooling and CRUD operations for:
- Scans: Email scan results with full check details
- Reports: User-reported phishing emails
- API Keys: Multi-tenant authentication
- Audit Log: Request/event logging
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
from dataclasses import asdict

import psycopg2
from psycopg2 import pool, extras

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Base exception for database operations."""
    pass


class Database:
    """
    Database connection manager with connection pooling.
    Thread-safe for use with Flask.
    """

    _instance: Optional['Database'] = None
    _pool: Optional[pool.ThreadedConnectionPool] = None

    def __init__(self, database_url: Optional[str] = None, min_connections: int = 2, max_connections: int = 10):
        """
        Initialize database connection pool.

        Args:
            database_url: PostgreSQL connection string
            min_connections: Minimum pool size
            max_connections: Maximum pool size
        """
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.min_connections = min_connections
        self.max_connections = max_connections
        self._initialized = False

    def initialize(self) -> bool:
        """
        Initialize the connection pool and create tables if needed.
        Returns True if successful, False otherwise.
        """
        if self._initialized:
            return True

        if not self.database_url:
            logger.warning("DATABASE_URL not configured - database features disabled")
            return False

        try:
            Database._pool = pool.ThreadedConnectionPool(
                self.min_connections,
                self.max_connections,
                self.database_url
            )
            self._initialized = True
            logger.info(f"Database connection pool initialized (min={self.min_connections}, max={self.max_connections})")

            # Auto-create tables if they don't exist
            self._create_tables_if_needed()

            return True
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            return False

    def _create_tables_if_needed(self):
        """Create database tables if they don't exist."""
        try:
            conn = Database._pool.getconn()
            try:
                with conn.cursor() as cur:
                    # Check if scans table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_schema = 'public'
                            AND table_name = 'scans'
                        )
                    """)
                    tables_exist = cur.fetchone()[0]

                    if not tables_exist:
                        logger.info("Database tables not found - creating schema...")
                        self._run_init_sql(cur)
                        conn.commit()
                        logger.info("Database schema created successfully")
                    else:
                        logger.info("Database tables already exist")
            finally:
                Database._pool.putconn(conn)
        except Exception as e:
            logger.error(f"Failed to check/create tables: {e}")

    def _run_init_sql(self, cursor):
        """Run the initialization SQL to create all tables."""
        init_sql = """
        -- Enable UUID extension
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

        -- Scans Table
        CREATE TABLE IF NOT EXISTS scans (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            email_hash VARCHAR(64) NOT NULL,
            verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('SAFE', 'SUSPICIOUS', 'PHISHING')),
            confidence DECIMAL(5,2) NOT NULL,
            ai_score DECIMAL(5,2),
            sublime_score DECIMAL(5,2),
            reasons JSONB DEFAULT '[]'::jsonb,
            indicators JSONB DEFAULT '[]'::jsonb,
            check_results JSONB DEFAULT '{}'::jsonb,
            email_subject VARCHAR(500),
            email_from VARCHAR(255),
            email_to VARCHAR(255),
            scanned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            scanned_by VARCHAR(255),
            ip_address INET,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_scans_email_hash ON scans(email_hash);
        CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at DESC);
        CREATE INDEX IF NOT EXISTS idx_scans_verdict ON scans(verdict);

        -- Reports Table
        CREATE TABLE IF NOT EXISTS reports (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            scan_id UUID REFERENCES scans(id) ON DELETE SET NULL,
            email_hash VARCHAR(64) NOT NULL,
            reporter_email VARCHAR(255),
            reporter_name VARCHAR(255),
            verdict VARCHAR(20) NOT NULL,
            confidence DECIMAL(5,2) NOT NULL,
            teams_notified BOOLEAN DEFAULT FALSE,
            telegram_notified BOOLEAN DEFAULT FALSE,
            whatsapp_notified BOOLEAN DEFAULT FALSE,
            reported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_email);
        CREATE INDEX IF NOT EXISTS idx_reports_reported_at ON reports(reported_at DESC);

        -- API Keys Table
        CREATE TABLE IF NOT EXISTS api_keys (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            key_hash VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            can_scan BOOLEAN DEFAULT TRUE,
            can_report BOOLEAN DEFAULT TRUE,
            can_admin BOOLEAN DEFAULT FALSE,
            rate_limit_per_minute INTEGER DEFAULT 60,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP WITH TIME ZONE,
            expires_at TIMESTAMP WITH TIME ZONE
        );

        -- Audit Log Table
        CREATE TABLE IF NOT EXISTS audit_log (
            id BIGSERIAL PRIMARY KEY,
            event_type VARCHAR(50) NOT NULL,
            event_data JSONB,
            ip_address INET,
            user_agent TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
        CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC);
        """
        cursor.execute(init_sql)

    @property
    def is_available(self) -> bool:
        """Check if database is available."""
        return self._initialized and Database._pool is not None

    @contextmanager
    def get_connection(self):
        """
        Get a connection from the pool (context manager).
        Automatically returns connection to pool on exit.
        """
        if not self.is_available:
            raise DatabaseError("Database not initialized")

        conn = Database._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise DatabaseError(f"Database operation failed: {e}") from e
        finally:
            Database._pool.putconn(conn)

    def close(self):
        """Close all connections in the pool."""
        if Database._pool:
            Database._pool.closeall()
            Database._pool = None
            self._initialized = False
            logger.info("Database connection pool closed")

    # -------------------------------------------------------------------------
    # Scan Operations
    # -------------------------------------------------------------------------

    def save_scan(
        self,
        email_hash: str,
        verdict: str,
        confidence: float,
        ai_score: Optional[float] = None,
        sublime_score: Optional[float] = None,
        reasons: Optional[List[str]] = None,
        indicators: Optional[List[str]] = None,
        check_results: Optional[Dict[str, Any]] = None,
        email_subject: Optional[str] = None,
        email_from: Optional[str] = None,
        email_to: Optional[str] = None,
        scanned_by: Optional[str] = None,
        ip_address: Optional[str] = None
    ) -> Optional[str]:
        """
        Save a scan result to the database.

        Returns:
            The scan UUID if successful, None otherwise.
        """
        if not self.is_available:
            logger.debug("Database not available - skipping scan save")
            return None

        # Serialize check_results, handling dataclass objects
        serialized_results = {}
        if check_results:
            for name, result in check_results.items():
                if hasattr(result, '__dataclass_fields__'):
                    serialized_results[name] = asdict(result)
                elif isinstance(result, dict):
                    serialized_results[name] = result
                else:
                    serialized_results[name] = str(result)

        sql = """
            INSERT INTO scans (
                email_hash, verdict, confidence, ai_score, sublime_score,
                reasons, indicators, check_results,
                email_subject, email_from, email_to,
                scanned_by, ip_address
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            RETURNING id
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        email_hash,
                        verdict,
                        confidence,
                        ai_score,
                        sublime_score,
                        json.dumps(reasons or []),
                        json.dumps(indicators or []),
                        json.dumps(serialized_results),
                        (email_subject or "")[:500],
                        (email_from or "")[:255],
                        (email_to or "")[:255],
                        scanned_by,
                        ip_address
                    ))
                    result = cur.fetchone()
                    scan_id = str(result[0]) if result else None
                    logger.info(f"Scan saved: id={scan_id}, verdict={verdict}, confidence={confidence}")
                    return scan_id
        except Exception as e:
            logger.error(f"Failed to save scan: {e}")
            return None

    def get_scan_by_hash(self, email_hash: str) -> Optional[Dict[str, Any]]:
        """
        Get cached scan result by email hash.
        Returns the most recent scan for this email.
        """
        if not self.is_available:
            return None

        sql = """
            SELECT id, email_hash, verdict, confidence, ai_score, sublime_score,
                   reasons, indicators, check_results, scanned_at
            FROM scans
            WHERE email_hash = %s
            ORDER BY scanned_at DESC
            LIMIT 1
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (email_hash,))
                    result = cur.fetchone()
                    if result:
                        return dict(result)
                    return None
        except Exception as e:
            logger.error(f"Failed to get scan by hash: {e}")
            return None

    def get_scan_by_id(self, scan_id: str) -> Optional[Dict[str, Any]]:
        """Get scan by UUID."""
        if not self.is_available:
            return None

        sql = """
            SELECT id, email_hash, verdict, confidence, ai_score, sublime_score,
                   reasons, indicators, check_results,
                   email_subject, email_from, email_to,
                   scanned_at, scanned_by, ip_address
            FROM scans
            WHERE id = %s
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (scan_id,))
                    result = cur.fetchone()
                    if result:
                        return dict(result)
                    return None
        except Exception as e:
            logger.error(f"Failed to get scan by id: {e}")
            return None

    def get_recent_scans(
        self,
        limit: int = 50,
        offset: int = 0,
        verdict_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get recent scans with optional verdict filter."""
        if not self.is_available:
            return []

        sql = """
            SELECT id, email_hash, verdict, confidence, ai_score, sublime_score,
                   email_subject, email_from, scanned_at
            FROM scans
            {where_clause}
            ORDER BY scanned_at DESC
            LIMIT %s OFFSET %s
        """

        params = []
        where_clause = ""

        if verdict_filter and verdict_filter in ('SAFE', 'SUSPICIOUS', 'PHISHING'):
            where_clause = "WHERE verdict = %s"
            params.append(verdict_filter)

        params.extend([limit, offset])

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql.format(where_clause=where_clause), tuple(params))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get recent scans: {e}")
            return []

    def get_scan_stats(self, days: int = 30) -> Dict[str, Any]:
        """Get scan statistics for the last N days."""
        if not self.is_available:
            return {"available": False}

        sql = """
            SELECT
                COUNT(*) as total_scans,
                COUNT(*) FILTER (WHERE verdict = 'SAFE') as safe_count,
                COUNT(*) FILTER (WHERE verdict = 'SUSPICIOUS') as suspicious_count,
                COUNT(*) FILTER (WHERE verdict = 'PHISHING') as phishing_count,
                ROUND(AVG(confidence)::numeric, 1) as avg_confidence,
                COUNT(DISTINCT email_hash) as unique_emails
            FROM scans
            WHERE scanned_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (days,))
                    result = cur.fetchone()
                    if result:
                        stats = dict(result)
                        stats["available"] = True
                        stats["period_days"] = days
                        return stats
                    return {"available": False}
        except Exception as e:
            logger.error(f"Failed to get scan stats: {e}")
            return {"available": False}

    def get_daily_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get daily scan breakdown for charts."""
        if not self.is_available:
            return []

        sql = """
            SELECT
                DATE(scanned_at) as scan_date,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE verdict = 'SAFE') as safe,
                COUNT(*) FILTER (WHERE verdict = 'SUSPICIOUS') as suspicious,
                COUNT(*) FILTER (WHERE verdict = 'PHISHING') as phishing
            FROM scans
            WHERE scanned_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
            GROUP BY DATE(scanned_at)
            ORDER BY scan_date DESC
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (days,))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get daily stats: {e}")
            return []

    # -------------------------------------------------------------------------
    # Report Operations
    # -------------------------------------------------------------------------

    def save_report(
        self,
        email_hash: str,
        verdict: str,
        confidence: float,
        scan_id: Optional[str] = None,
        reporter_email: Optional[str] = None,
        reporter_name: Optional[str] = None,
        teams_notified: bool = False,
        telegram_notified: bool = False,
        whatsapp_notified: bool = False,
        notes: Optional[str] = None
    ) -> Optional[str]:
        """
        Save a phishing report.

        Returns:
            The report UUID if successful, None otherwise.
        """
        if not self.is_available:
            logger.debug("Database not available - skipping report save")
            return None

        sql = """
            INSERT INTO reports (
                email_hash, verdict, confidence, scan_id,
                reporter_email, reporter_name,
                teams_notified, telegram_notified, whatsapp_notified,
                notes
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s
            )
            RETURNING id
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        email_hash,
                        verdict,
                        confidence,
                        scan_id,
                        reporter_email,
                        reporter_name,
                        teams_notified,
                        telegram_notified,
                        whatsapp_notified,
                        notes
                    ))
                    result = cur.fetchone()
                    report_id = str(result[0]) if result else None
                    logger.info(f"Report saved: id={report_id}, verdict={verdict}")
                    return report_id
        except Exception as e:
            logger.error(f"Failed to save report: {e}")
            return None

    def get_recent_reports(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Get recent phishing reports."""
        if not self.is_available:
            return []

        sql = """
            SELECT r.id, r.email_hash, r.verdict, r.confidence,
                   r.reporter_email, r.reporter_name,
                   r.teams_notified, r.telegram_notified, r.whatsapp_notified,
                   r.reported_at, r.notes,
                   s.email_subject, s.email_from
            FROM reports r
            LEFT JOIN scans s ON r.scan_id = s.id
            ORDER BY r.reported_at DESC
            LIMIT %s OFFSET %s
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (limit, offset))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get recent reports: {e}")
            return []

    def get_report_stats(self, days: int = 30) -> Dict[str, Any]:
        """Get report statistics."""
        if not self.is_available:
            return {"available": False}

        sql = """
            SELECT
                COUNT(*) as total_reports,
                COUNT(*) FILTER (WHERE teams_notified) as teams_sent,
                COUNT(*) FILTER (WHERE telegram_notified) as telegram_sent,
                COUNT(*) FILTER (WHERE whatsapp_notified) as whatsapp_sent,
                COUNT(DISTINCT reporter_email) as unique_reporters
            FROM reports
            WHERE reported_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (days,))
                    result = cur.fetchone()
                    if result:
                        stats = dict(result)
                        stats["available"] = True
                        stats["period_days"] = days
                        return stats
                    return {"available": False}
        except Exception as e:
            logger.error(f"Failed to get report stats: {e}")
            return {"available": False}

    # -------------------------------------------------------------------------
    # Audit Log Operations
    # -------------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        event_data: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> bool:
        """Log an audit event."""
        if not self.is_available:
            return False

        sql = """
            INSERT INTO audit_log (event_type, event_data, ip_address, user_agent)
            VALUES (%s, %s, %s, %s)
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        event_type,
                        json.dumps(event_data or {}),
                        ip_address,
                        user_agent
                    ))
                    return True
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
            return False

    def get_audit_log(
        self,
        limit: int = 100,
        event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get audit log entries."""
        if not self.is_available:
            return []

        sql = """
            SELECT id, event_type, event_data, ip_address, user_agent, created_at
            FROM audit_log
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
        """

        params = []
        where_clause = ""

        if event_type:
            where_clause = "WHERE event_type = %s"
            params.append(event_type)

        params.append(limit)

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql.format(where_clause=where_clause), tuple(params))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get audit log: {e}")
            return []

    # -------------------------------------------------------------------------
    # Top Threats (for admin dashboard)
    # -------------------------------------------------------------------------

    def get_top_senders(self, days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
        """Get top phishing sender domains."""
        if not self.is_available:
            return []

        sql = """
            SELECT
                SUBSTRING(email_from FROM '@(.+)$') as domain,
                COUNT(*) as count,
                ROUND(AVG(confidence)::numeric, 1) as avg_confidence
            FROM scans
            WHERE verdict = 'PHISHING'
              AND scanned_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
              AND email_from IS NOT NULL
              AND email_from LIKE '%%@%%'
            GROUP BY SUBSTRING(email_from FROM '@(.+)$')
            ORDER BY count DESC
            LIMIT %s
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(sql, (days, limit))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get top senders: {e}")
            return []


# Singleton instance
_db_instance: Optional[Database] = None


def get_db() -> Database:
    """Get or create the database singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
        _db_instance.initialize()
    return _db_instance


def init_db(app=None) -> Database:
    """Initialize database for Flask app."""
    db = get_db()
    if app:
        # Register teardown
        @app.teardown_appcontext
        def close_db_connection(exception=None):
            pass  # Pool manages connections
    return db
