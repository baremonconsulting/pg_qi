/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_LIFECYCLE_H
#define PG_QI_QI_LIFECYCLE_H

#include "postgres.h"

#include "executor/executor.h"
#include "nodes/plannodes.h"
#include "pg_qi/qi_snapshot.h"

extern bool qi_lifecycle_arm(const PlannedStmt *planned_stmt,
                             const QiV2SnapshotCandidate *candidate);
extern void qi_lifecycle_init(void);
extern void qi_lifecycle_fini(void);
extern void qi_lifecycle_executor_start(QueryDesc *query_desc, bool outermost,
                                        bool context_proven);
extern void qi_lifecycle_executor_run(QueryDesc *query_desc);
extern void qi_lifecycle_executor_end(QueryDesc *query_desc);
extern void qi_lifecycle_reset(void);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_lifecycle_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_LIFECYCLE_H */
