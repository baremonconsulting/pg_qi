/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_EXPLAIN_GUARD_H
#define PG_QI_QI_EXPLAIN_GUARD_H

#include "postgres.h"

#include "executor/execdesc.h"

extern void qi_explain_guard_executor_started(QueryDesc *query_desc);

#endif /* PG_QI_QI_EXPLAIN_GUARD_H */
