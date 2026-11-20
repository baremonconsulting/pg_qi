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
    QI_SHADOW_P0_FACTS_CATALOG_MISS
} QiShadowP0FactsReason;

typedef struct QiShadowP0Facts
{
    QiShadowP0FactsState state;
    QiShadowP0FactsReason reason;
    uint16              rtable_entries;
    uint16              relation_entries;
} QiShadowP0Facts;

extern QiShadowP0Facts qi_shadow_p0_observe_query(const Query *query);
extern void qi_shadow_p0_publish_facts(QiShadowP0Facts facts);
extern void qi_shadow_p0_reset_facts(void);
extern void qi_shadow_p0_init_gucs(void);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_shadow_p0_observer_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_SHADOW_P0_H */
