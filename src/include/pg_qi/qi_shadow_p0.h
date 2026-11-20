/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_SHADOW_P0_H
#define PG_QI_QI_SHADOW_P0_H

#include "postgres.h"
#include "nodes/parsenodes.h"

#define QI_SHADOW_P0_MAX_RTES 16
#define QI_SHADOW_P0_MAX_NODES 256
#define QI_SHADOW_P0_MAX_DEPTH 32
#define QI_SHADOW_P0_MAX_FACTS 16

typedef enum QiShadowP0FactsState
{
    QI_SHADOW_P0_FACTS_UNKNOWN = 0,
    QI_SHADOW_P0_FACTS_BOUNDED
} QiShadowP0FactsState;

typedef enum QiShadowP0FactsReason
{
    QI_SHADOW_P0_FACTS_OK = 0,
    QI_SHADOW_P0_FACTS_NO_QUERY,
    QI_SHADOW_P0_FACTS_LIMIT,
    QI_SHADOW_P0_FACTS_CATALOG_MISS,
    QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE,
    QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE,
    QI_SHADOW_P0_FACTS_UNSUPPORTED_RELATION,
    QI_SHADOW_P0_FACTS_CATALOG_MISMATCH
} QiShadowP0FactsReason;

typedef enum QiShadowP0Capability
{
    QI_SHADOW_P0_CAPABILITY_UNKNOWN = 0,
    QI_SHADOW_P0_CAPABILITY_COUNT_STAR,
    QI_SHADOW_P0_CAPABILITY_COUNT_INT4,
    QI_SHADOW_P0_CAPABILITY_MIN_INT4,
    QI_SHADOW_P0_CAPABILITY_MAX_INT4
} QiShadowP0Capability;

typedef struct QiShadowP0Facts
{
    QiShadowP0FactsState state;
    QiShadowP0FactsReason reason;
    QiShadowP0Capability capability;
    Oid                 relation_oid;
    AttrNumber          argument_attno;
    uint16              rtable_entries;
    uint16              relation_entries;
    uint16              nodes_seen;
    uint16              max_depth_seen;
    uint16              facts_emitted;
} QiShadowP0Facts;

extern QiShadowP0Facts qi_shadow_p0_observe_query(const Query *query);
extern void qi_shadow_p0_publish_facts(QiShadowP0Facts facts);
extern void qi_shadow_p0_reset_facts(void);
extern void qi_shadow_p0_init_gucs(void);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_shadow_p0_observer_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_SHADOW_P0_H */
