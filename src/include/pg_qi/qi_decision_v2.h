/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_DECISION_V2_H
#define PG_QI_QI_DECISION_V2_H

#include "postgres.h"
#include "fmgr.h"
#include "pg_qi/qi_decision.h"

#define QI_DECISION_V2_CONTRACT_VERSION 2
#define QI_DECISION_V2_RULE_VERSION 1
#define QI_DECISION_V2_MODEL_VERSION 1
#define QI_DECISION_V2_CERTIFICATE_VERSION 1

typedef enum QiGateState
{
    QI_GATE_NOT_EVALUATED = 0,
    QI_GATE_PASS,
    QI_GATE_VETO,
    QI_GATE_UNKNOWN
} QiGateState;

typedef enum QiV2Recommendation
{
    QI_V2_WOULD_PASS_THROUGH = 0,
    QI_V2_WOULD_DELEGATE,
    QI_V2_INSUFFICIENT_EVIDENCE
} QiV2Recommendation;

typedef enum QiV2ActivationBarrier
{
    QI_V2_ACTIVATION_CLOSED = 0,
    QI_V2_ACTIVATION_OPEN
} QiV2ActivationBarrier;

/* Stable values: append only; never renumber an existing reason. */
typedef enum QiV2ReasonCode
{
    QI_V2_REASON_NOT_EVALUATED = 0,
    QI_V2_REASON_ELIGIBILITY_VETO = 100,
    QI_V2_REASON_SEMANTIC_VETO = 200,
    QI_V2_REASON_BENEFIT_VETO = 300,
    QI_V2_REASON_ELIGIBILITY_UNKNOWN = 400,
    QI_V2_REASON_SEMANTIC_UNKNOWN = 500,
    QI_V2_REASON_BENEFIT_UNKNOWN = 600,
    QI_V2_REASON_ACTIVATION_BARRIER_CLOSED = 700,
    QI_V2_REASON_VERSION_EVIDENCE_INVALID = 800,
    QI_V2_REASON_ALL_GATES_PASS = 900
} QiV2ReasonCode;

typedef struct QiV2Decision
{
    int contract_version;
    int rule_version;
    int model_version;
    int certificate_version;
    QiGateState eligible;
    QiGateState semantically_safe;
    QiGateState beneficial;
    QiV2ActivationBarrier activation_barrier;
    QiV2Recommendation recommendation;
    int reason_code;
    QiPolicyIntent policy_intent;
    QiForceOrigin force_origin;
    QiShadowEngine observed_engine;
} QiV2Decision;

extern QiV2ActivationBarrier qi_v2_default_activation_barrier(void);
extern QiV2Decision qi_v2_evaluate(QiGateState eligible,
                                   QiGateState semantically_safe,
                                   QiGateState beneficial,
                                   QiV2ActivationBarrier activation_barrier,
                                   int rule_version,
                                   int model_version,
                                   int certificate_version);
extern bool qi_v2_decision_valid(const QiV2Decision *decision);
extern bool qi_v2_reason_known(int reason_code);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_decision_v2_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_DECISION_V2_H */
