-- Your project's named queries.  Every *.sql file in this folder is loaded at
-- startup (folders are listed in project.json -> "sql_dirs").
--
-- Use from Python:      rows = ctx.db.run("example.failed_operations", session_id=3)
-- Use from the UI:      Database Explorer -> Saved queries
-- Override a built-in:  define a query with the same name (see
--                       app/core/database/sql/workbench.sql)

-- name: example.failed_operations
-- Operations that did not pass, newest first (all sessions).
SELECT executed_at, session_id, device_name, peer, operation_name, status, message
FROM operation_results
WHERE status <> 'passed' AND status <> 'sent'
ORDER BY executed_at DESC
LIMIT 500;

-- name: example.session_device_traffic
-- Bytes per device for one session.
SELECT CASE direction WHEN 'RX' THEN source ELSE destination END AS device,
       SUM(CASE direction WHEN 'RX' THEN length ELSE 0 END) AS rx_bytes,
       SUM(CASE direction WHEN 'TX' THEN length ELSE 0 END) AS tx_bytes,
       COUNT(*) AS frames
FROM packets
WHERE session_id = :session_id
GROUP BY device
ORDER BY frames DESC;

-- name: example.operation_pass_rate
-- Pass rate per operation across every session.
SELECT operation_name,
       COUNT(*) AS runs,
       SUM(status = 'passed') AS passed,
       ROUND(100.0 * SUM(status = 'passed') / COUNT(*), 1) AS pass_percent
FROM operation_results
WHERE status <> 'sent'
GROUP BY operation_name
ORDER BY pass_percent;
