CREATE EXTENSION pg_qi;

SELECT qi.version();
SELECT qi.is_preloaded() IS NOT NULL AS reports_preload_state;
SHOW pg_qi.enabled;

DROP EXTENSION pg_qi;
