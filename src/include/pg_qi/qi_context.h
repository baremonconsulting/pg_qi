/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_CONTEXT_H
#define PG_QI_QI_CONTEXT_H

#include "postgres.h"

extern void qi_context_init_hooks(void);
extern void qi_context_fini_hooks(void);
extern bool qi_context_proven_for_routing(bool planner_hook_outermost,
                                         int planner_depth,
                                         const char *query_string);

#endif /* PG_QI_QI_CONTEXT_H */
