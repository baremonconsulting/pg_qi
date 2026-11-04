/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "catalog/namespace.h"
#include "catalog/pg_class.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_operator.h"
#include "catalog/pg_type_d.h"
#include "funcapi.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_classify.h"
#include "utils/lsyscache.h"
#include "utils/guc.h"
#include "utils/syscache.h"

typedef struct QiClassifyContext
{
    QiClassifyReason reason;
} QiClassifyContext;

static bool qi_classify_expression(Node *node, QiClassifyContext *context);

static bool
qi_reject(QiClassifyContext *context, QiClassifyReason reason)
{
    if (context->reason == QI_CLASSIFY_ELIGIBLE)
        context->reason = reason;
    return false;
}

static bool
qi_type_allowed(Oid type_oid)
{
    return type_oid == BOOLOID || type_oid == INT2OID ||
           type_oid == INT4OID || type_oid == INT8OID ||
           type_oid == DATEOID || type_oid == TIMESTAMPOID;
}

static bool
qi_integer_type(Oid type_oid)
{
    return type_oid == INT2OID || type_oid == INT4OID || type_oid == INT8OID;
}

static bool
qi_classify_operator(OpExpr *expression, QiClassifyContext *context)
{
    HeapTuple tuple;
    Form_pg_operator op;
    const char *name;
    bool comparison;
    bool arithmetic;
    ListCell *cell;

    tuple = SearchSysCache1(OPEROID, ObjectIdGetDatum(expression->opno));
    if (!HeapTupleIsValid(tuple))
        return qi_reject(context, QI_CLASSIFY_CATALOG_MISMATCH);
    op = (Form_pg_operator) GETSTRUCT(tuple);
    name = NameStr(op->oprname);
    comparison = strcmp(name, "=") == 0 || strcmp(name, "<>") == 0 ||
                 strcmp(name, "<") == 0 || strcmp(name, "<=") == 0 ||
                 strcmp(name, ">") == 0 || strcmp(name, ">=") == 0;
    arithmetic = strcmp(name, "+") == 0 || strcmp(name, "-") == 0 ||
                 strcmp(name, "*") == 0;
    if (op->oprnamespace != PG_CATALOG_NAMESPACE ||
        (!comparison && !arithmetic))
    {
        ReleaseSysCache(tuple);
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_OPERATOR);
    }
    if (arithmetic && (!qi_integer_type(op->oprleft) ||
                       !qi_integer_type(op->oprright)))
    {
        ReleaseSysCache(tuple);
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_OPERATOR);
    }
    if (comparison && (!qi_type_allowed(op->oprleft) ||
                       !qi_type_allowed(op->oprright)))
    {
        ReleaseSysCache(tuple);
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_TYPE);
    }
    ReleaseSysCache(tuple);

    foreach(cell, expression->args)
        if (!qi_classify_expression((Node *) lfirst(cell), context))
            return false;
    return true;
}

static bool
qi_classify_aggregate(Aggref *aggregate, QiClassifyContext *context)
{
    const char *name;
    ListCell *cell;

    if (get_func_namespace(aggregate->aggfnoid) != PG_CATALOG_NAMESPACE ||
        aggregate->aggdistinct != NIL || aggregate->aggorder != NIL ||
        aggregate->aggfilter != NULL || aggregate->aggfnoid == InvalidOid)
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_AGGREGATE);

    name = get_func_name(aggregate->aggfnoid);
    if (name == NULL)
        return qi_reject(context, QI_CLASSIFY_CATALOG_MISMATCH);
    if (strcmp(name, "count") != 0 && strcmp(name, "min") != 0 &&
        strcmp(name, "max") != 0)
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_AGGREGATE);
    if (strcmp(name, "count") != 0 && !qi_type_allowed(aggregate->aggtype))
        return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_TYPE);

    foreach(cell, aggregate->args)
    {
        TargetEntry *entry = castNode(TargetEntry, lfirst(cell));

        if (!qi_classify_expression((Node *) entry->expr, context))
            return false;
    }
    return true;
}

static bool
qi_classify_expression(Node *node, QiClassifyContext *context)
{
    ListCell *cell;

    if (node == NULL)
        return true;
    switch (nodeTag(node))
    {
        case T_Var:
        case T_Const:
            if (!qi_type_allowed(exprType(node)))
                return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_TYPE);
            return true;
        case T_RelabelType:
            return qi_classify_expression((Node *) ((RelabelType *) node)->arg,
                                          context);
        case T_BoolExpr:
            foreach(cell, ((BoolExpr *) node)->args)
                if (!qi_classify_expression((Node *) lfirst(cell), context))
                    return false;
            return true;
        case T_NullTest:
            return qi_classify_expression((Node *) ((NullTest *) node)->arg,
                                          context);
        case T_OpExpr:
            return qi_classify_operator((OpExpr *) node, context);
        case T_Aggref:
            return qi_classify_aggregate((Aggref *) node, context);
        case T_FuncExpr:
        {
            FuncExpr *function = (FuncExpr *) node;

            if (function->funcretset || list_length(function->args) != 1 ||
                function->funcformat != COERCE_IMPLICIT_CAST ||
                get_func_namespace(function->funcid) != PG_CATALOG_NAMESPACE ||
                !qi_integer_type(function->funcresulttype) ||
                !qi_integer_type(exprType(linitial(function->args))))
                return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_NODE);
            return qi_classify_expression(linitial(function->args), context);
        }
        default:
            return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_NODE);
    }
}

static bool
qi_classify_from(Node *node, QiClassifyContext *context)
{
    ListCell *cell;

    if (node == NULL)
        return true;
    if (IsA(node, RangeTblRef))
        return true;
    if (IsA(node, FromExpr))
    {
        FromExpr *from = (FromExpr *) node;

        foreach(cell, from->fromlist)
            if (!qi_classify_from((Node *) lfirst(cell), context))
                return false;
        return qi_classify_expression(from->quals, context);
    }
    if (IsA(node, JoinExpr))
    {
        JoinExpr *join = (JoinExpr *) node;
        OpExpr *operator;
        HeapTuple tuple;
        Form_pg_operator op;
        Node *left;
        Node *right;

        if (join->jointype != JOIN_INNER || join->usingClause != NIL ||
            join->join_using_alias != NULL || join->quals == NULL ||
            !IsA(join->quals, OpExpr))
            return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_JOIN);
        operator = (OpExpr *) join->quals;
        if (list_length(operator->args) != 2)
            return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_JOIN);
        left = linitial(operator->args);
        right = lsecond(operator->args);
        tuple = SearchSysCache1(OPEROID, ObjectIdGetDatum(operator->opno));
        if (!HeapTupleIsValid(tuple))
            return qi_reject(context, QI_CLASSIFY_CATALOG_MISMATCH);
        op = (Form_pg_operator) GETSTRUCT(tuple);
        if (op->oprnamespace != PG_CATALOG_NAMESPACE ||
            strcmp(NameStr(op->oprname), "=") != 0 ||
            !IsA(left, Var) || !IsA(right, Var) ||
            ((Var *) left)->varno == ((Var *) right)->varno ||
            !qi_integer_type(exprType(left)) ||
            !qi_integer_type(exprType(right)))
        {
            ReleaseSysCache(tuple);
            return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_JOIN);
        }
        ReleaseSysCache(tuple);
        if (!qi_classify_from(join->larg, context) ||
            !qi_classify_from(join->rarg, context))
            return false;
        return qi_classify_expression(join->quals, context);
    }
    return qi_reject(context, QI_CLASSIFY_UNSUPPORTED_JOIN);
}

QiClassifyResult
qi_classify_query(const Query *query)
{
    QiClassifyContext context = {QI_CLASSIFY_ELIGIBLE};
    ListCell *cell;

    if (query == NULL || query->commandType != CMD_SELECT)
        context.reason = QI_CLASSIFY_NOT_SELECT;
    else if (query->setOperations != NULL || query->hasWindowFuncs ||
             query->hasTargetSRFs || query->hasSubLinks ||
             query->cteList != NIL || query->rowMarks != NIL ||
             query->distinctClause != NIL)
        context.reason = QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE;

    foreach(cell, query != NULL ? query->rtable : NIL)
    {
        RangeTblEntry *entry = lfirst_node(RangeTblEntry, cell);

        if (context.reason != QI_CLASSIFY_ELIGIBLE)
            break;
        if (entry->rtekind == RTE_RELATION)
        {
            char relkind = get_rel_relkind(entry->relid);

            if (relkind != RELKIND_RELATION &&
                relkind != RELKIND_PARTITIONED_TABLE)
                context.reason = QI_CLASSIFY_UNSUPPORTED_RELATION;
        }
        else if (entry->rtekind != RTE_JOIN
#if PG_VERSION_NUM >= 180000
                 && entry->rtekind != RTE_GROUP
#endif
                 )
            context.reason = QI_CLASSIFY_UNSUPPORTED_RELATION;
    }

    foreach(cell, query != NULL ? query->targetList : NIL)
    {
        TargetEntry *entry = lfirst_node(TargetEntry, cell);

        if (context.reason != QI_CLASSIFY_ELIGIBLE ||
            !qi_classify_expression((Node *) entry->expr, &context))
            break;
    }
    if (context.reason == QI_CLASSIFY_ELIGIBLE)
        (void) qi_classify_expression(query->havingQual, &context);
    if (context.reason == QI_CLASSIFY_ELIGIBLE)
        (void) qi_classify_from((Node *) query->jointree, &context);
    if (context.reason == QI_CLASSIFY_ELIGIBLE && query->limitOffset != NULL &&
        (!qi_integer_type(exprType(query->limitOffset)) ||
         !qi_classify_expression(query->limitOffset, &context)))
        context.reason = QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE;
    if (context.reason == QI_CLASSIFY_ELIGIBLE && query->limitCount != NULL &&
        (!qi_integer_type(exprType(query->limitCount)) ||
         !qi_classify_expression(query->limitCount, &context)))
        context.reason = QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE;

    return (QiClassifyResult) {
        .eligible = context.reason == QI_CLASSIFY_ELIGIBLE,
        .reason = context.reason
    };
}

const char *
qi_classify_reason_name(QiClassifyReason reason)
{
    static const char *const names[] = {
        "eligible", "not_select", "unsupported_query_shape",
        "unsupported_relation", "unsupported_join", "unsupported_node",
        "unsupported_type", "unsupported_operator", "unsupported_aggregate",
        "catalog_mismatch"
    };

    if (reason < QI_CLASSIFY_ELIGIBLE || reason > QI_CLASSIFY_CATALOG_MISMATCH)
        return "classifier_error";
    return names[reason];
}

#if PG_QI_TESTING
static bool qi_test_classifier_eligible = false;
static int qi_test_classifier_reason = QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE;

static const struct config_enum_entry qi_classifier_reason_options[] = {
    {"eligible", QI_CLASSIFY_ELIGIBLE, false},
    {"not_select", QI_CLASSIFY_NOT_SELECT, false},
    {"unsupported_query_shape", QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE, false},
    {"unsupported_relation", QI_CLASSIFY_UNSUPPORTED_RELATION, false},
    {"unsupported_join", QI_CLASSIFY_UNSUPPORTED_JOIN, false},
    {"unsupported_node", QI_CLASSIFY_UNSUPPORTED_NODE, false},
    {"unsupported_type", QI_CLASSIFY_UNSUPPORTED_TYPE, false},
    {"unsupported_operator", QI_CLASSIFY_UNSUPPORTED_OPERATOR, false},
    {"unsupported_aggregate", QI_CLASSIFY_UNSUPPORTED_AGGREGATE, false},
    {"catalog_mismatch", QI_CLASSIFY_CATALOG_MISMATCH, false},
    {NULL, 0, false}
};

void
qi_classify_init_test_gucs(void)
{
    DefineCustomBoolVariable("pg_qi.test_classifier_eligible", NULL, NULL,
                             &qi_test_classifier_eligible, false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.test_classifier_reason", NULL, NULL,
                             &qi_test_classifier_reason,
                             QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE,
                             qi_classifier_reason_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
}

void
qi_classify_record_test_result(QiClassifyResult result)
{
    qi_test_classifier_eligible = result.eligible;
    qi_test_classifier_reason = result.reason;
}

Datum
qi_test_classifier_contract(PG_FUNCTION_ARGS)
{
    Query unknown_query = {0};
    Query mismatch_query = {0};
    CaseExpr *unknown_expression = makeNode(CaseExpr);
    OpExpr *mismatch_expression = makeNode(OpExpr);
    QiClassifyResult unknown_result;
    QiClassifyResult mismatch_result;
    QiClassifyResult null_result;
    TupleDesc tuple_desc;
    Datum values[3];
    bool nulls[3] = {false, false, false};

    NodeSetTag(&unknown_query, T_Query);
    unknown_query.commandType = CMD_SELECT;
    unknown_query.jointree = makeFromExpr(NIL, NULL);
    unknown_query.targetList = list_make1(
        makeTargetEntry((Expr *) unknown_expression, 1, NULL, false));

    NodeSetTag(&mismatch_query, T_Query);
    mismatch_query.commandType = CMD_SELECT;
    mismatch_query.jointree = makeFromExpr(NIL, NULL);
    mismatch_expression->opno = InvalidOid;
    mismatch_query.targetList = list_make1(
        makeTargetEntry((Expr *) mismatch_expression, 1, NULL, false));

    unknown_result = qi_classify_query(&unknown_query);
    mismatch_result = qi_classify_query(&mismatch_query);
    null_result = qi_classify_query(NULL);
    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));
    values[0] = BoolGetDatum(!unknown_result.eligible &&
                             unknown_result.reason ==
                                 QI_CLASSIFY_UNSUPPORTED_NODE);
    values[1] = BoolGetDatum(!mismatch_result.eligible &&
                             mismatch_result.reason ==
                                 QI_CLASSIFY_CATALOG_MISMATCH);
    values[2] = BoolGetDatum(!null_result.eligible &&
                             null_result.reason == QI_CLASSIFY_NOT_SELECT);
    PG_RETURN_DATUM(HeapTupleGetDatum(
        heap_form_tuple(tuple_desc, values, nulls)));
}
#endif
