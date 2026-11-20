/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <math.h>

#include "access/htup_details.h"
#include "catalog/pg_aggregate.h"
#include "catalog/pg_type_d.h"
#include "catalog/pg_class_d.h"
#include "catalog/pg_database_d.h"
#include "catalog/pg_class.h"
#include "fmgr.h"
#include "nodes/execnodes.h"
#include "nodes/bitmapset.h"
#include "nodes/makefuncs.h"
#include "nodes/plannodes.h"
#include "nodes/primnodes.h"
#include "optimizer/cost.h"
#include "pg_qi/qi_plan_features.h"
#include "utils/fmgroids.h"
#include "utils/syscache.h"

static bool
qi_plan_estimate_valid(const Plan *plan)
{
    return isfinite(plan->startup_cost) && plan->startup_cost >= 0.0 &&
        isfinite(plan->total_cost) &&
        plan->total_cost >= plan->startup_cost &&
        isfinite(plan->plan_rows) && plan->plan_rows >= 0.0 &&
        plan->plan_width >= 0;
}

static bool
qi_plan_path_disabled(const Plan *plan)
{
#if PG_VERSION_NUM >= 180000
    return plan->disabled_nodes > 0;
#else
    return plan->startup_cost >= disable_cost ||
        plan->total_cost >= disable_cost;
#endif
}

static QiPlanNodeEstimate
qi_plan_estimate(const Plan *plan)
{
    QiPlanNodeEstimate estimate;

    estimate.startup_cost = plan->startup_cost;
    estimate.total_cost = plan->total_cost;
    estimate.rows = plan->plan_rows;
    estimate.width = plan->plan_width;
    return estimate;
}

QiNativePlanFeatures
qi_plan_features_unknown(QiPlanFeatureReason reason)
{
    QiNativePlanFeatures features;

    MemSet(&features, 0, sizeof(features));
    features.state = QI_PLAN_FEATURE_UNKNOWN;
    features.reason = reason;
    features.spine = QI_PLAN_SPINE_NONE;
    features.relation_oid = InvalidOid;
    features.reltuples = -1.0;
    return features;
}

static bool
qi_plan_has_parameters(const Plan *plan)
{
    return plan->extParam != NULL || plan->allParam != NULL;
}

static QiPlanFeatureReason
qi_plan_unsupported_node_reason(const Plan *plan,
                                QiPlanFeatureReason fallback)
{
    if (plan == NULL)
        return QI_PLAN_FEATURE_MALFORMED;
    switch (nodeTag(plan))
    {
        case T_Sort:
        case T_IncrementalSort:
            return QI_PLAN_FEATURE_SORT;
        case T_Gather:
            return QI_PLAN_FEATURE_GATHER;
        case T_GatherMerge:
            return QI_PLAN_FEATURE_GATHER_MERGE;
        case T_Limit:
            return QI_PLAN_FEATURE_LIMIT_NODE;
        case T_CustomScan:
            return QI_PLAN_FEATURE_CUSTOM_SCAN;
        case T_IndexScan:
        case T_IndexOnlyScan:
        case T_BitmapHeapScan:
        case T_TidScan:
        case T_TidRangeScan:
            return QI_PLAN_FEATURE_ALTERNATE_SCAN;
        default:
            return fallback;
    }
}

static const TargetEntry *
qi_plan_target_entry_by_resno(const List *targetlist, AttrNumber resno)
{
    ListCell *cell;

    if (targetlist != NIL && !IsA(targetlist, List))
        return NULL;
    foreach(cell, targetlist)
    {
        const Node *node = lfirst(cell);
        const TargetEntry *entry;

        if (node == NULL || !IsA(node, TargetEntry))
            return NULL;
        entry = (const TargetEntry *) node;
        if (entry->resno == resno)
            return entry;
    }
    return NULL;
}

static bool
qi_plan_target_is_aggregate(const Agg *agg, const SeqScan *scan,
                            const QiShadowP0Facts *p0)
{
    const TargetEntry *entry;
    const Aggref *aggregate;
    const Node *entry_node;
    int argument_count;
    Oid expected_function;

    if (agg->plan.targetlist == NIL ||
        !IsA(agg->plan.targetlist, List) ||
        list_length(agg->plan.targetlist) != 1)
        return false;
    entry_node = linitial(agg->plan.targetlist);
    if (entry_node == NULL || !IsA(entry_node, TargetEntry))
        return false;
    entry = (const TargetEntry *) entry_node;
    if (entry->resjunk || entry->expr == NULL ||
        !IsA(entry->expr, Aggref))
        return false;
    aggregate = (const Aggref *) entry->expr;
    if (aggregate->aggkind != AGGKIND_NORMAL ||
        aggregate->agglevelsup != 0 || aggregate->aggdirectargs != NIL ||
        aggregate->aggorder != NIL || aggregate->aggdistinct != NIL ||
        aggregate->aggfilter != NULL)
        return false;
    if (aggregate->args != NIL && !IsA(aggregate->args, List))
        return false;
    argument_count = list_length(aggregate->args);
    if (p0->capability == QI_SHADOW_P0_CAPABILITY_COUNT_STAR)
        return aggregate->aggfnoid == F_COUNT_ &&
            aggregate->aggtype == INT8OID && aggregate->aggstar &&
            argument_count == 0;
    if (p0->capability == QI_SHADOW_P0_CAPABILITY_COUNT_INT4 ||
        p0->capability == QI_SHADOW_P0_CAPABILITY_MIN_INT4 ||
        p0->capability == QI_SHADOW_P0_CAPABILITY_MAX_INT4)
    {
        const TargetEntry *argument;
        const Var *outer_variable;
        const TargetEntry *scan_entry;
        const Var *base_variable;

        expected_function = p0->capability == QI_SHADOW_P0_CAPABILITY_COUNT_INT4
            ? F_COUNT_ANY
            : (p0->capability == QI_SHADOW_P0_CAPABILITY_MIN_INT4
               ? F_MIN_INT4 : F_MAX_INT4);
        if (aggregate->aggfnoid != expected_function || aggregate->aggstar ||
            argument_count != 1 ||
            (p0->capability == QI_SHADOW_P0_CAPABILITY_COUNT_INT4
             ? aggregate->aggtype != INT8OID
             : aggregate->aggtype != INT4OID))
            return false;
        entry_node = linitial(aggregate->args);
        if (entry_node == NULL || !IsA(entry_node, TargetEntry))
            return false;
        argument = (const TargetEntry *) entry_node;
        if (argument->resjunk || argument->expr == NULL ||
            !IsA(argument->expr, Var))
            return false;
        outer_variable = (const Var *) argument->expr;
        if (outer_variable->varno != OUTER_VAR ||
            outer_variable->varattno <= 0 || outer_variable->varlevelsup != 0)
            return false;
        scan_entry = qi_plan_target_entry_by_resno(scan->scan.plan.targetlist,
                                                    outer_variable->varattno);
        if (scan_entry == NULL || scan_entry->resjunk ||
            scan_entry->expr == NULL || !IsA(scan_entry->expr, Var))
            return false;
        base_variable = (const Var *) scan_entry->expr;
        return base_variable->varno == scan->scan.scanrelid &&
            base_variable->varattno == p0->argument_attno &&
            base_variable->varlevelsup == 0 &&
            base_variable->vartype == INT4OID;
    }
    return false;
}

QiNativePlanFeatures
qi_plan_features_observe(const PlannedStmt *stmt,
                         const QiShadowP0Facts *p0)
{
    QiNativePlanFeatures features =
        qi_plan_features_unknown(QI_PLAN_FEATURE_NOT_OBSERVED);
    const Agg *agg;
    const SeqScan *scan;
    const RangeTblEntry *rte;
    HeapTuple relation_tuple;
    Form_pg_class relation;
    int rtable_length;

    if (stmt == NULL || p0 == NULL)
    {
        features.reason = QI_PLAN_FEATURE_MALFORMED;
        return features;
    }
    if (stmt->commandType != CMD_SELECT)
    {
        features.reason = QI_PLAN_FEATURE_UNSUPPORTED_ROOT;
        return features;
    }
    if (p0->state != QI_SHADOW_P0_FACTS_BOUNDED ||
        p0->reason != QI_SHADOW_P0_FACTS_OK ||
        !OidIsValid(p0->relation_oid))
    {
        features.reason = QI_PLAN_FEATURE_P0_UNKNOWN;
        return features;
    }
    if (stmt->rtable != NIL && !IsA(stmt->rtable, List))
    {
        features.reason = QI_PLAN_FEATURE_MALFORMED;
        return features;
    }
    rtable_length = list_length(stmt->rtable);
    if (rtable_length > QI_PLAN_FEATURE_MAX_RTES)
    {
        features.reason = QI_PLAN_FEATURE_RTE_LIMIT;
        return features;
    }
    if (stmt->subplans != NIL || stmt->rowMarks != NIL)
    {
        features.reason = QI_PLAN_FEATURE_AUXILIARY_PLAN;
        return features;
    }
    if (stmt->planTree == NULL || !IsA(stmt->planTree, Agg))
    {
        features.reason = qi_plan_unsupported_node_reason(
            stmt->planTree, QI_PLAN_FEATURE_UNSUPPORTED_ROOT);
        return features;
    }
    features.nodes_seen = 1;
    features.max_depth_seen = 1;
    agg = (const Agg *) stmt->planTree;
    if (agg->aggstrategy != AGG_PLAIN)
    {
        features.reason = QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
        return features;
    }
    if (agg->aggsplit != AGGSPLIT_SIMPLE)
    {
        features.reason = agg->aggsplit == AGGSPLIT_FINAL_DESERIAL
            ? QI_PLAN_FEATURE_FINAL_AGGREGATE
            : QI_PLAN_FEATURE_PARTIAL_AGGREGATE;
        return features;
    }
    if (agg->numCols != 0 || agg->groupingSets != NIL || agg->chain != NIL ||
        agg->plan.qual != NIL || agg->plan.initPlan != NIL ||
        agg->plan.righttree != NULL || qi_plan_has_parameters(&agg->plan))
    {
        features.reason = QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
        return features;
    }
    if (agg->plan.lefttree == NULL || !IsA(agg->plan.lefttree, SeqScan))
    {
        features.reason = qi_plan_unsupported_node_reason(
            agg->plan.lefttree, QI_PLAN_FEATURE_UNSUPPORTED_SCAN);
        return features;
    }
    features.nodes_seen = 2;
    features.max_depth_seen = 2;
    scan = (const SeqScan *) agg->plan.lefttree;
    if (scan->scan.plan.lefttree != NULL ||
        scan->scan.plan.righttree != NULL ||
        scan->scan.plan.initPlan != NIL || scan->scan.plan.qual != NIL ||
        qi_plan_has_parameters(&scan->scan.plan) ||
        scan->scan.plan.parallel_aware ||
        scan->scan.scanrelid <= 0 || scan->scan.scanrelid > rtable_length)
    {
        features.reason = QI_PLAN_FEATURE_UNSUPPORTED_SCAN;
        return features;
    }
    if (!qi_plan_target_is_aggregate(agg, scan, p0))
    {
        features.reason = QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
        return features;
    }
    if (qi_plan_path_disabled(&agg->plan) ||
        qi_plan_path_disabled(&scan->scan.plan))
    {
        features.reason = QI_PLAN_FEATURE_DISABLED_PATH;
        return features;
    }
    {
        const Node *rte_node = list_nth(stmt->rtable,
                                        scan->scan.scanrelid - 1);

        if (rte_node == NULL || !IsA(rte_node, RangeTblEntry))
        {
            features.reason = QI_PLAN_FEATURE_MALFORMED;
            return features;
        }
        rte = (const RangeTblEntry *) rte_node;
    }
    if (rte->rtekind != RTE_RELATION ||
        rte->relid != p0->relation_oid)
    {
        features.reason = QI_PLAN_FEATURE_RELATION_MISMATCH;
        return features;
    }
    if (!qi_plan_estimate_valid(&agg->plan) ||
        !qi_plan_estimate_valid(&scan->scan.plan))
    {
        features.reason = QI_PLAN_FEATURE_INVALID_ESTIMATE;
        return features;
    }

    relation_tuple = SearchSysCache1(RELOID,
                                      ObjectIdGetDatum(p0->relation_oid));
    if (!HeapTupleIsValid(relation_tuple))
    {
        features.reason = QI_PLAN_FEATURE_CATALOG_MISS;
        return features;
    }
    relation = (Form_pg_class) GETSTRUCT(relation_tuple);

    features.state = QI_PLAN_FEATURE_BOUNDED;
    features.reason = QI_PLAN_FEATURE_OK;
    features.spine = QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN;
    features.relation_oid = p0->relation_oid;
    features.aggregate = qi_plan_estimate(&agg->plan);
    features.scan = qi_plan_estimate(&scan->scan.plan);
    features.relpages = relation->relpages;
    features.reltuples = relation->reltuples;
    features.aggregate_parallel_safe = agg->plan.parallel_safe;
    features.scan_parallel_safe = scan->scan.plan.parallel_safe;
    features.parallel_aware = false;
    features.parallel_mode_needed = stmt->parallelModeNeeded;
    features.workers_planned = 0;
    features.has_limit = false;
    features.known_fields = QI_PLAN_KNOWN_RELATION |
        QI_PLAN_KNOWN_AGG_ESTIMATE | QI_PLAN_KNOWN_SCAN_ESTIMATE |
        QI_PLAN_KNOWN_PARALLEL |
        QI_PLAN_KNOWN_WORKERS | QI_PLAN_KNOWN_LIMIT;
    if (relation->reltuples >= 0.0 && isfinite(relation->reltuples))
    {
        features.known_fields |= QI_PLAN_KNOWN_RELTUPLES;
        if (relation->relpages >= 0)
            features.known_fields |= QI_PLAN_KNOWN_RELPAGES;
    }
    ReleaseSysCache(relation_tuple);
    return features;
}

const char *
qi_plan_feature_state_name(QiPlanFeatureState state)
{
    return state == QI_PLAN_FEATURE_BOUNDED ? "bounded" : "unknown";
}

const char *
qi_plan_feature_reason_name(QiPlanFeatureReason reason)
{
    switch (reason)
    {
        case QI_PLAN_FEATURE_NOT_OBSERVED: return "not_observed";
        case QI_PLAN_FEATURE_OK: return "ok";
        case QI_PLAN_FEATURE_P0_UNKNOWN: return "p0_unknown";
        case QI_PLAN_FEATURE_RTE_LIMIT: return "rte_limit";
        case QI_PLAN_FEATURE_AUXILIARY_PLAN: return "auxiliary_plan";
        case QI_PLAN_FEATURE_UNSUPPORTED_ROOT: return "unsupported_root";
        case QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE:
            return "unsupported_aggregate";
        case QI_PLAN_FEATURE_PARTIAL_AGGREGATE: return "partial_aggregate";
        case QI_PLAN_FEATURE_FINAL_AGGREGATE: return "final_aggregate";
        case QI_PLAN_FEATURE_SORT: return "sort";
        case QI_PLAN_FEATURE_GATHER: return "gather";
        case QI_PLAN_FEATURE_GATHER_MERGE: return "gather_merge";
        case QI_PLAN_FEATURE_LIMIT_NODE: return "limit_node";
        case QI_PLAN_FEATURE_CUSTOM_SCAN: return "custom_scan";
        case QI_PLAN_FEATURE_ALTERNATE_SCAN: return "alternate_scan";
        case QI_PLAN_FEATURE_UNSUPPORTED_SCAN: return "unsupported_scan";
        case QI_PLAN_FEATURE_RELATION_MISMATCH: return "relation_mismatch";
        case QI_PLAN_FEATURE_CATALOG_MISS: return "catalog_miss";
        case QI_PLAN_FEATURE_INVALID_ESTIMATE: return "invalid_estimate";
        case QI_PLAN_FEATURE_DISABLED_PATH: return "disabled_path";
        case QI_PLAN_FEATURE_MALFORMED: return "malformed";
    }
    return "invalid";
}

const char *
qi_plan_feature_spine_name(QiPlanFeatureSpine spine)
{
    return spine == QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN
        ? "plain_agg_seq_scan" : "none";
}

#if PG_QI_TESTING
Datum
qi_test_plan_features_contract(PG_FUNCTION_ARGS)
{
    QiNativePlanFeatures unknown =
        qi_plan_features_unknown(QI_PLAN_FEATURE_NOT_OBSERVED);
    PlannedStmt *stmt = makeNode(PlannedStmt);
    Agg *agg = makeNode(Agg);
    SeqScan *scan = makeNode(SeqScan);
    RangeTblEntry *rte = makeNode(RangeTblEntry);
    Aggref *aggregate = makeNode(Aggref);
    QiShadowP0Facts p0;
    QiNativePlanFeatures result;
    bool default_ok;
    bool positive_ok;
    bool partial_rejected;
    bool final_rejected;
    bool auxiliary_rejected;
    bool initplan_rejected;
    bool rowmark_rejected;
    bool parameter_rejected;
    bool grouping_rejected;
    bool having_rejected;
    bool wrapper_rejected;
    bool custom_rejected;
    bool root_reason_matrix;
    bool relation_rejected;
    bool estimate_rejected;
    bool estimate_matrix_rejected;
    bool disabled_rejected;
    bool scan_rejected;
    bool scan_index_rejected;
    bool malformed_rejected;
    bool rte_boundary_ok;
    bool rte_limit_rejected;
    bool p0_rejected;
    bool typed_positive;
    bool typed_link_rejected;

    default_ok = unknown.state == QI_PLAN_FEATURE_UNKNOWN &&
        unknown.known_fields == 0 && unknown.reltuples == -1.0 &&
        strcmp(qi_plan_feature_reason_name(unknown.reason),
               "not_observed") == 0;

    MemSet(&p0, 0, sizeof(p0));
    p0.state = QI_SHADOW_P0_FACTS_BOUNDED;
    p0.reason = QI_SHADOW_P0_FACTS_OK;
    p0.capability = QI_SHADOW_P0_CAPABILITY_COUNT_STAR;
    p0.relation_oid = RelationRelationId;

    rte->rtekind = RTE_RELATION;
    rte->relid = RelationRelationId;
    stmt->commandType = CMD_SELECT;
    stmt->rtable = list_make1(rte);
    stmt->planTree = &agg->plan;
    agg->aggstrategy = AGG_PLAIN;
    agg->aggsplit = AGGSPLIT_SIMPLE;
    agg->plan.startup_cost = 10.0;
    agg->plan.total_cost = 11.0;
    agg->plan.plan_rows = 1.0;
    agg->plan.plan_width = 8;
    agg->plan.parallel_safe = true;
    agg->plan.lefttree = &scan->scan.plan;
    aggregate->aggfnoid = F_COUNT_;
    aggregate->aggtype = INT8OID;
    aggregate->aggkind = AGGKIND_NORMAL;
    aggregate->aggstar = true;
    agg->plan.targetlist = list_make1(
        makeTargetEntry((Expr *) aggregate, 1, NULL, false));
    scan->scan.scanrelid = 1;
    scan->scan.plan.startup_cost = 0.0;
    scan->scan.plan.total_cost = 10.0;
    scan->scan.plan.plan_rows = 100.0;
    scan->scan.plan.plan_width = 0;

#if PG_VERSION_NUM >= 180000
    scan->scan.plan.disabled_nodes = 1;
#else
    scan->scan.plan.total_cost = disable_cost;
#endif
    disabled_rejected =
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_DISABLED_PATH;
#if PG_VERSION_NUM >= 180000
    scan->scan.plan.disabled_nodes = 0;
#else
    scan->scan.plan.total_cost = 10.0;
#endif
    scan->scan.plan.parallel_safe = true;

    result = qi_plan_features_observe(stmt, &p0);
    positive_ok = result.state == QI_PLAN_FEATURE_BOUNDED &&
        result.reason == QI_PLAN_FEATURE_OK &&
        result.spine == QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN &&
        result.relation_oid == RelationRelationId &&
        result.scan.width == 0 && result.nodes_seen == 2;

    agg->aggsplit = AGGSPLIT_INITIAL_SERIAL;
    result = qi_plan_features_observe(stmt, &p0);
    partial_rejected = result.reason == QI_PLAN_FEATURE_PARTIAL_AGGREGATE;
    agg->aggsplit = AGGSPLIT_FINAL_DESERIAL;
    result = qi_plan_features_observe(stmt, &p0);
    final_rejected = result.reason == QI_PLAN_FEATURE_FINAL_AGGREGATE;
    agg->aggsplit = AGGSPLIT_SIMPLE;

    stmt->subplans = list_make1(makeNode(Result));
    result = qi_plan_features_observe(stmt, &p0);
    auxiliary_rejected = result.reason == QI_PLAN_FEATURE_AUXILIARY_PLAN;
    stmt->subplans = NIL;

    agg->plan.initPlan = list_make1(makeNode(Result));
    result = qi_plan_features_observe(stmt, &p0);
    initplan_rejected =
        result.reason == QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
    agg->plan.initPlan = NIL;

    stmt->rowMarks = list_make1(makeNode(PlanRowMark));
    result = qi_plan_features_observe(stmt, &p0);
    rowmark_rejected = result.reason == QI_PLAN_FEATURE_AUXILIARY_PLAN;
    stmt->rowMarks = NIL;

    agg->plan.extParam = bms_make_singleton(1);
    result = qi_plan_features_observe(stmt, &p0);
    parameter_rejected =
        result.reason == QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
    agg->plan.extParam = NULL;

    agg->numCols = 1;
    result = qi_plan_features_observe(stmt, &p0);
    grouping_rejected =
        result.reason == QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
    agg->numCols = 0;
    agg->plan.qual = list_make1(makeBoolConst(true, false));
    result = qi_plan_features_observe(stmt, &p0);
    having_rejected =
        result.reason == QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;
    agg->plan.qual = NIL;

    {
        Limit *limit = makeNode(Limit);
        CustomScan *custom = makeNode(CustomScan);
        Sort *sort = makeNode(Sort);
        Gather *gather = makeNode(Gather);
        GatherMerge *gather_merge = makeNode(GatherMerge);
        IndexScan *index_scan = makeNode(IndexScan);

        agg->plan.lefttree = &limit->plan;
        result = qi_plan_features_observe(stmt, &p0);
        wrapper_rejected = result.reason == QI_PLAN_FEATURE_LIMIT_NODE;
        agg->plan.lefttree = &custom->scan.plan;
        result = qi_plan_features_observe(stmt, &p0);
        custom_rejected = result.reason == QI_PLAN_FEATURE_CUSTOM_SCAN;
        agg->plan.lefttree = &scan->scan.plan;

        stmt->planTree = &sort->plan;
        root_reason_matrix =
            qi_plan_features_observe(stmt, &p0).reason == QI_PLAN_FEATURE_SORT;
        stmt->planTree = &gather->plan;
        root_reason_matrix = root_reason_matrix &&
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_GATHER;
        stmt->planTree = &gather_merge->plan;
        root_reason_matrix = root_reason_matrix &&
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_GATHER_MERGE;
        stmt->planTree = &limit->plan;
        root_reason_matrix = root_reason_matrix &&
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_LIMIT_NODE;
        stmt->planTree = &custom->scan.plan;
        root_reason_matrix = root_reason_matrix &&
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_CUSTOM_SCAN;
        stmt->planTree = &index_scan->scan.plan;
        root_reason_matrix = root_reason_matrix &&
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_ALTERNATE_SCAN;
        stmt->planTree = &agg->plan;
    }

    rte->relid = DatabaseRelationId;
    result = qi_plan_features_observe(stmt, &p0);
    relation_rejected = result.reason == QI_PLAN_FEATURE_RELATION_MISMATCH;
    rte->relid = RelationRelationId;

    scan->scan.plan.total_cost = NAN;
    result = qi_plan_features_observe(stmt, &p0);
    estimate_rejected = result.reason == QI_PLAN_FEATURE_INVALID_ESTIMATE;
    scan->scan.plan.total_cost = 10.0;

    estimate_matrix_rejected = true;
    agg->plan.startup_cost = -1.0;
    estimate_matrix_rejected = estimate_matrix_rejected &&
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_INVALID_ESTIMATE;
    agg->plan.startup_cost = 10.0;
    agg->plan.total_cost = 9.0;
    estimate_matrix_rejected = estimate_matrix_rejected &&
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_INVALID_ESTIMATE;
    agg->plan.total_cost = 11.0;
    scan->scan.plan.plan_rows = INFINITY;
    estimate_matrix_rejected = estimate_matrix_rejected &&
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_INVALID_ESTIMATE;
    scan->scan.plan.plan_rows = 100.0;
    scan->scan.plan.plan_width = -1;
    estimate_matrix_rejected = estimate_matrix_rejected &&
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_INVALID_ESTIMATE;
    scan->scan.plan.plan_width = 0;

    scan->scan.plan.qual = list_make1(makeBoolConst(true, false));
    result = qi_plan_features_observe(stmt, &p0);
    scan_rejected = result.reason == QI_PLAN_FEATURE_UNSUPPORTED_SCAN;
    scan->scan.plan.qual = NIL;

    scan->scan.scanrelid = 0;
    scan_index_rejected =
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_UNSUPPORTED_SCAN;
    scan->scan.scanrelid = 2;
    scan_index_rejected = scan_index_rejected &&
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_UNSUPPORTED_SCAN;
    scan->scan.scanrelid = 1;

    stmt->rtable = list_make1(makeNode(Result));
    malformed_rejected =
        qi_plan_features_observe(stmt, &p0).reason ==
            QI_PLAN_FEATURE_MALFORMED;
    stmt->rtable = NIL;
    {
        int index;

        for (index = 0; index < QI_PLAN_FEATURE_MAX_RTES; index++)
            stmt->rtable = lappend(stmt->rtable, rte);
        rte_boundary_ok =
            qi_plan_features_observe(stmt, &p0).state ==
                QI_PLAN_FEATURE_BOUNDED;
        stmt->rtable = lappend(stmt->rtable, rte);
        rte_limit_rejected =
            qi_plan_features_observe(stmt, &p0).reason ==
                QI_PLAN_FEATURE_RTE_LIMIT;
        stmt->rtable = list_make1(rte);
    }

    p0.state = QI_SHADOW_P0_FACTS_UNKNOWN;
    result = qi_plan_features_observe(stmt, &p0);
    p0_rejected = result.reason == QI_PLAN_FEATURE_P0_UNKNOWN;
    p0.state = QI_SHADOW_P0_FACTS_BOUNDED;

    p0.capability = QI_SHADOW_P0_CAPABILITY_COUNT_INT4;
    p0.argument_attno = 1;
    aggregate->aggfnoid = F_COUNT_ANY;
    aggregate->aggstar = false;
    scan->scan.plan.targetlist = list_make1(makeTargetEntry(
        (Expr *) makeVar(1, 1, INT4OID, -1, InvalidOid, 0),
        1, NULL, false));
    aggregate->args = list_make1(makeTargetEntry(
        (Expr *) makeVar(OUTER_VAR, 1, INT4OID, -1, InvalidOid, 0),
        1, NULL, false));
    result = qi_plan_features_observe(stmt, &p0);
    typed_positive = result.state == QI_PLAN_FEATURE_BOUNDED;
    ((Var *) ((TargetEntry *) linitial(aggregate->args))->expr)->varattno = 2;
    result = qi_plan_features_observe(stmt, &p0);
    typed_link_rejected =
        result.reason == QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE;

    PG_RETURN_BOOL(default_ok && positive_ok && partial_rejected &&
                   final_rejected && auxiliary_rejected &&
                   initplan_rejected && rowmark_rejected &&
                   parameter_rejected && grouping_rejected &&
                   having_rejected && wrapper_rejected && custom_rejected &&
                   root_reason_matrix &&
                   relation_rejected && estimate_rejected &&
                   estimate_matrix_rejected && disabled_rejected &&
                   scan_rejected &&
                   scan_index_rejected && malformed_rejected &&
                   rte_boundary_ok && rte_limit_rejected && p0_rejected &&
                   typed_positive && typed_link_rejected);
}
#endif
