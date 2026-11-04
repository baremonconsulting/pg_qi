/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "funcapi.h"
#include "nodes/extensible.h"
#include "nodes/makefuncs.h"
#include "nodes/pg_list.h"
#include "pg_qi/qi_detector.h"

static bool qi_visit_plan(const Plan *plan);
static bool qi_visit_plan_list(const List *plans);

static bool
qi_visit_plan_list(const List *plans)
{
    const ListCell *cell;

    foreach(cell, plans)
    {
        if (qi_visit_plan((const Plan *) lfirst(cell)))
            return true;
    }

    return false;
}

static bool
qi_visit_plan(const Plan *plan)
{
    if (plan == NULL)
        return false;

    if (IsA(plan, CustomScan))
    {
        const CustomScan *custom_scan = (const CustomScan *) plan;

        if (custom_scan->methods != NULL &&
            custom_scan->methods->CustomName != NULL &&
            strcmp(custom_scan->methods->CustomName,
                   QI_DUCKDB_SCAN_PROVIDER) == 0)
            return true;
    }

    if (qi_visit_plan(plan->lefttree) || qi_visit_plan(plan->righttree))
        return true;

    switch (nodeTag(plan))
    {
        case T_Append:
            return qi_visit_plan_list(((const Append *) plan)->appendplans);
        case T_MergeAppend:
            return qi_visit_plan_list(((const MergeAppend *) plan)->mergeplans);
        case T_BitmapAnd:
            return qi_visit_plan_list(((const BitmapAnd *) plan)->bitmapplans);
        case T_BitmapOr:
            return qi_visit_plan_list(((const BitmapOr *) plan)->bitmapplans);
        case T_SubqueryScan:
            return qi_visit_plan(((const SubqueryScan *) plan)->subplan);
        case T_CustomScan:
            return qi_visit_plan_list(((const CustomScan *) plan)->custom_plans);
        default:
            return false;
    }
}

bool
qi_plan_has_duckdb_scan(const PlannedStmt *planned_stmt)
{
    if (planned_stmt == NULL)
        return false;

    return qi_visit_plan(planned_stmt->planTree) ||
           qi_visit_plan_list(planned_stmt->subplans);
}

#if PG_QI_TESTING
Datum
qi_test_detector_contract(PG_FUNCTION_ARGS)
{
    static const CustomScanMethods duckdb_methods = {
        .CustomName = QI_DUCKDB_SCAN_PROVIDER
    };
    static const CustomScanMethods other_methods = {
        .CustomName = "NotDuckDBScan"
    };
    PlannedStmt root_stmt = {0};
    PlannedStmt material_stmt = {0};
    PlannedStmt subplan_stmt = {0};
    PlannedStmt absent_stmt = {0};
    CustomScan root_scan = {0};
    CustomScan material_scan = {0};
    CustomScan subplan_scan = {0};
    CustomScan other_scan = {0};
    Material material = {0};
    Result result = {0};
    TupleDesc tuple_desc;
    Datum values[4];
    bool nulls[4] = {false, false, false, false};

    NodeSetTag(&root_stmt, T_PlannedStmt);
    NodeSetTag(&material_stmt, T_PlannedStmt);
    NodeSetTag(&subplan_stmt, T_PlannedStmt);
    NodeSetTag(&absent_stmt, T_PlannedStmt);
    NodeSetTag(&root_scan, T_CustomScan);
    NodeSetTag(&material_scan, T_CustomScan);
    NodeSetTag(&subplan_scan, T_CustomScan);
    NodeSetTag(&other_scan, T_CustomScan);
    NodeSetTag(&material, T_Material);
    NodeSetTag(&result, T_Result);

    root_scan.methods = &duckdb_methods;
    root_stmt.planTree = &root_scan.scan.plan;

    material_scan.methods = &duckdb_methods;
    material.plan.lefttree = &material_scan.scan.plan;
    material_stmt.planTree = &material.plan;

    subplan_scan.methods = &duckdb_methods;
    subplan_stmt.planTree = &result.plan;
    subplan_stmt.subplans = list_make1(&subplan_scan.scan.plan);

    other_scan.methods = &other_methods;
    absent_stmt.planTree = &other_scan.scan.plan;

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR,
                (errmsg("return type must be a row type")));

    values[0] = BoolGetDatum(qi_plan_has_duckdb_scan(&root_stmt));
    values[1] = BoolGetDatum(qi_plan_has_duckdb_scan(&material_stmt));
    values[2] = BoolGetDatum(qi_plan_has_duckdb_scan(&subplan_stmt));
    values[3] = BoolGetDatum(!qi_plan_has_duckdb_scan(&absent_stmt));

    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}
#endif
