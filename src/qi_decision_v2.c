/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "funcapi.h"
#include "pg_qi/qi_decision_v2.h"

static bool
qi_gate_state_valid(QiGateState state)
{
    return state >= QI_GATE_NOT_EVALUATED && state <= QI_GATE_UNKNOWN;
}

bool
qi_v2_reason_known(int reason_code)
{
    switch (reason_code)
    {
        case QI_V2_REASON_NOT_EVALUATED:
        case QI_V2_REASON_ELIGIBILITY_VETO:
        case QI_V2_REASON_SEMANTIC_VETO:
        case QI_V2_REASON_BENEFIT_VETO:
        case QI_V2_REASON_ELIGIBILITY_UNKNOWN:
        case QI_V2_REASON_SEMANTIC_UNKNOWN:
        case QI_V2_REASON_BENEFIT_UNKNOWN:
        case QI_V2_REASON_ACTIVATION_BARRIER_CLOSED:
        case QI_V2_REASON_VERSION_EVIDENCE_INVALID:
        case QI_V2_REASON_ALL_GATES_PASS:
            return true;
        default:
            return false;
    }
}

QiV2ActivationBarrier
qi_v2_default_activation_barrier(void)
{
    return QI_V2_ACTIVATION_CLOSED;
}

QiV2Decision
qi_v2_evaluate(QiGateState eligible, QiGateState semantically_safe,
               QiGateState beneficial,
               QiV2ActivationBarrier activation_barrier,
               int rule_version, int model_version, int certificate_version)
{
    QiV2Decision decision = {
        .contract_version = QI_DECISION_V2_CONTRACT_VERSION,
        .rule_version = rule_version,
        .model_version = model_version,
        .certificate_version = certificate_version,
        .eligible = eligible,
        .semantically_safe = semantically_safe,
        .beneficial = beneficial,
        .activation_barrier = activation_barrier,
        .recommendation = QI_V2_INSUFFICIENT_EVIDENCE,
        .reason_code = QI_V2_REASON_NOT_EVALUATED,
        .policy_intent = QI_POLICY_PASS_THROUGH,
        .force_origin = QI_FORCE_OFF,
        .observed_engine = QI_SHADOW_ENGINE_UNKNOWN
    };

    if (eligible == QI_GATE_VETO)
    {
        decision.recommendation = QI_V2_WOULD_PASS_THROUGH;
        decision.reason_code = QI_V2_REASON_ELIGIBILITY_VETO;
    }
    else if (semantically_safe == QI_GATE_VETO)
    {
        decision.recommendation = QI_V2_WOULD_PASS_THROUGH;
        decision.reason_code = QI_V2_REASON_SEMANTIC_VETO;
    }
    else if (beneficial == QI_GATE_VETO)
    {
        decision.recommendation = QI_V2_WOULD_PASS_THROUGH;
        decision.reason_code = QI_V2_REASON_BENEFIT_VETO;
    }
    else if (eligible == QI_GATE_UNKNOWN)
        decision.reason_code = QI_V2_REASON_ELIGIBILITY_UNKNOWN;
    else if (semantically_safe == QI_GATE_UNKNOWN)
        decision.reason_code = QI_V2_REASON_SEMANTIC_UNKNOWN;
    else if (beneficial == QI_GATE_UNKNOWN)
        decision.reason_code = QI_V2_REASON_BENEFIT_UNKNOWN;
    else if (eligible != QI_GATE_PASS ||
             semantically_safe != QI_GATE_PASS ||
             beneficial != QI_GATE_PASS)
        decision.reason_code = QI_V2_REASON_NOT_EVALUATED;
    else if (rule_version != QI_DECISION_V2_RULE_VERSION ||
             model_version != QI_DECISION_V2_MODEL_VERSION ||
             certificate_version != QI_DECISION_V2_CERTIFICATE_VERSION)
        decision.reason_code = QI_V2_REASON_VERSION_EVIDENCE_INVALID;
    else if (activation_barrier != QI_V2_ACTIVATION_OPEN)
        decision.reason_code = QI_V2_REASON_ACTIVATION_BARRIER_CLOSED;
    else
    {
        decision.recommendation = QI_V2_WOULD_DELEGATE;
        decision.reason_code = QI_V2_REASON_ALL_GATES_PASS;
    }

    return decision;
}

bool
qi_v2_decision_valid(const QiV2Decision *decision)
{
    QiV2Decision expected;
    bool any_veto;
    bool all_pass;
    bool versions_present;

    if (decision == NULL ||
        decision->contract_version != QI_DECISION_V2_CONTRACT_VERSION ||
        !qi_gate_state_valid(decision->eligible) ||
        !qi_gate_state_valid(decision->semantically_safe) ||
        !qi_gate_state_valid(decision->beneficial) ||
        decision->activation_barrier < QI_V2_ACTIVATION_CLOSED ||
        decision->activation_barrier > QI_V2_ACTIVATION_OPEN ||
        decision->recommendation < QI_V2_WOULD_PASS_THROUGH ||
        decision->recommendation > QI_V2_INSUFFICIENT_EVIDENCE ||
        decision->policy_intent < QI_POLICY_PASS_THROUGH ||
        decision->policy_intent > QI_POLICY_REQUEST_DELEGATION ||
        decision->force_origin < QI_FORCE_OFF ||
        decision->force_origin > QI_FORCE_QI_STACK ||
        decision->observed_engine < QI_SHADOW_ENGINE_UNKNOWN ||
        decision->observed_engine > QI_SHADOW_ENGINE_DUCKDB)
        return false;

    any_veto = decision->eligible == QI_GATE_VETO ||
        decision->semantically_safe == QI_GATE_VETO ||
        decision->beneficial == QI_GATE_VETO;
    all_pass = decision->eligible == QI_GATE_PASS &&
        decision->semantically_safe == QI_GATE_PASS &&
        decision->beneficial == QI_GATE_PASS;
    versions_present =
        decision->rule_version == QI_DECISION_V2_RULE_VERSION &&
        decision->model_version == QI_DECISION_V2_MODEL_VERSION &&
        decision->certificate_version ==
            QI_DECISION_V2_CERTIFICATE_VERSION;

    if (!qi_v2_reason_known(decision->reason_code))
        return decision->recommendation == QI_V2_INSUFFICIENT_EVIDENCE &&
            !any_veto &&
            !(all_pass && versions_present &&
              decision->activation_barrier == QI_V2_ACTIVATION_OPEN);

    expected = qi_v2_evaluate(decision->eligible,
                              decision->semantically_safe,
                              decision->beneficial,
                              decision->activation_barrier,
                              decision->rule_version,
                              decision->model_version,
                              decision->certificate_version);
    return decision->recommendation == expected.recommendation &&
        decision->reason_code == expected.reason_code;
}

#if PG_QI_TESTING
Datum
qi_test_decision_v2_contract(PG_FUNCTION_ARGS)
{
    typedef struct QiV2TruthCase
    {
        QiGateState eligible;
        QiGateState semantically_safe;
        QiGateState beneficial;
        QiV2ActivationBarrier barrier;
        int rule_version;
        int model_version;
        int certificate_version;
        QiV2Recommendation recommendation;
        int reason_code;
    } QiV2TruthCase;
    static const QiV2TruthCase truth_cases[] = {
        {QI_GATE_VETO, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH,
         QI_V2_REASON_ELIGIBILITY_VETO},
        {QI_GATE_UNKNOWN, QI_GATE_VETO, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH, QI_V2_REASON_SEMANTIC_VETO},
        {QI_GATE_UNKNOWN, QI_GATE_PASS, QI_GATE_VETO, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH, QI_V2_REASON_BENEFIT_VETO},
        {QI_GATE_PASS, QI_GATE_VETO, QI_GATE_VETO, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH, QI_V2_REASON_SEMANTIC_VETO},
        {QI_GATE_VETO, QI_GATE_VETO, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH,
         QI_V2_REASON_ELIGIBILITY_VETO},
        {QI_GATE_VETO, QI_GATE_PASS, QI_GATE_VETO, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_PASS_THROUGH,
         QI_V2_REASON_ELIGIBILITY_VETO},
        {QI_GATE_UNKNOWN, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_ELIGIBILITY_UNKNOWN},
        {QI_GATE_UNKNOWN, QI_GATE_UNKNOWN, QI_GATE_PASS,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_ELIGIBILITY_UNKNOWN},
        {QI_GATE_NOT_EVALUATED, QI_GATE_UNKNOWN, QI_GATE_PASS,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_SEMANTIC_UNKNOWN},
        {QI_GATE_PASS, QI_GATE_UNKNOWN, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_SEMANTIC_UNKNOWN},
        {QI_GATE_PASS, QI_GATE_UNKNOWN, QI_GATE_UNKNOWN,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_SEMANTIC_UNKNOWN},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_UNKNOWN, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_BENEFIT_UNKNOWN},
        {QI_GATE_NOT_EVALUATED, QI_GATE_PASS, QI_GATE_PASS,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_NOT_EVALUATED},
        {QI_GATE_PASS, QI_GATE_NOT_EVALUATED, QI_GATE_PASS,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE, QI_V2_REASON_NOT_EVALUATED},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_NOT_EVALUATED,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE, QI_V2_REASON_NOT_EVALUATED},
        {QI_GATE_UNKNOWN, QI_GATE_NOT_EVALUATED, QI_GATE_PASS,
         QI_V2_ACTIVATION_OPEN, 1, 1, 1,
         QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_ELIGIBILITY_UNKNOWN},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_CLOSED,
         1, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_ACTIVATION_BARRIER_CLOSED},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_CLOSED,
         1, 0, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_CLOSED,
         0, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         0, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 0, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 0, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         2, 1, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 2, 1, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 2, QI_V2_INSUFFICIENT_EVIDENCE,
         QI_V2_REASON_VERSION_EVIDENCE_INVALID},
        {QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS, QI_V2_ACTIVATION_OPEN,
         1, 1, 1, QI_V2_WOULD_DELEGATE, QI_V2_REASON_ALL_GATES_PASS}
    };
    QiV2Decision closed = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS,
        qi_v2_default_activation_barrier(), 1, 1, 1);
    QiV2Decision open = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS,
        QI_V2_ACTIVATION_OPEN, 1, 1, 1);
    QiV2Decision veto = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_VETO, QI_GATE_PASS,
        QI_V2_ACTIVATION_OPEN, 1, 1, 1);
    QiV2Decision unknown = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_UNKNOWN, QI_GATE_PASS,
        QI_V2_ACTIVATION_OPEN, 1, 1, 1);
    QiV2Decision missing_version = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS,
        QI_V2_ACTIVATION_OPEN, 1, 0, 1);
    QiV2Decision unknown_version = qi_v2_evaluate(
        QI_GATE_PASS, QI_GATE_PASS, QI_GATE_PASS,
        QI_V2_ACTIVATION_OPEN, 1, 2, 1);
    QiV2Decision unknown_reason = unknown;
    QiV2Decision unknown_reason_closed = closed;
    QiV2Decision unknown_reason_missing_version = missing_version;
    QiV2Decision invalid_positive = open;
    QiV2Decision invalid_pass_through = veto;
    QiV2Decision closed_as_delegate = closed;
    QiV2Decision veto_as_insufficient = veto;
    QiV2Decision mismatched_reason = unknown;
    QiV2Decision wrong_contract = unknown;
    QiV2Decision invalid_gate = unknown;
    QiV2Decision invalid_barrier = unknown;
    QiV2Decision invalid_policy = unknown;
    QiV2Decision invalid_force = unknown;
    QiV2Decision invalid_engine = unknown;
    QiV2Decision unknown_reason_with_veto = veto;
    QiV2Decision unknown_reason_with_delegate = open;
    QiV2Decision unknown_reason_on_delegate_fields = open;
    QiV2Decision unknown_reason_with_pass_through = unknown;
    QiV2Decision independent_axes = veto;
    TupleDesc tuple_desc;
    Datum values[9];
    bool nulls[9] = {false, false, false, false, false, false, false, false,
                     false};
    bool exhaustive = true;
    bool frozen_truth_table = true;
    Size truth_index;
    int eligible;
    int semantically_safe;
    int beneficial;
    int barrier;
    int rule_version_case;
    int model_version_case;
    int certificate_version_case;

    unknown_reason.reason_code = 123456;
    unknown_reason_closed.reason_code = 123456;
    unknown_reason_missing_version.reason_code = 123456;
    invalid_positive.semantically_safe = QI_GATE_UNKNOWN;
    invalid_pass_through.semantically_safe = QI_GATE_PASS;
    closed_as_delegate.recommendation = QI_V2_WOULD_DELEGATE;
    veto_as_insufficient.recommendation = QI_V2_INSUFFICIENT_EVIDENCE;
    mismatched_reason.reason_code = QI_V2_REASON_BENEFIT_UNKNOWN;
    wrong_contract.contract_version = 1;
    invalid_gate.eligible = (QiGateState) 99;
    invalid_barrier.activation_barrier = (QiV2ActivationBarrier) 99;
    invalid_policy.policy_intent = (QiPolicyIntent) 99;
    invalid_force.force_origin = (QiForceOrigin) 99;
    invalid_engine.observed_engine = (QiShadowEngine) 99;
    unknown_reason_with_veto.reason_code = 123456;
    unknown_reason_with_veto.recommendation =
        QI_V2_INSUFFICIENT_EVIDENCE;
    unknown_reason_with_delegate.reason_code = 123456;
    unknown_reason_on_delegate_fields.reason_code = 123456;
    unknown_reason_on_delegate_fields.recommendation =
        QI_V2_INSUFFICIENT_EVIDENCE;
    unknown_reason_with_pass_through.reason_code = 123456;
    unknown_reason_with_pass_through.recommendation =
        QI_V2_WOULD_PASS_THROUGH;
    independent_axes.policy_intent = QI_POLICY_REQUEST_DELEGATION;
    independent_axes.force_origin = QI_FORCE_EXTERNAL;
    independent_axes.observed_engine = QI_SHADOW_ENGINE_DUCKDB;

    for (eligible = QI_GATE_NOT_EVALUATED;
         eligible <= QI_GATE_UNKNOWN; eligible++)
        for (semantically_safe = QI_GATE_NOT_EVALUATED;
             semantically_safe <= QI_GATE_UNKNOWN; semantically_safe++)
            for (beneficial = QI_GATE_NOT_EVALUATED;
                 beneficial <= QI_GATE_UNKNOWN; beneficial++)
                for (barrier = QI_V2_ACTIVATION_CLOSED;
                     barrier <= QI_V2_ACTIVATION_OPEN; barrier++)
                    for (rule_version_case = 0; rule_version_case <= 2;
                         rule_version_case++)
                        for (model_version_case = 0; model_version_case <= 2;
                             model_version_case++)
                            for (certificate_version_case = 0;
                                 certificate_version_case <= 2;
                                 certificate_version_case++)
                            {
                                QiV2Recommendation expected_recommendation;
                                int expected_reason;
                                QiV2Decision candidate;

                                if (eligible == QI_GATE_VETO)
                                {
                                    expected_recommendation =
                                        QI_V2_WOULD_PASS_THROUGH;
                                    expected_reason =
                                        QI_V2_REASON_ELIGIBILITY_VETO;
                                }
                                else if (semantically_safe == QI_GATE_VETO)
                                {
                                    expected_recommendation =
                                        QI_V2_WOULD_PASS_THROUGH;
                                    expected_reason =
                                        QI_V2_REASON_SEMANTIC_VETO;
                                }
                                else if (beneficial == QI_GATE_VETO)
                                {
                                    expected_recommendation =
                                        QI_V2_WOULD_PASS_THROUGH;
                                    expected_reason =
                                        QI_V2_REASON_BENEFIT_VETO;
                                }
                                else if (eligible == QI_GATE_UNKNOWN)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_ELIGIBILITY_UNKNOWN;
                                }
                                else if (semantically_safe == QI_GATE_UNKNOWN)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_SEMANTIC_UNKNOWN;
                                }
                                else if (beneficial == QI_GATE_UNKNOWN)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_BENEFIT_UNKNOWN;
                                }
                                else if (eligible != QI_GATE_PASS ||
                                         semantically_safe != QI_GATE_PASS ||
                                         beneficial != QI_GATE_PASS)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_NOT_EVALUATED;
                                }
                                else if (rule_version_case != 1 ||
                                         model_version_case != 1 ||
                                         certificate_version_case != 1)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_VERSION_EVIDENCE_INVALID;
                                }
                                else if (barrier == QI_V2_ACTIVATION_CLOSED)
                                {
                                    expected_recommendation =
                                        QI_V2_INSUFFICIENT_EVIDENCE;
                                    expected_reason =
                                        QI_V2_REASON_ACTIVATION_BARRIER_CLOSED;
                                }
                                else
                                {
                                    expected_recommendation =
                                        QI_V2_WOULD_DELEGATE;
                                    expected_reason =
                                        QI_V2_REASON_ALL_GATES_PASS;
                                }

                                candidate = qi_v2_evaluate(
                                    (QiGateState) eligible,
                                    (QiGateState) semantically_safe,
                                    (QiGateState) beneficial,
                                    (QiV2ActivationBarrier) barrier,
                                    rule_version_case, model_version_case,
                                    certificate_version_case);

                                if (!qi_v2_decision_valid(&candidate) ||
                                    candidate.recommendation !=
                                        expected_recommendation ||
                                    candidate.reason_code != expected_reason)
                                    exhaustive = false;
                            }

    for (truth_index = 0;
         truth_index < lengthof(truth_cases); truth_index++)
    {
        const QiV2TruthCase *truth = &truth_cases[truth_index];
        QiV2Decision candidate = qi_v2_evaluate(
            truth->eligible, truth->semantically_safe, truth->beneficial,
            truth->barrier, truth->rule_version, truth->model_version,
            truth->certificate_version);

        if (candidate.recommendation != truth->recommendation ||
            candidate.reason_code != truth->reason_code ||
            !qi_v2_decision_valid(&candidate))
            frozen_truth_table = false;
    }

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));

    values[0] = BoolGetDatum(qi_v2_default_activation_barrier() ==
                             QI_V2_ACTIVATION_CLOSED);
    values[1] = BoolGetDatum(qi_v2_decision_valid(&closed) &&
                             closed.recommendation ==
                                 QI_V2_INSUFFICIENT_EVIDENCE);
    values[2] = BoolGetDatum(qi_v2_decision_valid(&open) &&
                             open.recommendation == QI_V2_WOULD_DELEGATE &&
                             open.policy_intent == QI_POLICY_PASS_THROUGH &&
                             open.force_origin == QI_FORCE_OFF &&
                             open.observed_engine ==
                                 QI_SHADOW_ENGINE_UNKNOWN);
    values[3] = BoolGetDatum(qi_v2_decision_valid(&veto) &&
                             veto.recommendation ==
                                 QI_V2_WOULD_PASS_THROUGH &&
                             qi_v2_decision_valid(&independent_axes));
    values[4] = BoolGetDatum(qi_v2_decision_valid(&unknown) &&
                             unknown.recommendation ==
                                 QI_V2_INSUFFICIENT_EVIDENCE);
    values[5] = BoolGetDatum(qi_v2_decision_valid(&unknown_reason) &&
                             unknown_reason.recommendation ==
                                 QI_V2_INSUFFICIENT_EVIDENCE &&
                             qi_v2_decision_valid(&unknown_reason_closed) &&
                             qi_v2_decision_valid(
                                 &unknown_reason_missing_version) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_NOT_EVALUATED) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_ELIGIBILITY_VETO) &&
                             qi_v2_reason_known(QI_V2_REASON_SEMANTIC_VETO) &&
                             qi_v2_reason_known(QI_V2_REASON_BENEFIT_VETO) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_ELIGIBILITY_UNKNOWN) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_SEMANTIC_UNKNOWN) &&
                             qi_v2_reason_known(QI_V2_REASON_BENEFIT_UNKNOWN) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_ACTIVATION_BARRIER_CLOSED) &&
                             qi_v2_reason_known(
                                 QI_V2_REASON_VERSION_EVIDENCE_INVALID) &&
                             qi_v2_reason_known(QI_V2_REASON_ALL_GATES_PASS) &&
                             !qi_v2_reason_known(-1) &&
                             !qi_v2_reason_known(1) &&
                             !qi_v2_reason_known(123456));
    values[6] = BoolGetDatum(!qi_v2_decision_valid(&invalid_positive) &&
                             !qi_v2_decision_valid(&invalid_pass_through) &&
                             !qi_v2_decision_valid(&closed_as_delegate) &&
                             !qi_v2_decision_valid(&veto_as_insufficient) &&
                             !qi_v2_decision_valid(&mismatched_reason) &&
                             !qi_v2_decision_valid(NULL) &&
                             !qi_v2_decision_valid(&wrong_contract) &&
                             !qi_v2_decision_valid(&invalid_gate) &&
                             !qi_v2_decision_valid(&invalid_barrier) &&
                             !qi_v2_decision_valid(&invalid_policy) &&
                             !qi_v2_decision_valid(&invalid_force) &&
                             !qi_v2_decision_valid(&invalid_engine) &&
                             !qi_v2_decision_valid(
                                 &unknown_reason_with_veto) &&
                             !qi_v2_decision_valid(
                                 &unknown_reason_with_delegate) &&
                             !qi_v2_decision_valid(
                                 &unknown_reason_on_delegate_fields) &&
                             !qi_v2_decision_valid(
                                 &unknown_reason_with_pass_through));
    values[7] = BoolGetDatum(qi_v2_decision_valid(&missing_version) &&
                             missing_version.recommendation ==
                                 QI_V2_INSUFFICIENT_EVIDENCE &&
                             qi_v2_decision_valid(&unknown_version) &&
                             unknown_version.recommendation ==
                                 QI_V2_INSUFFICIENT_EVIDENCE);
    values[8] = BoolGetDatum(exhaustive && frozen_truth_table);

    PG_RETURN_DATUM(HeapTupleGetDatum(
        heap_form_tuple(tuple_desc, values, nulls)));
}
#endif
