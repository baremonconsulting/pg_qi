/*
 * Copyright 2026 Baremon s.r.o.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_PLANNER_COMPAT_H
#define PG_QI_PLANNER_COMPAT_H

#include "postgres.h"

#include "optimizer/planner.h"

/*
 * PostgreSQL 16-18 expose the four-argument planner hook.  PostgreSQL 19
 * development sources add ExplainState as a fifth argument.  Keep this
 * version split in one place and validate it against real server headers.
 *
 * Do not add a compatibility typedef for ExplainState.  PostgreSQL owns the
 * public type, and the released four-argument branch does not need it.
 */
#if PG_VERSION_NUM >= 190000

#define QI_HAVE_PLANNER_ES 1

typedef PlannedStmt *(*qi_planner_fn_type) (Query *parse,
                                             const char *query_string,
                                             int cursor_options,
                                             ParamListInfo bound_params,
                                             ExplainState *es);

#define QI_PLANNER_PROTO(fn) \
    static PlannedStmt *fn(Query *parse, const char *query_string, \
                           int cursor_options, ParamListInfo bound_params, \
                           ExplainState *es)

#define QI_CALL_PLANNER(fn, parse, query_string, cursor_options, \
                        bound_params, es) \
    (fn)((parse), (query_string), (cursor_options), (bound_params), (es))

#define QI_CURRENT_EXPLAIN_STATE es

#else

#define QI_HAVE_PLANNER_ES 0

typedef PlannedStmt *(*qi_planner_fn_type) (Query *parse,
                                             const char *query_string,
                                             int cursor_options,
                                             ParamListInfo bound_params);

#define QI_PLANNER_PROTO(fn) \
    static PlannedStmt *fn(Query *parse, const char *query_string, \
                           int cursor_options, ParamListInfo bound_params)

#define QI_CALL_PLANNER(fn, parse, query_string, cursor_options, \
                        bound_params, es_ignored) \
    (fn)((parse), (query_string), (cursor_options), (bound_params))

#define QI_CURRENT_EXPLAIN_STATE NULL

#endif

#endif /* PG_QI_PLANNER_COMPAT_H */
