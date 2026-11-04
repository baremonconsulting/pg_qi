/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_REENTER_H
#define PG_QI_QI_REENTER_H

#include "postgres.h"

#include "nodes/params.h"
#include "nodes/parsenodes.h"

extern void qi_reenter_init_gucs(void);
extern bool qi_reenter_is_active(void);
extern bool qi_reenter_inner_error_enabled(void);
extern void qi_maybe_reenter(Query *parse, const char *query_string,
                             int cursor_options, ParamListInfo bound_params,
                             void *explain_state,
                             bool outer_delegation_requested);

#endif /* PG_QI_QI_REENTER_H */
