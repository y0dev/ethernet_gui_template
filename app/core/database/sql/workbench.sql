-- Built-in named queries used by the application.
--
-- Call them with db.run("<name>", param=value), db.run_one(...), db.run_scalar(...)
-- or db.run_write(...).  To change one for your project, copy it into a file
-- in sql/ (project root) with the same name: later files override this one.

-- name: sessions.list_with_counts
-- All recorded sessions, newest first, with their packet count.
SELECT s.*, (SELECT COUNT(*) FROM packets p WHERE p.session_id = s.id) AS packet_count
FROM sessions s
ORDER BY s.started_at DESC;

-- name: logs.prune
-- Keep only the newest :keep rows of application_logs.
DELETE FROM application_logs
WHERE id <= (SELECT COALESCE(MAX(id), 0) - :keep FROM application_logs);

-- name: logs.recent
-- Newest :limit log records, returned oldest first.
SELECT * FROM (SELECT * FROM application_logs ORDER BY id DESC LIMIT :limit) ORDER BY id;

-- name: stats.database_summary
-- Row counts shown in the Session Workflow database step.
SELECT
    (SELECT COUNT(*) FROM sessions)          AS sessions,
    (SELECT COUNT(*) FROM device_operations) AS operations,
    (SELECT COUNT(*) FROM devices)           AS devices;

-- name: porting.status_counts
-- Number of porting modules per status (dashboard).
SELECT status, COUNT(*) AS n FROM porting_modules GROUP BY status;

-- name: report.traffic
-- Frames and bytes per direction for one session.
SELECT direction, COUNT(*) AS frames, SUM(length) AS bytes
FROM packets
WHERE session_id = :session_id
GROUP BY direction;

-- name: report.traffic_per_peer
-- Frames and bytes per device address and direction for one session.
SELECT CASE direction WHEN 'RX' THEN source ELSE destination END AS peer,
       direction, COUNT(*) AS frames, SUM(length) AS bytes
FROM packets
WHERE session_id = :session_id
GROUP BY peer, direction;

-- name: report.warnings
-- Warnings and errors logged between :start and :end (epoch seconds).
SELECT timestamp, level, logger, message, context_json
FROM application_logs
WHERE timestamp >= :start AND timestamp <= :end
  AND level IN ('WARNING', 'ERROR', 'CRITICAL')
ORDER BY timestamp
LIMIT 200;

-- name: report.operation_results
-- Every operation result of one session, in execution order.
SELECT * FROM operation_results WHERE session_id = :session_id ORDER BY executed_at, id;

-- name: schema.tables
-- User tables in the database.
SELECT name FROM sqlite_master
WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
ORDER BY name;
