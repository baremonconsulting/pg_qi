/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "catalog/pg_class.h"
#include "fmgr.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_shadow_p0.h"
#include "utils/guc.h"
#include "utils/syscache.h"

static int qi_shadow_p0_facts_state = QI_SHADOW_P0_FACTS_UNKNOWN;
static int qi_shadow_p0_facts_reason = QI_SHADOW_P0_FACTS_NO_QUERY;

static const struct config_enum_entry qi_shadow_p0_state_options[] = {
    {"unknown", QI_SHADOW_P0_FACTS_UNKNOWN, false},
    {"bounded", QI_SHADOW_P0_FACTS_BOUNDED, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_shadow_p0_reason_options[] = {
    {"ok", QI_SHADOW_P0_FACTS_OK, false},
    {"no_query", QI_SHADOW_P0_FACTS_NO_QUERY, false},
    {"limit", QI_SHADOW_P0_FACTS_LIMIT, false},
    {"catalog_miss", QI_SHADOW_P0_FACTS_CATALOG_MISS, false},
    {NULL, 0, false}
};

typedef struct QiShadowP0WalkContext
{
    QiShadowP0Facts facts;
    uint16 nodes_seen;
} QiShadowP0WalkContext;

static bool qi_shadow_p0_walk_query(const Query *query,
                                    QiShadowP0WalkContext *context);

static bool
qi_shadow_p0_charge_node(QiShadowP0WalkContext *context)
{
    if (context->nodes_seen >= QI_SHADOW_P0_MAX_NODES)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
        return false;
    }
    context->nodes_seen++;
    return true;
}

static bool
qi_shadow_p0_walk_node(Node *node, void *context)
{
    if (node == NULL)
        return false;
    if (!qi_shadow_p0_charge_node((QiShadowP0WalkContext *) context))
        return true;
    if (IsA(node, Query))
        return !qi_shadow_p0_walk_query((const Query *) node,
                                        (QiShadowP0WalkContext *) context);
    return expression_tree_walker(node, qi_shadow_p0_walk_node, context);
}

static bool
qi_shadow_p0_walk_query(const Query *query, QiShadowP0WalkContext *context)
{
    ListCell *cell;

    foreach(cell, query->rtable)
    {
        const RangeTblEntry *rte = lfirst_node(RangeTblEntry, cell);

        if (!qi_shadow_p0_charge_node(context) ||
            context->facts.rtable_entries >= QI_SHADOW_P0_MAX_RTES)
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
            return false;
        }
        context->facts.rtable_entries++;

        if (rte->rtekind != RTE_RELATION)
            continue;

        context->facts.relation_entries++;
        if (!OidIsValid(rte->relid) ||
            !SearchSysCacheExists1(RELOID, ObjectIdGetDatum(rte->relid)))
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_CATALOG_MISS;
            return false;
        }
    }

    /* query_tree_walker scans WindowClause fields but not the clauses. */
    foreach(cell, query->windowClause)
    {
        if (!qi_shadow_p0_charge_node(context))
            return false;
    }

    return !query_tree_walker((Query *) query, qi_shadow_p0_walk_node,
                              context, 0);
}

void
qi_shadow_p0_publish_facts(QiShadowP0Facts facts)
{
    qi_shadow_p0_facts_state = facts.state;
    qi_shadow_p0_facts_reason = facts.reason;
}

void
qi_shadow_p0_reset_facts(void)
{
    QiShadowP0Facts facts = {
        .state = QI_SHADOW_P0_FACTS_UNKNOWN,
        .reason = QI_SHADOW_P0_FACTS_NO_QUERY,
        .rtable_entries = 0,
        .relation_entries = 0
    };

    qi_shadow_p0_publish_facts(facts);
}

void
qi_shadow_p0_init_gucs(void)
{
    DefineCustomEnumVariable("pg_qi.shadow_p0_facts_state",
                             "Reports the last bounded shadow observer state.",
                             NULL, &qi_shadow_p0_facts_state,
                             QI_SHADOW_P0_FACTS_UNKNOWN,
                             qi_shadow_p0_state_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_p0_facts_reason",
                             "Reports the last bounded shadow observer reason.",
                             NULL, &qi_shadow_p0_facts_reason,
                             QI_SHADOW_P0_FACTS_NO_QUERY,
                             qi_shadow_p0_reason_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
}

QiShadowP0Facts
qi_shadow_p0_observe_query(const Query *query)
{
    QiShadowP0WalkContext context = {
        .facts = {
            .state = QI_SHADOW_P0_FACTS_UNKNOWN,
            .reason = QI_SHADOW_P0_FACTS_NO_QUERY,
            .rtable_entries = 0,
            .relation_entries = 0
        },
        .nodes_seen = 1
    };

    if (query == NULL)
        return context.facts;

    if (!qi_shadow_p0_walk_query(query, &context))
        return context.facts;

    context.facts.state = QI_SHADOW_P0_FACTS_BOUNDED;
    context.facts.reason = QI_SHADOW_P0_FACTS_OK;
    return context.facts;
}

#if PG_QI_TESTING
Datum
qi_test_shadow_p0_observer_contract(PG_FUNCTION_ARGS)
{
    Query *query = makeNode(Query);
    RangeTblEntry *rte = makeNode(RangeTblEntry);
    QiShadowP0Facts facts;
    bool empty_bounded;
    bool null_unknown;
    bool existing_relation_bounded;
    bool catalog_miss_unknown;
    bool exact_limit_bounded;
    bool mixed_counts_valid;
    bool nested_limit_unknown;
    bool zero_rte_nodes_limit_unknown;
    bool empty_windows_limit_unknown;
    bool limit_unknown;
    Oid missing_oid = (Oid) 0xFFFFFFFEU;
    int i;

    facts = qi_shadow_p0_observe_query(NULL);
    null_unknown = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_NO_QUERY;

    facts = qi_shadow_p0_observe_query(query);
    empty_bounded = facts.state == QI_SHADOW_P0_FACTS_BOUNDED &&
        facts.reason == QI_SHADOW_P0_FACTS_OK &&
        facts.rtable_entries == 0 && facts.relation_entries == 0;

    rte->rtekind = RTE_RELATION;
    rte->relid = RelationRelationId;
    query->rtable = list_make1(rte);
    facts = qi_shadow_p0_observe_query(query);
    existing_relation_bounded =
        facts.state == QI_SHADOW_P0_FACTS_BOUNDED &&
        facts.reason == QI_SHADOW_P0_FACTS_OK &&
        facts.rtable_entries == 1 && facts.relation_entries == 1;

    while (SearchSysCacheExists1(RELOID, ObjectIdGetDatum(missing_oid)))
        missing_oid--;
    rte->relid = missing_oid;
    facts = qi_shadow_p0_observe_query(query);
    catalog_miss_unknown = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_CATALOG_MISS;

    query = makeNode(Query);
    for (i = 0; i < QI_SHADOW_P0_MAX_RTES; i++)
    {
        rte = makeNode(RangeTblEntry);
        rte->rtekind = RTE_RESULT;
        query->rtable = lappend(query->rtable, rte);
    }
    facts = qi_shadow_p0_observe_query(query);
    exact_limit_bounded = facts.state == QI_SHADOW_P0_FACTS_BOUNDED &&
        facts.reason == QI_SHADOW_P0_FACTS_OK &&
        facts.rtable_entries == QI_SHADOW_P0_MAX_RTES &&
        facts.relation_entries == 0;

    query = makeNode(Query);
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RESULT;
    query->rtable = lappend(query->rtable, rte);
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RELATION;
    rte->relid = RelationRelationId;
    query->rtable = lappend(query->rtable, rte);
    facts = qi_shadow_p0_observe_query(query);
    mixed_counts_valid = facts.state == QI_SHADOW_P0_FACTS_BOUNDED &&
        facts.reason == QI_SHADOW_P0_FACTS_OK &&
        facts.rtable_entries == 2 && facts.relation_entries == 1;

    query = makeNode(Query);
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_SUBQUERY;
    rte->subquery = makeNode(Query);
    query->rtable = list_make1(rte);
    for (i = 0; i < QI_SHADOW_P0_MAX_RTES; i++)
    {
        RangeTblEntry *nested_rte = makeNode(RangeTblEntry);

        nested_rte->rtekind = RTE_RESULT;
        rte->subquery->rtable = lappend(rte->subquery->rtable, nested_rte);
    }
    facts = qi_shadow_p0_observe_query(query);
    nested_limit_unknown = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_LIMIT;

    query = makeNode(Query);
    for (i = 0; i < QI_SHADOW_P0_MAX_NODES; i++)
    {
        CommonTableExpr *cte = makeNode(CommonTableExpr);

        cte->ctequery = (Node *) makeNode(Query);
        query->cteList = lappend(query->cteList, cte);
    }
    facts = qi_shadow_p0_observe_query(query);
    zero_rte_nodes_limit_unknown =
        facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_LIMIT;

    query = makeNode(Query);
    for (i = 0; i < QI_SHADOW_P0_MAX_NODES; i++)
        query->windowClause = lappend(query->windowClause,
                                     makeNode(WindowClause));
    facts = qi_shadow_p0_observe_query(query);
    empty_windows_limit_unknown =
        facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_LIMIT;

    query = makeNode(Query);
    for (i = 0; i <= QI_SHADOW_P0_MAX_RTES; i++)
    {
        rte = makeNode(RangeTblEntry);
        rte->rtekind = RTE_RESULT;
        query->rtable = lappend(query->rtable, rte);
    }
    facts = qi_shadow_p0_observe_query(query);
    limit_unknown = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_LIMIT;

    PG_RETURN_BOOL(null_unknown && empty_bounded &&
                   existing_relation_bounded && catalog_miss_unknown &&
                   exact_limit_bounded && mixed_counts_valid &&
                   nested_limit_unknown && zero_rte_nodes_limit_unknown &&
                   empty_windows_limit_unknown && limit_unknown);
}
#endif
