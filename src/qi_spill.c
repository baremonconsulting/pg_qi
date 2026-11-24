/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <math.h>

#include "access/htup_details.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_type_d.h"
#include "common/int.h"
#include "nodes/nodeFuncs.h"
#include "utils/fmgroids.h"
#include "utils/guc.h"
#include "utils/syscache.h"
#include "pg_qi/qi_spill.h"

#define QI_SPILL_MAX_NODES       4
#define QI_SPILL_MAX_COLUMNS     16
#define QI_SPILL_MAX_EXPRESSIONS 64
#define QI_SPILL_MAX_EXACT_ROWS  9007199254740992.0
#define QI_SPILL_MIB             UINT64CONST(1048576)
#define QI_SPILL_HASH_OVERHEAD   64U
#define QI_SPILL_SORT_OVERHEAD   32U
#define QI_SPILL_ROW_MARGIN      2U

typedef struct QiSpillAggregateSearch
{
    unsigned int remaining;
    bool found;
} QiSpillAggregateSearch;

static bool
qi_spill_find_ordered_aggregate(Node *node, void *arg)
{
    QiSpillAggregateSearch *search = arg;

    if (node == NULL || IsA(node, Query))
        return false;
    if (search->remaining == 0)
        return true;
    --search->remaining;

    if (IsA(node, Aggref))
    {
        const Aggref *call = (const Aggref *) node;

        search->found = call->agglevelsup == 0 &&
            (call->aggdistinct != NIL || call->aggorder != NIL);
        return search->found;
    }
    return expression_tree_walker(node, qi_spill_find_ordered_aggregate, arg);
}

bool
qi_spill_query_requested(const Query *query)
{
    QiSpillAggregateSearch search = {.remaining = QI_SPILL_MAX_EXPRESSIONS};

    if (query == NULL || query->commandType != CMD_SELECT)
        return false;
    if (query->groupClause != NIL || query->groupingSets != NIL ||
        query->distinctClause != NIL || query->sortClause != NIL)
        return true;
    if (!query->hasAggs)
        return false;

    /* Find DISTINCT or ordering within scalar aggregate calls. */
    if (!qi_spill_find_ordered_aggregate((Node *) query->targetList, &search))
        qi_spill_find_ordered_aggregate(query->havingQual, &search);
    return search.found;
}

/* Size the supported DuckDB values independently of PostgreSQL's mean width. */
static unsigned int
qi_spill_fixed_width(Oid type)
{
    switch (type)
    {
        case BOOLOID: return 1;
        case INT2OID: return 2;
        case INT4OID: return 4;
        case INT8OID: return 8;
    }
    return 0;
}

static const TargetEntry *
qi_spill_target(const List *targets, AttrNumber number)
{
    ListCell *cell;

    foreach(cell, targets)
    {
        const TargetEntry *target = lfirst_node(TargetEntry, cell);

        if (target->resno == number)
            return target;
    }
    return NULL;
}

static bool
qi_spill_key_bytes(const Plan *input, int count, const AttrNumber *columns,
                   uint64 *bytes)
{
    int index;

    if (input == NULL)
        return false;
    *bytes = 0;
    if (count <= 0 || count > QI_SPILL_MAX_COLUMNS || columns == NULL ||
        list_length(input->targetlist) > QI_SPILL_MAX_COLUMNS)
        return false;
    for (index = 0; index < count; ++index)
    {
        const TargetEntry *target = qi_spill_target(input->targetlist,
                                                   columns[index]);
        unsigned int width = target == NULL ? 0 :
            qi_spill_fixed_width(exprType((const Node *) target->expr));

        if (width == 0)
            return false;
        *bytes += width;
    }
    return true;
}

static QiSpillReason
qi_spill_add_sort(const Plan *node, uint64 *per_row)
{
    ListCell *cell;
    uint64 width = 0;

    foreach(cell, node->targetlist)
    {
        const TargetEntry *target = lfirst_node(TargetEntry, cell);
        unsigned int bytes = qi_spill_fixed_width(
            exprType((const Node *) target->expr));

        if (bytes == 0)
            return QI_SPILL_UNBOUNDED_WIDTH;
        width += bytes;
    }
    *per_row += Max(width, (uint64) node->plan_width) +
        QI_SPILL_SORT_OVERHEAD;
    return QI_SPILL_ESTIMATE_FITS;
}

static QiSpillReason
qi_spill_add_aggregate(const Agg *aggregate, QiSpillShape *shape,
                       uint64 *per_row)
{
    const Plan *node = &aggregate->plan;
    ListCell *cell;
    unsigned int counts = 0;
    uint64 distinct_bytes = 0;
    uint64 key_bytes = 0;

    if (aggregate->aggsplit != AGGSPLIT_SIMPLE ||
        aggregate->aggstrategy == AGG_MIXED ||
        aggregate->groupingSets != NIL || aggregate->chain != NIL ||
        node->qual != NIL)
        return QI_SPILL_UNSUPPORTED_PLAN;
    if (aggregate->numCols > 0 &&
        !qi_spill_key_bytes(node->lefttree, aggregate->numCols,
                            aggregate->grpColIdx, &key_bytes))
        return QI_SPILL_UNBOUNDED_WIDTH;
    foreach(cell, node->targetlist)
    {
        const TargetEntry *target = lfirst_node(TargetEntry, cell);
        const Aggref *call;

        if (IsA(target->expr, Var))
        {
            if (qi_spill_fixed_width(exprType((const Node *) target->expr)) == 0)
                return QI_SPILL_UNBOUNDED_WIDTH;
            continue;
        }
        if (!IsA(target->expr, Aggref))
            return QI_SPILL_UNSUPPORTED_PLAN;
        call = (const Aggref *) target->expr;
        if ((call->aggfnoid != F_COUNT_ && call->aggfnoid != F_COUNT_ANY) ||
            call->aggfilter != NULL || call->aggorder != NIL ||
            call->aggdirectargs != NIL || call->aggsplit != AGGSPLIT_SIMPLE)
            return QI_SPILL_UNSUPPORTED_PLAN;
        ++counts;
        if (call->aggfnoid == F_COUNT_ANY)
        {
            const TargetEntry *arg;
            unsigned int width;

            if (list_length(call->args) != 1 ||
                list_length(call->aggdistinct) > 1)
                return QI_SPILL_UNSUPPORTED_PLAN;
            arg = linitial_node(TargetEntry, call->args);
            width = qi_spill_fixed_width(exprType((const Node *) arg->expr));
            if (width == 0)
                return QI_SPILL_UNBOUNDED_WIDTH;
            if (call->aggdistinct != NIL)
                distinct_bytes += key_bytes + width + QI_SPILL_HASH_OVERHEAD;
        }
    }
    if (aggregate->numCols == 0 && distinct_bytes == 0)
        return QI_SPILL_UNSUPPORTED_PLAN;
    if (aggregate->numCols > 0)
        *per_row += Max(key_bytes, (uint64) node->plan_width) +
            QI_SPILL_HASH_OVERHEAD + counts * 8U;
    *per_row += distinct_bytes;
    *shape = counts == 0 || distinct_bytes != 0 ? QI_SPILL_DISTINCT :
        QI_SPILL_HASH_AGGREGATE;
    return QI_SPILL_ESTIMATE_FITS;
}

static bool
qi_spill_input_rows(const PlannedStmt *stmt, const SeqScan *scan, uint64 *rows)
{
    Index index = scan->scan.scanrelid;
    const RangeTblEntry *rte;
    Form_pg_class relation;
    HeapTuple tuple;
    double estimate;

    if (index == 0 || index > (Index) list_length(stmt->rtable))
        return false;
    rte = list_nth_node(RangeTblEntry, stmt->rtable, index - 1);
    if (rte->rtekind != RTE_RELATION || rte->relkind != RELKIND_RELATION)
        return false;
    tuple = SearchSysCache1(RELOID, ObjectIdGetDatum(rte->relid));
    if (!HeapTupleIsValid(tuple))
        return false;
    relation = (Form_pg_class) GETSTRUCT(tuple);
    estimate = relation->relam == HEAP_TABLE_AM_OID ? relation->reltuples : -1;
    ReleaseSysCache(tuple);
    if (!isfinite(estimate) || estimate < 0.0 ||
        estimate >= QI_SPILL_MAX_EXACT_ROWS)
        return false;

    /* A filtered scan can have fewer estimated rows than the heap. */
    *rows = (uint64) ceil(Max(estimate, scan->scan.plan.plan_rows));
    return true;
}

static bool
qi_spill_positive_setting(const char *name, int *value)
{
    const char *setting = GetConfigOption(name, true, false);

    return setting != NULL &&
        (GetConfigOptionFlags(name, true) & GUC_CUSTOM_PLACEHOLDER) == 0 &&
        parse_int(setting, value, 0, NULL) && *value > 0;
}

QiSpillEvidence
qi_spill_observe(const PlannedStmt *stmt)
{
    QiSpillEvidence evidence = {
        .state = QI_SPILL_UNKNOWN,
        .reason = QI_SPILL_UNSUPPORTED_PLAN
    };
    const Plan *node;
    uint64 per_row = 0;
    uint64 rows;
    uint64 bytes;
    bool grouped = false;
    int depth;
    int memory_mib;
    int threads;

    if (stmt == NULL || stmt->commandType != CMD_SELECT ||
        stmt->hasModifyingCTE || stmt->parallelModeNeeded ||
        stmt->subplans != NIL ||
        list_length(stmt->rtable) > QI_SPILL_MAX_COLUMNS)
        return evidence;

    node = stmt->planTree;
    for (depth = 0; node != NULL && depth < QI_SPILL_MAX_NODES; ++depth)
    {
        QiSpillReason reason = QI_SPILL_ESTIMATE_FITS;

        if (node->righttree != NULL || node->initPlan != NIL ||
            node->extParam != NULL || node->allParam != NULL ||
            node->parallel_aware ||
            list_length(node->targetlist) > QI_SPILL_MAX_COLUMNS)
            return evidence;
        if (!isfinite(node->plan_rows) || node->plan_rows < 0.0 ||
            node->plan_rows >= QI_SPILL_MAX_EXACT_ROWS || node->plan_width < 0)
        {
            evidence.reason = QI_SPILL_INVALID_ESTIMATE;
            return evidence;
        }
        if (IsA(node, SeqScan))
            break;
        if (node->lefttree == NULL)
            return evidence;
        if (IsA(node, Sort))
        {
            if (evidence.shape == QI_SPILL_NONE)
                evidence.shape = QI_SPILL_SORT;
            reason = qi_spill_add_sort(node, &per_row);
        }
        else if (IsA(node, Agg) && !grouped)
        {
            grouped = true;
            reason = qi_spill_add_aggregate((const Agg *) node,
                                             &evidence.shape, &per_row);
        }
        else if (IsA(node, Unique) && !grouped)
        {
            const Unique *unique = (const Unique *) node;
            uint64 width;

            grouped = true;
            evidence.shape = QI_SPILL_DISTINCT;
            if (!qi_spill_key_bytes(node->lefttree, unique->numCols,
                                    unique->uniqColIdx, &width))
                reason = QI_SPILL_UNBOUNDED_WIDTH;
            else
                per_row += width + QI_SPILL_HASH_OVERHEAD;
        }
        else
            return evidence;
        if (reason != QI_SPILL_ESTIMATE_FITS)
        {
            evidence.reason = reason;
            return evidence;
        }
        node = node->lefttree;
    }
    if (depth == QI_SPILL_MAX_NODES || node == NULL ||
        !IsA(node, SeqScan) || node->lefttree != NULL || per_row == 0)
        return evidence;
    if (!qi_spill_input_rows(stmt, (const SeqScan *) node, &rows))
    {
        evidence.reason = QI_SPILL_INVALID_ESTIMATE;
        return evidence;
    }
    if (!qi_spill_positive_setting("duckdb.memory_limit", &memory_mib) ||
        !qi_spill_positive_setting("duckdb.threads", &threads))
    {
        evidence.reason = QI_SPILL_BUDGET_UNKNOWN;
        return evidence;
    }
    evidence.budget_bytes = (uint64) memory_mib * QI_SPILL_MIB;
    /*
     * Charge all observed stages against the larger input estimate. Reserve
     * one MiB per worker and allow duplicated state and row growth.
     */
    if (pg_mul_u64_overflow(rows, per_row, &bytes) ||
        pg_mul_u64_overflow(bytes, QI_SPILL_ROW_MARGIN, &bytes) ||
        pg_mul_u64_overflow(bytes, (uint64) threads, &bytes) ||
        pg_add_u64_overflow(bytes, (uint64) threads * QI_SPILL_MIB, &bytes))
    {
        evidence.reason = QI_SPILL_INVALID_ESTIMATE;
        return evidence;
    }
    evidence.estimated_bytes = bytes;
    if (bytes >= evidence.budget_bytes)
    {
        evidence.state = QI_SPILL_LIKELY;
        evidence.reason = QI_SPILL_RESOURCES_UNVERIFIED;
    }
    else
    {
        evidence.state = QI_SPILL_WITHIN_BUDGET;
        evidence.reason = QI_SPILL_ESTIMATE_FITS;
    }
    return evidence;
}

const char *
qi_spill_state_name(QiSpillState state)
{
    switch (state)
    {
        case QI_SPILL_NOT_EVALUATED: return "not_evaluated";
        case QI_SPILL_UNKNOWN: return "unknown";
        case QI_SPILL_WITHIN_BUDGET: return "within_budget";
        case QI_SPILL_LIKELY: return "likely_spill";
    }
    return "invalid";
}

const char *
qi_spill_reason_name(QiSpillReason reason)
{
    switch (reason)
    {
        case QI_SPILL_NOT_OBSERVED: return "not_observed";
        case QI_SPILL_UNSUPPORTED_PLAN: return "unsupported_plan";
        case QI_SPILL_UNBOUNDED_WIDTH: return "unbounded_width";
        case QI_SPILL_INVALID_ESTIMATE: return "invalid_estimate";
        case QI_SPILL_BUDGET_UNKNOWN: return "budget_unknown";
        case QI_SPILL_ESTIMATE_FITS: return "estimate_fits";
        case QI_SPILL_RESOURCES_UNVERIFIED: return "spill_resources_unverified";
    }
    return "invalid";
}

const char *
qi_spill_shape_name(QiSpillShape shape)
{
    switch (shape)
    {
        case QI_SPILL_NONE: return "none";
        case QI_SPILL_HASH_AGGREGATE: return "hash_aggregate";
        case QI_SPILL_DISTINCT: return "distinct";
        case QI_SPILL_SORT: return "sort";
    }
    return "invalid";
}
