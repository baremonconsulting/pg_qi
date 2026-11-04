\set seq :seq + 1
\set delegated :seq % 2
\if :delegated = 1
SELECT qi.spike_arm_next();
\endif
SELECT count(*) FROM wp10_probe WHERE v = (:seq % 10);
