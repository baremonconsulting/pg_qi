/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_classify.h"
#include "pg_qi/qi_eligibility.h"
#include "utils/syscache.h"

typedef struct QiEligibilityContext
{
    QiEligibilityReason reason;
} QiEligibilityContext;

static bool
qi_shadow_expression_walker(Node *node, void *context_arg)
{
    QiEligibilityContext *context = context_arg;

    if (node == NULL)
        return false;

    switch (nodeTag(node))
    {
        case T_Query:
        case T_SubLink:
            context->reason = QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION;
            return true;
        case T_Param:
            context->reason = QI_ELIGIBILITY_UNSUPPORTED_PARAMETER;
            return true;
        case T_Aggref:
            context->reason = QI_ELIGIBILITY_UNSUPPORTED_AGGREGATE;
            return true;
        case T_FuncExpr:
            context->reason = QI_ELIGIBILITY_UNSUPPORTED_FUNCTION;
            return true;
        case T_RelabelType:
        case T_CoerceViaIO:
        case T_ArrayCoerceExpr:
        case T_ConvertRowtypeExpr:
        case T_CollateExpr:
            context->reason = QI_ELIGIBILITY_UNSUPPORTED_CAST;
            return true;
        case T_Var:
        {
            const Var *var = (const Var *) node;

            if (var->varlevelsup != 0 || var->varno <= 0 || var->varattno <= 0)
            {
                context->reason = QI_ELIGIBILITY_INVALID_VAR;
                return true;
            }
            return false;
        }
        default:
            break;
    }

    return expression_tree_walker(node, qi_shadow_expression_walker,
                                  context_arg);
}

static bool
qi_shadow_expression_allowed(Node *node, QiEligibilityContext *context)
{
    return !qi_shadow_expression_walker(node, context);
}

static bool
qi_relation_allowed(const RangeTblEntry *entry,
                    QiEligibilityContext *context)
{
    HeapTuple tuple;
    Form_pg_class relation;
    bool allowed = true;
    bool has_inheritance_children = false;

    if (entry->tablesample != NULL)
    {
        context->reason = QI_ELIGIBILITY_TABLE_SAMPLE;
        return false;
    }
    if (entry->securityQuals != NIL)
    {
        context->reason = QI_ELIGIBILITY_SECURITY_CONTEXT;
        return false;
    }

    tuple = SearchSysCache1(RELOID, ObjectIdGetDatum(entry->relid));
    if (!HeapTupleIsValid(tuple))
    {
        context->reason = QI_ELIGIBILITY_UNSUPPORTED_RELATION;
        return false;
    }
    relation = (Form_pg_class) GETSTRUCT(tuple);
    if (entry->inh && relation->relhassubclass)
    {
        List *children = find_inheritance_children(entry->relid, NoLock);

        has_inheritance_children = children != NIL;
        list_free(children);
    }
    if (relation->relkind != RELKIND_RELATION ||
        relation->relpersistence != RELPERSISTENCE_PERMANENT ||
        relation->relispartition)
    {
        context->reason = QI_ELIGIBILITY_UNSUPPORTED_RELATION;
        allowed = false;
    }
    else if (relation->relrowsecurity || relation->relforcerowsecurity)
    {
        context->reason = QI_ELIGIBILITY_SECURITY_CONTEXT;
        allowed = false;
    }
    else if (has_inheritance_children)
    {
        context->reason = QI_ELIGIBILITY_INHERITANCE;
        allowed = false;
    }
    ReleaseSysCache(tuple);
    return allowed;
}

QiEligibilityResult
qi_eligibility_query(const Query *query)
{
    QiClassifyResult prototype = {
        .eligible = false,
        .reason = QI_CLASSIFY_NOT_SELECT
    };
    QiEligibilityContext context = {QI_ELIGIBILITY_CANDIDATE};
    ListCell *cell;
    int relation_count = 0;

    if (query == NULL || query->commandType != CMD_SELECT)
    {
        prototype = qi_classify_query(query);
        return (QiEligibilityResult) {
            .candidate = false,
            .reason = QI_ELIGIBILITY_PROTOTYPE_REJECTED,
            .prototype_reason = prototype.reason
        };
    }

    if (query->setOperations != NULL || query->hasWindowFuncs ||
        query->hasTargetSRFs || query->hasSubLinks || query->cteList != NIL ||
        query->rowMarks != NIL || query->distinctClause != NIL)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION;
    else if (query->hasRowSecurity)
        context.reason = QI_ELIGIBILITY_SECURITY_CONTEXT;
    else if (query->hasAggs)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_AGGREGATE;
    else if (query->groupClause != NIL || query->groupingSets != NIL ||
             query->havingQual != NULL)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_GROUPING;
    else if (query->sortClause != NIL)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_ORDERING;
    else if (query->hasRecursive || query->hasModifyingCTE ||
             query->windowClause != NIL)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION;

    foreach(cell, query->rtable)
    {
        RangeTblEntry *entry = lfirst_node(RangeTblEntry, cell);

        if (context.reason != QI_ELIGIBILITY_CANDIDATE)
            break;
        if (entry->rtekind == RTE_RELATION)
        {
            relation_count++;
            if (!qi_relation_allowed(entry, &context))
                break;
        }
        else
        {
            context.reason = entry->rtekind == RTE_JOIN
                ? QI_ELIGIBILITY_UNSUPPORTED_JOIN
                : QI_ELIGIBILITY_UNSUPPORTED_RELATION;
            break;
        }
    }
    if (context.reason == QI_ELIGIBILITY_CANDIDATE && relation_count > 1)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_JOIN;

    foreach(cell, query->targetList)
    {
        TargetEntry *entry = lfirst_node(TargetEntry, cell);

        if (context.reason != QI_ELIGIBILITY_CANDIDATE)
            break;
        if (entry->resjunk)
        {
            context.reason = QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION;
            break;
        }
        if (!qi_shadow_expression_allowed((Node *) entry->expr, &context))
            break;
    }

    if (context.reason == QI_ELIGIBILITY_CANDIDATE &&
        query->jointree == NULL)
        context.reason = QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION;
    if (context.reason == QI_ELIGIBILITY_CANDIDATE)
        (void) qi_shadow_expression_allowed((Node *) query->jointree->quals,
                                            &context);
    if (context.reason == QI_ELIGIBILITY_CANDIDATE)
        (void) qi_shadow_expression_allowed(query->limitOffset, &context);
    if (context.reason == QI_ELIGIBILITY_CANDIDATE)
        (void) qi_shadow_expression_allowed(query->limitCount, &context);

    if (context.reason == QI_ELIGIBILITY_CANDIDATE)
    {
        prototype = qi_classify_query(query);
        if (!prototype.eligible)
            context.reason = QI_ELIGIBILITY_PROTOTYPE_REJECTED;
    }

    return (QiEligibilityResult) {
        .candidate = context.reason == QI_ELIGIBILITY_CANDIDATE,
        .reason = context.reason,
        .prototype_reason = prototype.reason
    };
}

const char *
qi_eligibility_reason_name(QiEligibilityReason reason)
{
    static const char *const names[] = {
        "candidate", "prototype_rejected", "security_context",
        "table_sample", "inheritance", "unsupported_relation",
        "unsupported_join", "unsupported_aggregate",
        "unsupported_grouping", "unsupported_ordering",
        "unsupported_parameter", "unsupported_function", "unsupported_cast",
        "unsupported_expression", "invalid_var"
    };

    if (reason < QI_ELIGIBILITY_CANDIDATE ||
        reason > QI_ELIGIBILITY_INVALID_VAR)
        return "classifier_error";
    return names[reason];
}
