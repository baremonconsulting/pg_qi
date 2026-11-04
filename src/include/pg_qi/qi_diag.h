/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_DIAG_H
#define PG_QI_QI_DIAG_H

#include "postgres.h"

typedef enum QiFailpoint
{
    QI_FAILPOINT_OFF = 0,
    QI_FAILPOINT_BEFORE_PUSH,
    QI_FAILPOINT_INSIDE_TRY_BEFORE_POLICY,
    QI_FAILPOINT_INSIDE_TRY_AFTER_POLICY,
    QI_FAILPOINT_INSIDE_TRY_BEFORE_DOWNSTREAM,
    QI_FAILPOINT_INNER_PLANNER,
    QI_FAILPOINT_SUBTRANSACTION_BODY
} QiFailpoint;

extern void qi_diag_init_gucs(void);
extern void qi_failpoint_hit(QiFailpoint location);

#endif /* PG_QI_QI_DIAG_H */
