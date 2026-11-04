/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_DETECTOR_H
#define PG_QI_QI_DETECTOR_H

#include "postgres.h"
#include "fmgr.h"

#include "nodes/plannodes.h"

#define QI_DUCKDB_SCAN_PROVIDER "DuckDBScan"

extern bool qi_plan_has_duckdb_scan(const PlannedStmt *planned_stmt);
extern PGDLLEXPORT Datum qi_test_detector_contract(PG_FUNCTION_ARGS);

#endif /* PG_QI_QI_DETECTOR_H */
