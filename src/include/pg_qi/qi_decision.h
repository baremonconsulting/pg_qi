/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_DECISION_H
#define PG_QI_QI_DECISION_H

#include "postgres.h"
#include "pg_qi/qi_classify.h"
#include "pg_qi/qi_delegation.h"
#include "pg_qi/qi_eligibility.h"

typedef enum QiShadowContext
{
    QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST = 0,
    QI_SHADOW_CONTEXT_NESTED
} QiShadowContext;

typedef enum QiShadowClassifierStatus
{
    QI_SHADOW_CLASSIFIER_NOT_EVALUATED = 0,
    QI_SHADOW_CLASSIFIER_CANDIDATE,
    QI_SHADOW_CLASSIFIER_REJECTED
} QiShadowClassifierStatus;

typedef enum QiShadowReason
{
    QI_SHADOW_NO_COMPLETED_PLAN = 0,
    QI_SHADOW_CONTROL_DISABLED,
    QI_SHADOW_CONTEXT_NESTED_REASON,
    QI_SHADOW_FORCE_EXTERNAL,
    QI_SHADOW_CLASSIFIER_REJECTED_REASON,
    QI_SHADOW_POLICY_MISSING
} QiShadowReason;

typedef enum QiShadowEngine
{
    QI_SHADOW_ENGINE_UNKNOWN = 0,
    QI_SHADOW_ENGINE_DUCKDB
} QiShadowEngine;

typedef enum QiShadowObservation
{
    QI_SHADOW_OBSERVATION_NONE = 0,
    QI_SHADOW_OBSERVATION_NO_AUTHORITATIVE_MATCH,
    QI_SHADOW_OBSERVATION_DUCKDB_SCAN_AT_DOWNSTREAM_BOUNDARY
} QiShadowObservation;

extern void qi_shadow_init_gucs(void);
extern void qi_shadow_publish(QiShadowContext context,
                              QiForceOrigin force_origin,
                              QiShadowClassifierStatus classifier_status,
                              QiEligibilityResult eligibility,
                              QiShadowReason reason,
                              QiShadowEngine observed_engine,
                              QiShadowObservation observation);

#endif /* PG_QI_QI_DECISION_H */
