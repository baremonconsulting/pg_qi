/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>

#include "access/htup_details.h"
#include "catalog/pg_aggregate.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_attribute.h"
#include "catalog/catalog.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_proc.h"
#include "catalog/pg_type_d.h"
#include "fmgr.h"
#include "nodes/makefuncs.h"
#include "pg_qi/qi_shadow_p0.h"
#include "utils/fmgroids.h"
#include "utils/guc.h"
#include "utils/rel.h"
#include "utils/syscache.h"

static int qi_shadow_p0_facts_state = QI_SHADOW_P0_FACTS_UNKNOWN;
static int qi_shadow_p0_facts_reason = QI_SHADOW_P0_FACTS_NO_QUERY;
static int qi_shadow_p0_capability = QI_SHADOW_P0_CAPABILITY_UNKNOWN;

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
    {"unsupported_shape", QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE, false},
    {"unsupported_node", QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE, false},
    {"unsupported_relation", QI_SHADOW_P0_FACTS_UNSUPPORTED_RELATION, false},
    {"catalog_mismatch", QI_SHADOW_P0_FACTS_CATALOG_MISMATCH, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_shadow_p0_capability_options[] = {
    {"unknown", QI_SHADOW_P0_CAPABILITY_UNKNOWN, false},
    {"count_star", QI_SHADOW_P0_CAPABILITY_COUNT_STAR, false},
    {"count_int4", QI_SHADOW_P0_CAPABILITY_COUNT_INT4, false},
    {"min_int4", QI_SHADOW_P0_CAPABILITY_MIN_INT4, false},
    {"max_int4", QI_SHADOW_P0_CAPABILITY_MAX_INT4, false},
    {NULL, 0, false}
};

typedef struct QiShadowP0WalkContext
{
    QiShadowP0Facts facts;
} QiShadowP0WalkContext;

static bool
qi_shadow_p0_precheck_rtable(const Query *query,
                             QiShadowP0WalkContext *context)
{
    ListCell *cell;

    if (query->rtable != NIL && !IsA(query->rtable, List))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    if (list_length(query->rtable) > QI_SHADOW_P0_MAX_RTES)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
        return false;
    }
    foreach(cell, query->rtable)
    {
        const Node *node = lfirst(cell);
        const RangeTblEntry *rte;

        if (node == NULL || !IsA(node, RangeTblEntry))
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
            return false;
        }
        rte = (const RangeTblEntry *) node;

        if (rte->rtekind == RTE_RELATION)
            continue;
#if PG_VERSION_NUM >= 180000
        if (rte->rtekind == RTE_GROUP)
            continue;
#endif
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    return true;
}

static bool
qi_shadow_p0_charge(QiShadowP0WalkContext *context, uint16 depth)
{
    if (depth > QI_SHADOW_P0_MAX_DEPTH ||
        context->facts.nodes_seen >= QI_SHADOW_P0_MAX_NODES)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
        return false;
    }
    context->facts.nodes_seen++;
    if (depth > context->facts.max_depth_seen)
        context->facts.max_depth_seen = depth;
    return true;
}

static bool
qi_shadow_p0_emit_fact(QiShadowP0WalkContext *context)
{
    if (context->facts.facts_emitted >= QI_SHADOW_P0_MAX_FACTS)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
        return false;
    }
    context->facts.facts_emitted++;
    return true;
}

static bool
qi_shadow_p0_aggregate_catalog_matches(Oid function_oid, Oid result_type,
                                       int expected_arguments,
                                       Oid declared_argument_type)
{
    HeapTuple procedure_tuple;
    HeapTuple aggregate_tuple;
    Form_pg_proc procedure;
    Form_pg_aggregate aggregate;
    bool matches;

    procedure_tuple = SearchSysCache1(PROCOID, ObjectIdGetDatum(function_oid));
    if (!HeapTupleIsValid(procedure_tuple))
        return false;
    aggregate_tuple = SearchSysCache1(AGGFNOID,
                                      ObjectIdGetDatum(function_oid));
    if (!HeapTupleIsValid(aggregate_tuple))
    {
        ReleaseSysCache(procedure_tuple);
        return false;
    }
    procedure = (Form_pg_proc) GETSTRUCT(procedure_tuple);
    aggregate = (Form_pg_aggregate) GETSTRUCT(aggregate_tuple);
    matches = procedure->pronamespace == PG_CATALOG_NAMESPACE &&
        procedure->prokind == PROKIND_AGGREGATE &&
        !procedure->proretset && !OidIsValid(procedure->provariadic) &&
        procedure->prorettype == result_type &&
        procedure->pronargs == expected_arguments &&
        (expected_arguments == 0 ||
         procedure->proargtypes.values[0] == declared_argument_type) &&
        aggregate->aggkind == AGGKIND_NORMAL &&
        aggregate->aggnumdirectargs == 0;
    ReleaseSysCache(aggregate_tuple);
    ReleaseSysCache(procedure_tuple);
    return matches;
}

static bool
qi_shadow_p0_class_shape_supported(const Form_pg_class relation_form,
                                   char advertised_relkind)
{
    Oid namespace_oid = relation_form->relnamespace;

    return advertised_relkind == relation_form->relkind &&
        relation_form->relkind == RELKIND_RELATION &&
        relation_form->relpersistence == RELPERSISTENCE_PERMANENT &&
        relation_form->relam == HEAP_TABLE_AM_OID &&
        !relation_form->relisshared &&
        !relation_form->relispartition &&
        !relation_form->relhassubclass &&
        !relation_form->relrowsecurity &&
        !relation_form->relforcerowsecurity &&
        !IsCatalogNamespace(namespace_oid) &&
        !IsToastNamespace(namespace_oid);
}

static bool
qi_shadow_p0_relation_supported(Oid relation_oid,
                                char advertised_relkind,
                                QiShadowP0FactsReason *reason)
{
    HeapTuple relation_tuple;
    Form_pg_class relation_form;
    bool supported;

    if (!OidIsValid(relation_oid))
    {
        *reason = QI_SHADOW_P0_FACTS_CATALOG_MISS;
        return false;
    }
    relation_tuple = SearchSysCache1(RELOID, ObjectIdGetDatum(relation_oid));
    if (!HeapTupleIsValid(relation_tuple))
    {
        *reason = QI_SHADOW_P0_FACTS_CATALOG_MISS;
        return false;
    }
    relation_form = (Form_pg_class) GETSTRUCT(relation_tuple);
    if (advertised_relkind != relation_form->relkind)
    {
        ReleaseSysCache(relation_tuple);
        *reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
        return false;
    }
    supported = qi_shadow_p0_class_shape_supported(relation_form,
                                                   advertised_relkind) &&
        !has_superclass(relation_oid);
    ReleaseSysCache(relation_tuple);
    if (!supported)
        *reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_RELATION;
    return supported;
}

static bool
qi_shadow_p0_attribute_matches(Oid relation_oid, const Var *variable,
                               QiShadowP0FactsReason *reason)
{
    HeapTuple attribute_tuple;
    Form_pg_attribute attribute;
    bool matches;

    if (variable->varno != 1 || variable->varlevelsup != 0 ||
        variable->varattno <= 0 || variable->vartype != INT4OID ||
        variable->vartypmod != -1 || OidIsValid(variable->varcollid) ||
        variable->varnullingrels != NULL
#if PG_VERSION_NUM >= 180000
        || variable->varreturningtype != VAR_RETURNING_DEFAULT
#endif
        )
    {
        *reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
    attribute_tuple = SearchSysCache2(ATTNUM, ObjectIdGetDatum(relation_oid),
                                      Int16GetDatum(variable->varattno));
    if (!HeapTupleIsValid(attribute_tuple))
    {
        *reason = QI_SHADOW_P0_FACTS_CATALOG_MISS;
        return false;
    }
    attribute = (Form_pg_attribute) GETSTRUCT(attribute_tuple);
    matches = !attribute->attisdropped && attribute->atttypid == INT4OID &&
        attribute->atttypmod == variable->vartypmod;
    ReleaseSysCache(attribute_tuple);
    if (!matches)
        *reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
    return matches;
}

static bool
qi_shadow_p0_validate_relation(const Query *query,
                               QiShadowP0WalkContext *context,
                               Oid *relation_oid)
{
    const RangeTblEntry *rte;
    const Node *from_node;
    const RangeTblRef *reference;

    if (list_length(query->rtable) > QI_SHADOW_P0_MAX_RTES)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_LIMIT;
        return false;
    }
    if (list_length(query->rtable) != 1 || query->jointree == NULL ||
        !IsA(query->jointree, FromExpr) ||
        (query->jointree->fromlist != NIL &&
         !IsA(query->jointree->fromlist, List)) ||
        query->jointree->quals != NULL ||
        list_length(query->jointree->fromlist) != 1)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
    if (!qi_shadow_p0_charge(context, 2))
        return false;
    from_node = linitial(query->rtable);
    if (from_node == NULL || !IsA(from_node, RangeTblEntry))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    rte = (const RangeTblEntry *) from_node;
    context->facts.rtable_entries = 1;
    if (rte->rtekind != RTE_RELATION || !OidIsValid(rte->relid) ||
        rte->lateral || rte->tablesample != NULL || rte->securityQuals != NIL)
    {
        context->facts.reason = rte->rtekind == RTE_RELATION
            ? QI_SHADOW_P0_FACTS_UNSUPPORTED_RELATION
            : QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    context->facts.relation_entries = 1;
    if (!qi_shadow_p0_charge(context, 2) ||
        !qi_shadow_p0_charge(context, 3))
        return false;
    from_node = linitial(query->jointree->fromlist);
    if (from_node == NULL || !IsA(from_node, RangeTblRef))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    reference = (const RangeTblRef *) from_node;
    if (reference->rtindex != 1)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
    if (!qi_shadow_p0_relation_supported(rte->relid, rte->relkind,
                                         &context->facts.reason))
        return false;
    *relation_oid = rte->relid;
    return true;
}

static bool
qi_shadow_p0_validate_aggregate(const Query *query, Oid relation_oid,
                                QiShadowP0WalkContext *context)
{
    const TargetEntry *target;
    const Aggref *aggregate;
    const TargetEntry *argument_target;
    const Var *variable;
    QiShadowP0Capability capability = QI_SHADOW_P0_CAPABILITY_UNKNOWN;
    Oid result_type;
    Oid declared_argument_type;
    int expected_arguments;

    if (query->targetList != NIL && !IsA(query->targetList, List))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    if (list_length(query->targetList) != 1)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
    if (!qi_shadow_p0_charge(context, 2))
        return false;
    target = (const TargetEntry *) linitial(query->targetList);
    if (target == NULL || !IsA(target, TargetEntry))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    if (target->resjunk || target->expr == NULL ||
        !IsA(target->expr, Aggref))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return false;
    }
    if (!qi_shadow_p0_charge(context, 3))
        return false;
    aggregate = (const Aggref *) target->expr;
    if ((aggregate->args != NIL && !IsA(aggregate->args, List)) ||
        (aggregate->aggargtypes != NIL &&
         !IsA(aggregate->aggargtypes, OidList)) ||
        aggregate->agglevelsup != 0 || aggregate->aggdistinct != NIL ||
        aggregate->aggorder != NIL || aggregate->aggfilter != NULL ||
        aggregate->aggdirectargs != NIL || aggregate->aggvariadic ||
        aggregate->aggkind != AGGKIND_NORMAL ||
        aggregate->aggsplit != AGGSPLIT_SIMPLE || aggregate->aggpresorted ||
        OidIsValid(aggregate->aggcollid) ||
        OidIsValid(aggregate->inputcollid))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
    if (aggregate->aggfnoid == F_COUNT_ && aggregate->aggstar &&
        aggregate->args == NIL && aggregate->aggtype == INT8OID)
    {
        capability = QI_SHADOW_P0_CAPABILITY_COUNT_STAR;
        result_type = INT8OID;
        expected_arguments = 0;
        declared_argument_type = InvalidOid;
        if (aggregate->aggargtypes != NIL)
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
            return false;
        }
    }
    else
    {
        if (aggregate->aggstar || list_length(aggregate->args) != 1)
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
            return false;
        }
        if (aggregate->aggfnoid == F_COUNT_ANY && aggregate->aggtype == INT8OID)
        {
            capability = QI_SHADOW_P0_CAPABILITY_COUNT_INT4;
            result_type = INT8OID;
        }
        else if (aggregate->aggfnoid == F_MIN_INT4 &&
                 aggregate->aggtype == INT4OID)
        {
            capability = QI_SHADOW_P0_CAPABILITY_MIN_INT4;
            result_type = INT4OID;
        }
        else if (aggregate->aggfnoid == F_MAX_INT4 &&
                 aggregate->aggtype == INT4OID)
        {
            capability = QI_SHADOW_P0_CAPABILITY_MAX_INT4;
            result_type = INT4OID;
        }
        else
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
            return false;
        }
        expected_arguments = 1;
        declared_argument_type = aggregate->aggfnoid == F_COUNT_ANY
            ? ANYOID : INT4OID;
        if (list_length(aggregate->aggargtypes) != 1 ||
            linitial_oid(aggregate->aggargtypes) != INT4OID)
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
            return false;
        }
        if (!qi_shadow_p0_charge(context, 4))
            return false;
        argument_target = (const TargetEntry *) linitial(aggregate->args);
        if (argument_target == NULL || !IsA(argument_target, TargetEntry))
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
            return false;
        }
        if (argument_target->resjunk || argument_target->expr == NULL ||
            !IsA(argument_target->expr, Var))
        {
            context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
            return false;
        }
        if (!qi_shadow_p0_charge(context, 5))
            return false;
        variable = (const Var *) argument_target->expr;
        if (!qi_shadow_p0_attribute_matches(relation_oid, variable,
                                            &context->facts.reason))
            return false;
        context->facts.argument_attno = variable->varattno;
    }
    if (!qi_shadow_p0_aggregate_catalog_matches(aggregate->aggfnoid,
                                                result_type,
                                                expected_arguments,
                                                declared_argument_type))
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
        return false;
    }
    if (!qi_shadow_p0_emit_fact(context))
        return false;
    context->facts.capability = capability;
    return true;
}

static bool
qi_shadow_p0_query_shape_supported(const Query *query,
                                   QiShadowP0WalkContext *context)
{
    if (query->commandType != CMD_SELECT || query->utilityStmt != NULL ||
        query->resultRelation != 0 || query->cteList != NIL ||
        !query->hasAggs || query->hasWindowFuncs || query->hasTargetSRFs ||
        query->hasSubLinks || query->hasDistinctOn || query->hasRecursive ||
        query->hasModifyingCTE || query->hasForUpdate ||
        query->hasRowSecurity || query->isReturn ||
        query->mergeActionList != NIL || query->override != OVERRIDING_NOT_SET ||
        query->onConflict != NULL || query->returningList != NIL ||
        query->withCheckOptions != NIL || query->distinctClause != NIL ||
        query->sortClause != NIL || query->groupClause != NIL ||
        query->groupDistinct || query->groupingSets != NIL ||
        query->havingQual != NULL ||
        query->windowClause != NIL || query->limitOffset != NULL ||
        query->limitCount != NULL || query->rowMarks != NIL ||
        query->setOperations != NULL)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
#if PG_VERSION_NUM < 170000
    if (query->mergeUseOuterJoin)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
#else
    if (query->mergeTargetRelation != 0 || query->mergeJoinCondition != NULL)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
#endif
#if PG_VERSION_NUM >= 180000
    if (query->hasGroupRTE)
    {
        context->facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;
        return false;
    }
#endif
    return true;
}

void
qi_shadow_p0_publish_facts(QiShadowP0Facts facts)
{
    qi_shadow_p0_facts_state = facts.state;
    qi_shadow_p0_facts_reason = facts.reason;
    qi_shadow_p0_capability = facts.state == QI_SHADOW_P0_FACTS_BOUNDED
        ? facts.capability : QI_SHADOW_P0_CAPABILITY_UNKNOWN;
}

void
qi_shadow_p0_reset_facts(void)
{
    QiShadowP0Facts facts = {
        .state = QI_SHADOW_P0_FACTS_UNKNOWN,
        .reason = QI_SHADOW_P0_FACTS_NO_QUERY,
        .capability = QI_SHADOW_P0_CAPABILITY_UNKNOWN
    };
    qi_shadow_p0_publish_facts(facts);
}

void
qi_shadow_p0_init_gucs(void)
{
    DefineCustomEnumVariable("pg_qi.shadow_p0_facts_state",
                             "Reports the last bounded P0 capability state.",
                             NULL, &qi_shadow_p0_facts_state,
                             QI_SHADOW_P0_FACTS_UNKNOWN,
                             qi_shadow_p0_state_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_p0_facts_reason",
                             "Reports the last bounded P0 capability reason.",
                             NULL, &qi_shadow_p0_facts_reason,
                             QI_SHADOW_P0_FACTS_NO_QUERY,
                             qi_shadow_p0_reason_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_p0_capability",
                             "Reports the last bounded P0 capability.",
                             NULL, &qi_shadow_p0_capability,
                             QI_SHADOW_P0_CAPABILITY_UNKNOWN,
                             qi_shadow_p0_capability_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
}

QiShadowP0Facts
qi_shadow_p0_observe_query(const Query *query)
{
    QiShadowP0WalkContext context = {
        .facts = {
            .state = QI_SHADOW_P0_FACTS_UNKNOWN,
            .reason = QI_SHADOW_P0_FACTS_NO_QUERY,
            .capability = QI_SHADOW_P0_CAPABILITY_UNKNOWN,
            .relation_oid = InvalidOid,
            .argument_attno = InvalidAttrNumber
        }
    };
    Oid relation_oid = InvalidOid;

    if (query == NULL)
        return context.facts;
    if (!IsA(query, Query))
    {
        context.facts.reason = QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;
        return context.facts;
    }
    if (!qi_shadow_p0_precheck_rtable(query, &context) ||
        !qi_shadow_p0_charge(&context, 1) ||
        !qi_shadow_p0_query_shape_supported(query, &context) ||
        !qi_shadow_p0_validate_relation(query, &context, &relation_oid) ||
        !qi_shadow_p0_validate_aggregate(query, relation_oid, &context))
        return context.facts;
    context.facts.state = QI_SHADOW_P0_FACTS_BOUNDED;
    context.facts.reason = QI_SHADOW_P0_FACTS_OK;
    context.facts.relation_oid = relation_oid;
    return context.facts;
}

#if PG_QI_TESTING
Datum
qi_test_shadow_p0_observer_contract(PG_FUNCTION_ARGS)
{
    Query *query = makeNode(Query);
    Query *aggregate_query;
    RangeTblEntry *rte;
    Aggref *aggregate;
    QiShadowP0Facts facts;
    bool null_unknown;
    bool empty_rejected;
    bool unknown_node_rejected;
    bool exact_node_limit_allowed = true;
    bool node_limit_plus_one_rejected;
    bool exact_depth_allowed;
    bool depth_plus_one_rejected;
    bool exact_fact_limit_allowed;
    bool fact_limit_plus_one_rejected;
    bool wrong_root_rejected;
    bool malformed_list_rejected;
    bool null_from_rejected;
    bool valid_count_star_shape;
    bool direct_argument_rejected;
    bool bad_argtypes_rejected;
    bool split_mode_rejected;
    bool parameter_rejected;
    bool unknown_aggregate_rejected;
    bool catalog_signature_rejected;
    bool missing_relation_rejected;
    bool relkind_mismatch_rejected;
    bool missing_attribute_rejected;
    bool custom_am_rejected;
    bool mutation_fuzz_safe = true;
    bool exact_rte_budget_not_limited;
    bool rte_budget_plus_one_limited;
    QiShadowP0WalkContext bounds_context = {0};
    int i;

    facts = qi_shadow_p0_observe_query(NULL);
    null_unknown = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_NO_QUERY;
    query->commandType = CMD_SELECT;
    facts = qi_shadow_p0_observe_query(query);
    empty_rejected = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE;

    facts = qi_shadow_p0_observe_query((const Query *) makeNode(Const));
    wrong_root_rejected = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;

    query = makeNode(Query);
    query->commandType = CMD_SELECT;
    query->rtable = list_make1_int(1);
    facts = qi_shadow_p0_observe_query(query);
    malformed_list_rejected =
        facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;

    query = makeNode(Query);
    query->commandType = CMD_SELECT;
    query->hasAggs = true;
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RESULT;
    query->rtable = list_make1(rte);
    query->jointree = makeFromExpr(list_make1(makeNode(Const)), NULL);
    facts = qi_shadow_p0_observe_query(query);
    unknown_node_rejected = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;

    query = makeNode(Query);
    query->commandType = CMD_SELECT;
    query->hasAggs = true;
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RELATION;
    rte->relid = RelationRelationId;
    rte->relkind = RELKIND_RELATION;
    query->rtable = list_make1(rte);
    query->jointree = makeFromExpr(list_make1(NULL), NULL);
    facts = qi_shadow_p0_observe_query(query);
    null_from_rejected = facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;

    aggregate_query = makeNode(Query);
    aggregate = makeNode(Aggref);
    aggregate->aggfnoid = F_COUNT_;
    aggregate->aggtype = INT8OID;
    aggregate->aggkind = AGGKIND_NORMAL;
    aggregate->aggstar = true;
    aggregate->aggsplit = AGGSPLIT_SIMPLE;
    aggregate_query->targetList = list_make1(
        makeTargetEntry((Expr *) aggregate, 1, NULL, false));
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    valid_count_star_shape = qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context) &&
        bounds_context.facts.capability ==
            QI_SHADOW_P0_CAPABILITY_COUNT_STAR;

    aggregate->aggdirectargs = list_make1(makeNode(Param));
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    direct_argument_rejected = !qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context);
    aggregate->aggdirectargs = NIL;

    aggregate->aggargtypes = list_make1_oid(INT4OID);
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    bad_argtypes_rejected = !qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context);
    aggregate->aggargtypes = NIL;

    aggregate->aggsplit = AGGSPLIT_INITIAL_SERIAL;
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    split_mode_rejected = !qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context);
    aggregate->aggsplit = AGGSPLIT_SIMPLE;

    aggregate->aggfnoid = F_COUNT_ANY;
    aggregate->aggstar = false;
    aggregate->aggargtypes = list_make1_oid(INT4OID);
    aggregate->args = list_make1(
        makeTargetEntry((Expr *) makeNode(Param), 1, NULL, false));
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    parameter_rejected = !qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context) &&
        bounds_context.facts.reason == QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE;

    aggregate->aggfnoid = (Oid) 1;
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    unknown_aggregate_rejected = !qi_shadow_p0_validate_aggregate(
        aggregate_query, InvalidOid, &bounds_context) &&
        bounds_context.facts.reason == QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;

    catalog_signature_rejected =
        !qi_shadow_p0_aggregate_catalog_matches(F_COUNT_ANY, INT8OID, 1,
                                                INT4OID);

    {
        QiShadowP0FactsReason reason = QI_SHADOW_P0_FACTS_OK;
        Var variable = {0};
        FormData_pg_class class_form = {0};

        missing_relation_rejected = !qi_shadow_p0_relation_supported(
            InvalidOid, RELKIND_RELATION, &reason) &&
            reason == QI_SHADOW_P0_FACTS_CATALOG_MISS;
        reason = QI_SHADOW_P0_FACTS_OK;
        relkind_mismatch_rejected = !qi_shadow_p0_relation_supported(
            RelationRelationId, RELKIND_VIEW, &reason) &&
            reason == QI_SHADOW_P0_FACTS_CATALOG_MISMATCH;
        NodeSetTag(&variable, T_Var);
        variable.varno = 1;
        variable.varattno = SHRT_MAX;
        variable.vartype = INT4OID;
        variable.vartypmod = -1;
        reason = QI_SHADOW_P0_FACTS_OK;
        missing_attribute_rejected = !qi_shadow_p0_attribute_matches(
            RelationRelationId, &variable, &reason) &&
            reason == QI_SHADOW_P0_FACTS_CATALOG_MISS;
        class_form.relkind = RELKIND_RELATION;
        class_form.relpersistence = RELPERSISTENCE_PERMANENT;
        class_form.relnamespace = PG_PUBLIC_NAMESPACE;
        class_form.relam = InvalidOid;
        custom_am_rejected = !qi_shadow_p0_class_shape_supported(
            &class_form, RELKIND_RELATION);
    }

    /*
     * Deterministic, well-allocated tag mutation corpus.  It deliberately
     * includes current and future-looking tag/kind values; every case must
     * abstain without dispatching a generic walker or raising an ERROR.
     */
    for (i = 0; i < 1024; i++)
    {
        Query *mutated = palloc0(sizeof(Query));

        mutated->type = (NodeTag) i;
        facts = qi_shadow_p0_observe_query(mutated);
        mutation_fuzz_safe = mutation_fuzz_safe &&
            facts.state == QI_SHADOW_P0_FACTS_UNKNOWN;
    }

    query = makeNode(Query);
    query->commandType = CMD_SELECT;
    query->hasAggs = true;
    for (i = 0; i < QI_SHADOW_P0_MAX_RTES; i++)
    {
        rte = makeNode(RangeTblEntry);
        rte->rtekind = RTE_RESULT;
        query->rtable = lappend(query->rtable, rte);
    }
    facts = qi_shadow_p0_observe_query(query);
    exact_rte_budget_not_limited =
        facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason != QI_SHADOW_P0_FACTS_LIMIT;
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RESULT;
    query->rtable = lappend(query->rtable, rte);
    facts = qi_shadow_p0_observe_query(query);
    rte_budget_plus_one_limited =
        facts.state == QI_SHADOW_P0_FACTS_UNKNOWN &&
        facts.reason == QI_SHADOW_P0_FACTS_LIMIT;
    for (i = 0; i < 64; i++)
    {
        Query *mutated = makeNode(Query);
        RangeTblEntry *mutated_rte = makeNode(RangeTblEntry);

        mutated->commandType = CMD_SELECT;
        mutated->hasAggs = true;
        mutated_rte->rtekind = (RTEKind) i;
        mutated->rtable = list_make1(mutated_rte);
        facts = qi_shadow_p0_observe_query(mutated);
        mutation_fuzz_safe = mutation_fuzz_safe &&
            facts.state == QI_SHADOW_P0_FACTS_UNKNOWN;
    }
    for (i = 0; i < 1024; i++)
    {
        Var *mutated_argument = palloc0(sizeof(Var));

        NodeSetTag(mutated_argument, (NodeTag) i);
        aggregate->aggfnoid = F_COUNT_ANY;
        aggregate->aggstar = false;
        aggregate->aggargtypes = list_make1_oid(INT4OID);
        aggregate->args = list_make1(
            makeTargetEntry((Expr *) mutated_argument, 1, NULL, false));
        MemSet(&bounds_context, 0, sizeof(bounds_context));
        mutation_fuzz_safe = mutation_fuzz_safe &&
            !qi_shadow_p0_validate_aggregate(aggregate_query,
                                             RelationRelationId,
                                             &bounds_context);
    }

    MemSet(&bounds_context, 0, sizeof(bounds_context));
    for (i = 0; i < QI_SHADOW_P0_MAX_NODES; i++)
        exact_node_limit_allowed = exact_node_limit_allowed &&
            qi_shadow_p0_charge(&bounds_context, 1);
    node_limit_plus_one_rejected =
        !qi_shadow_p0_charge(&bounds_context, 1) &&
        bounds_context.facts.reason == QI_SHADOW_P0_FACTS_LIMIT;
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    exact_depth_allowed = qi_shadow_p0_charge(&bounds_context,
                                               QI_SHADOW_P0_MAX_DEPTH);
    depth_plus_one_rejected = !qi_shadow_p0_charge(
        &bounds_context, QI_SHADOW_P0_MAX_DEPTH + 1) &&
        bounds_context.facts.reason == QI_SHADOW_P0_FACTS_LIMIT;
    MemSet(&bounds_context, 0, sizeof(bounds_context));
    bounds_context.facts.facts_emitted = QI_SHADOW_P0_MAX_FACTS - 1;
    exact_fact_limit_allowed = qi_shadow_p0_emit_fact(&bounds_context) &&
        bounds_context.facts.facts_emitted == QI_SHADOW_P0_MAX_FACTS;
    fact_limit_plus_one_rejected = !qi_shadow_p0_emit_fact(&bounds_context) &&
        bounds_context.facts.reason == QI_SHADOW_P0_FACTS_LIMIT;

    PG_RETURN_BOOL(null_unknown && empty_rejected && wrong_root_rejected &&
                   malformed_list_rejected &&
                   unknown_node_rejected && null_from_rejected &&
                   valid_count_star_shape && direct_argument_rejected &&
                   bad_argtypes_rejected && split_mode_rejected &&
                   parameter_rejected && unknown_aggregate_rejected &&
                   catalog_signature_rejected &&
                   missing_relation_rejected && relkind_mismatch_rejected &&
                   missing_attribute_rejected && custom_am_rejected &&
                   mutation_fuzz_safe &&
                   exact_rte_budget_not_limited &&
                   rte_budget_plus_one_limited &&
                   exact_node_limit_allowed && node_limit_plus_one_rejected &&
                   exact_depth_allowed && depth_plus_one_rejected &&
                   exact_fact_limit_allowed && fact_limit_plus_one_rejected);
}
#endif
