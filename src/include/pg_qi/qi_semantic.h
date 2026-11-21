/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_SEMANTIC_H
#define PG_QI_QI_SEMANTIC_H

#include "postgres.h"

#include "executor/execdesc.h"
#include "nodes/parsenodes.h"
#include "pg_qi/qi_decision_v2.h"
#include "pg_qi/qi_shadow_p0.h"

typedef enum QiSemanticStatus
{
    QI_SEMANTIC_NOT_EVALUATED = 0,
    QI_SEMANTIC_UNKNOWN,
    QI_SEMANTIC_CERTIFIED
} QiSemanticStatus;

typedef enum QiSemanticProfileId
{
    QI_SEMANTIC_PROFILE_NONE = 0,
    QI_SEMANTIC_PROFILE_COUNT_STAR,
    QI_SEMANTIC_PROFILE_COUNT_INT4,
    QI_SEMANTIC_PROFILE_MIN_INT4,
    QI_SEMANTIC_PROFILE_MAX_INT4
} QiSemanticProfileId;

typedef enum QiSemanticBlocker
{
    QI_SEMANTIC_BLOCKER_INACTIVE = (1U << 0),
    QI_SEMANTIC_BLOCKER_BUILD_IDENTITY_UNVERIFIED = (1U << 1),
    QI_SEMANTIC_BLOCKER_SNAPSHOT_UNPROVEN = (1U << 2),
    QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH = (1U << 3),
    QI_SEMANTIC_BLOCKER_PERMISSION_METADATA = (1U << 4),
    QI_SEMANTIC_BLOCKER_CHECK_AS_USER = (1U << 5),
    QI_SEMANTIC_BLOCKER_ROLE_CONTEXT = (1U << 6),
    QI_SEMANTIC_BLOCKER_ACL = (1U << 7),
    QI_SEMANTIC_BLOCKER_SECURITY_QUAL = (1U << 8),
    QI_SEMANTIC_BLOCKER_RLS = (1U << 9),
    QI_SEMANTIC_BLOCKER_SERIALIZABLE = (1U << 10),
    QI_SEMANTIC_BLOCKER_OWN_XID = (1U << 11)
} QiSemanticBlocker;

typedef struct QiSemanticFacts
{
    QiSemanticStatus status;
    QiSemanticProfileId profile;
    uint32 blockers;
    bool count_scope;
} QiSemanticFacts;

extern QiSemanticFacts qi_semantic_observe_query(
    const Query *query, const QiShadowP0Facts *p0_facts);
extern QiGateState qi_semantic_gate_state(const QiSemanticFacts *facts);
extern bool qi_semantic_confirm_count_execution(QiSemanticFacts *facts,
                                                const QueryDesc *query_desc,
                                                Oid expected_relation);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_semantic_contract(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_semantic_probe(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_SEMANTIC_H */
