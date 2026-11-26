/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>
#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/pg_aggregate.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_proc_d.h"
#include "catalog/pg_type_d.h"
#include "common/cryptohash.h"
#include "executor/executor.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_bound.h"
#include "utils/acl.h"
#include "utils/datum.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/syscache.h"
#include "utils/timestamp.h"

#define QI_BOUND_MAX_RULES 64
#define QI_BOUND_MAX_PARAMETERS 64
#define QI_BOUND_MAX_SOURCE_BYTES 65536
#define QI_BOUND_MAX_NODES 4096
#define QI_BOUND_MAX_DEPTH 64
#define QI_BOUND_MAX_RELATIONS 64

typedef struct QiBoundRule
{
    char key[QI_BOUND_KEY_LENGTH + 1];
    uint32 postgres_ms;
    uint32 duckdb_ms;
    uint32 expires_at;
} QiBoundRule;

typedef struct QiBoundPolicy
{
    int count;
    QiBoundRule rules[QI_BOUND_MAX_RULES];
} QiBoundPolicy;

typedef struct QiBoundRelation
{
    Oid oid;
    Oid file;
    int32 pages;
    float4 tuples;
} QiBoundRelation;

typedef struct QiBoundWalk
{
    ParamListInfo params;
    int nodes;
    int depth;
    int references;
    int relation_count;
    bool analytical;
    QiBoundRelation relations[QI_BOUND_MAX_RELATIONS];
} QiBoundWalk;

static char *qi_bound_policy_text = NULL;
static const QiBoundPolicy *qi_bound_policy = NULL;
static int qi_bound_min_saving_ms = 5000;
static int qi_bound_min_saving_percent = 10;

static bool qi_bound_walk(Node *node, void *context);

static bool
qi_bound_uint(const char **cursor, uint32 *result)
{
    const char *start = *cursor;
    uint64 value = 0;

    while (**cursor >= '0' && **cursor <= '9')
    {
        value = value * 10 + (**cursor - '0');
        if (value > PG_UINT32_MAX)
            return false;
        ++*cursor;
    }
    *result = (uint32) value;
    return *cursor != start;
}

static bool
qi_bound_check_policy(char **value, void **extra, GucSource source)
{
    QiBoundPolicy parsed = {0};
    const char *cursor = *value;
    Size size;

    if (cursor == NULL || *cursor == '\0')
        return true;
    for (;;)
    {
        QiBoundRule *rule;
        Size length;
        int index;

        if (parsed.count == QI_BOUND_MAX_RULES)
            return false;
        rule = &parsed.rules[parsed.count];
        length = strspn(cursor, "0123456789abcdef");
        if (length != QI_BOUND_KEY_LENGTH || cursor[length] != ':')
            return false;
        memcpy(rule->key, cursor, QI_BOUND_KEY_LENGTH);
        cursor += length + 1;
        if (!qi_bound_uint(&cursor, &rule->postgres_ms) || *cursor++ != ':' ||
            !qi_bound_uint(&cursor, &rule->duckdb_ms) || *cursor++ != ':' ||
            !qi_bound_uint(&cursor, &rule->expires_at) ||
            rule->postgres_ms == 0 || rule->duckdb_ms == 0 || rule->expires_at == 0)
            return false;
        for (index = 0; index < parsed.count; index++)
            if (strcmp(rule->key, parsed.rules[index].key) == 0)
                return false;
        parsed.count++;
        if (*cursor == '\0')
            break;
        if (*cursor++ != ',')
            return false;
    }

    size = offsetof(QiBoundPolicy, rules) + parsed.count * sizeof(QiBoundRule);
    *extra = guc_malloc(LOG, size);
    if (*extra == NULL)
        return false;
    memcpy(*extra, &parsed, size);
    return true;
}

static void
qi_bound_assign_policy(const char *value, void *extra)
{
    qi_bound_policy = extra;
}

void
qi_bound_init_gucs(void)
{
    DefineCustomStringVariable("pg_qi.bound_policy",
                               "Reviewed rules for delegating queries with bound values.",
                               NULL, &qi_bound_policy_text, "", PGC_SUSET,
                               GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
                               qi_bound_check_policy, qi_bound_assign_policy, NULL);
    DefineCustomIntVariable("pg_qi.bound_min_saving_ms",
                            "Minimum measured saving for a bound routing rule in milliseconds.",
                            NULL, &qi_bound_min_saving_ms, 5000, 0, INT_MAX,
                            PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.bound_min_saving_percent",
                            "Minimum measured percentage saving for a bound routing rule.",
                            NULL, &qi_bound_min_saving_percent, 10, 0, 100,
                            PGC_SUSET, 0, NULL, NULL, NULL);
}

static bool
qi_bound_scalar(Oid type)
{
    return type == INT2OID || type == INT4OID || type == INT8OID ||
        type == BOOLOID;
}

static bool
qi_bound_builtin(Oid function)
{
    return OidIsValid(function) && function < FirstNormalObjectId &&
        get_func_namespace(function) == PG_CATALOG_NAMESPACE &&
        func_volatile(function) == PROVOLATILE_IMMUTABLE;
}

static bool
qi_bound_relation(const RangeTblEntry *entry, QiBoundWalk *walk)
{
    HeapTuple tuple;
    Form_pg_class relation;
    bool supported;

    if (!entry->inh || entry->tablesample != NULL ||
        walk->relation_count == QI_BOUND_MAX_RELATIONS)
        return false;
    tuple = SearchSysCache1(RELOID, ObjectIdGetDatum(entry->relid));
    if (!HeapTupleIsValid(tuple))
        return false;

    relation = (Form_pg_class) GETSTRUCT(tuple);
    supported = relation->relkind == RELKIND_RELATION &&
        relation->relpersistence == RELPERSISTENCE_PERMANENT &&
        relation->relam == HEAP_TABLE_AM_OID &&
        !relation->relrowsecurity && !relation->relforcerowsecurity &&
        !relation->relispartition && !relation->relhassubclass;
    if (supported)
    {
        QiBoundRelation *observed = &walk->relations[walk->relation_count++];

        observed->oid = entry->relid;
        observed->file = relation->relfilenode;
        observed->pages = relation->relpages;
        observed->tuples = relation->reltuples;
    }
    ReleaseSysCache(tuple);
    return supported;
}

static bool
qi_bound_query_walk(Query *query, QiBoundWalk *walk)
{
    ListCell *cell;

    if (query->commandType != CMD_SELECT || query->utilityStmt != NULL ||
        query->resultRelation != 0 || query->hasModifyingCTE ||
        query->hasRecursive || query->hasRowSecurity || query->hasTargetSRFs ||
        query->rowMarks != NIL || query->setOperations != NULL ||
        query->groupingSets != NIL || query->hasForUpdate)
        return true;

    foreach(cell, query->rteperminfos)
    {
        const RTEPermissionInfo *permissions = lfirst_node(RTEPermissionInfo, cell);

        if (OidIsValid(permissions->checkAsUser) ||
            (permissions->requiredPerms & ~ACL_SELECT) != 0)
            return true;
    }
    if (!ExecCheckPermissions(query->rtable, query->rteperminfos, false))
        return true;

    walk->analytical |= query->hasAggs || query->hasWindowFuncs ||
        query->groupClause != NIL || query->distinctClause != NIL;
    return query_tree_walker(query, qi_bound_walk, walk,
                             QTW_EXAMINE_RTES_BEFORE | QTW_EXAMINE_SORTGROUP);
}

static bool
qi_bound_walk_node(Node *node, QiBoundWalk *walk)
{
    Oid type;

    switch (nodeTag(node))
    {
        case T_Query:
            return qi_bound_query_walk((Query *) node, walk);
        case T_RangeTblEntry:
        {
            const RangeTblEntry *entry = (const RangeTblEntry *) node;

            if (entry->security_barrier || entry->securityQuals != NIL)
                return true;
            if (entry->rtekind == RTE_RELATION)
                return !qi_bound_relation(entry, walk);
#if PG_VERSION_NUM >= 180000
            if (entry->rtekind == RTE_GROUP)
                return false;
#endif
            return entry->rtekind != RTE_SUBQUERY && entry->rtekind != RTE_CTE &&
                entry->rtekind != RTE_JOIN && entry->rtekind != RTE_RESULT;
        }
        case T_Var:
        {
            const Var *var = (const Var *) node;

            return var->varattno <= 0 || !qi_bound_scalar(var->vartype) ||
                OidIsValid(var->varcollid);
        }
        case T_Const:
        {
            const Const *constant = (const Const *) node;

            return !qi_bound_scalar(constant->consttype) ||
                OidIsValid(constant->constcollid);
        }
        case T_Param:
        {
            const Param *param = (const Param *) node;

            if (param->paramkind != PARAM_EXTERN || param->paramid <= 0 ||
                param->paramid > walk->params->numParams ||
                param->paramtype != walk->params->params[param->paramid - 1].ptype ||
                OidIsValid(param->paramcollid))
                return true;
            walk->references++;
            return false;
        }
        case T_Aggref:
        {
            const Aggref *aggregate = (const Aggref *) node;

            if (!qi_bound_builtin(aggregate->aggfnoid) ||
                aggregate->aggkind != AGGKIND_NORMAL ||
                OidIsValid(aggregate->inputcollid))
                return true;
            type = aggregate->aggtype;
            break;
        }
        case T_WindowFunc:
        {
            const WindowFunc *window = (const WindowFunc *) node;

            if (!qi_bound_builtin(window->winfnoid) ||
                OidIsValid(window->inputcollid))
                return true;
            type = window->wintype;
            break;
        }
        case T_OpExpr:
        case T_DistinctExpr:
        case T_NullIfExpr:
        {
            const OpExpr *op = (const OpExpr *) node;

            if (op->opno >= FirstNormalObjectId ||
                !qi_bound_builtin(get_opcode(op->opno)) || op->opretset ||
                OidIsValid(op->inputcollid))
                return true;
            type = op->opresulttype;
            break;
        }
        case T_FuncExpr:
        {
            const FuncExpr *function = (const FuncExpr *) node;

            if (!qi_bound_builtin(function->funcid) || function->funcretset ||
                OidIsValid(function->inputcollid))
                return true;
            type = function->funcresulttype;
            break;
        }
        case T_SortGroupClause:
        {
            const SortGroupClause *sort = (const SortGroupClause *) node;

            return sort->eqop >= FirstNormalObjectId ||
                !qi_bound_builtin(get_opcode(sort->eqop)) ||
                (OidIsValid(sort->sortop) &&
                 (sort->sortop >= FirstNormalObjectId ||
                  !qi_bound_builtin(get_opcode(sort->sortop))));
        }
        case T_JoinExpr:
        {
            const JoinExpr *join = (const JoinExpr *) node;

            return join->jointype != JOIN_INNER ||
                expression_tree_walker(node, qi_bound_walk, walk);
        }
        case T_WindowClause:
        {
            const WindowClause *window = (const WindowClause *) node;

            return (OidIsValid(window->startInRangeFunc) &&
                    !qi_bound_builtin(window->startInRangeFunc)) ||
                (OidIsValid(window->endInRangeFunc) &&
                 !qi_bound_builtin(window->endInRangeFunc)) ||
                expression_tree_walker(node, qi_bound_walk, walk);
        }
        case T_List:
        case T_TargetEntry:
        case T_FromExpr:
        case T_RangeTblRef:
        case T_CommonTableExpr:
        case T_NullTest:
        case T_BooleanTest:
        case T_BoolExpr:
            return expression_tree_walker(node, qi_bound_walk, walk);
        case T_CaseExpr:
        case T_CaseTestExpr:
        case T_CoalesceExpr:
        case T_RelabelType:
            type = exprType(node);
            break;
        case T_CaseWhen:
            return expression_tree_walker(node, qi_bound_walk, walk);
        default:
            return true;
    }
    return !qi_bound_scalar(type) || OidIsValid(exprCollation(node)) ||
        expression_tree_walker(node, qi_bound_walk, walk);
}

static bool
qi_bound_walk(Node *node, void *context)
{
    QiBoundWalk *walk = context;
    bool unsupported;

    if (node == NULL)
        return false;
    if (++walk->nodes > QI_BOUND_MAX_NODES || walk->depth == QI_BOUND_MAX_DEPTH)
        return true;
    walk->depth++;
    unsupported = qi_bound_walk_node(node, walk);
    walk->depth--;
    return unsupported;
}

static Node *
qi_bound_materialise(Node *node, void *context)
{
    ParamListInfo params = context;
    const Param *parameter;
    const ParamExternData *value;
    int16 length;
    bool by_value;
    Datum datum;

    if (node == NULL)
        return NULL;
    if (IsA(node, Query))
        return (Node *) query_tree_mutator((Query *) node, qi_bound_materialise,
                                          context, 0);
    if (!IsA(node, Param))
        return expression_tree_mutator(node, qi_bound_materialise, context);

    parameter = (const Param *) node;
    value = &params->params[parameter->paramid - 1];
    get_typlenbyval(parameter->paramtype, &length, &by_value);
    datum = value->isnull ? (Datum) 0 : datumCopy(value->value, by_value, length);
    return (Node *) makeConst(parameter->paramtype, parameter->paramtypmod,
                              parameter->paramcollid, length, datum,
                              value->isnull, by_value);
}

static bool
qi_bound_hash_values(pg_cryptohash_ctx *hash, ParamListInfo params)
{
    int index;

    for (index = 0; index < params->numParams; index++)
    {
        const ParamExternData *param = &params->params[index];
        int64 value = 0;

        if (!param->isnull)
        {
            switch (param->ptype)
            {
                case INT2OID: value = DatumGetInt16(param->value); break;
                case INT4OID: value = DatumGetInt32(param->value); break;
                case INT8OID: value = DatumGetInt64(param->value); break;
                case BOOLOID: value = DatumGetBool(param->value); break;
                default: return false;
            }
        }
        if (pg_cryptohash_update(hash, (const uint8 *) &param->ptype, sizeof(param->ptype)) != 0 ||
            pg_cryptohash_update(hash, (const uint8 *) &param->isnull, sizeof(param->isnull)) != 0 ||
            pg_cryptohash_update(hash, (const uint8 *) &value, sizeof(value)) != 0)
            return false;
    }
    return true;
}

static bool
qi_bound_make_key(QiBoundAssessment *assessment, Query *query,
                  const char *source, const QiBoundWalk *walk)
{
    static const char *const settings[] = {
        "work_mem", "hash_mem_multiplier", "max_parallel_workers_per_gather",
        "jit", "enable_hashagg", "enable_presorted_aggregate",
        "seq_page_cost", "random_page_cost", "cpu_tuple_cost", "cpu_operator_cost",
        "duckdb.memory_limit", "duckdb.threads",
        "duckdb.max_workers_per_postgres_scan", "duckdb.threads_for_postgres_scan"
    };
    static const char key_version[] = "pg_qi bound v1";
    static const char hex[] = "0123456789abcdef";
    Oid identity[] = {MyDatabaseId, GetSessionUserId(), GetUserId()};
    uint8 digest[QI_BOUND_KEY_LENGTH / 2];
    char *tree = nodeToString(query);
    pg_cryptohash_ctx *hash = pg_cryptohash_create(PG_SHA256);
    bool success = hash != NULL && pg_cryptohash_init(hash) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) key_version, sizeof(key_version)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) identity, sizeof(identity)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &PgStartTime, sizeof(PgStartTime)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &XactIsoLevel, sizeof(XactIsoLevel)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &XactReadOnly, sizeof(XactReadOnly)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) source, strlen(source) + 1) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) tree, strlen(tree) + 1) == 0 &&
        qi_bound_hash_values(hash, walk->params) &&
        pg_cryptohash_update(hash, (const uint8 *) walk->relations,
                             walk->relation_count * sizeof(QiBoundRelation)) == 0;
    Size index;

    pfree(tree);
    for (index = 0; success && index < lengthof(settings); index++)
    {
        const char *setting = GetConfigOption(settings[index], true, false);

        success = setting != NULL &&
            pg_cryptohash_update(hash, (const uint8 *) setting, strlen(setting) + 1) == 0;
    }
    success = success && pg_cryptohash_final(hash, digest, sizeof(digest)) == 0;
    pg_cryptohash_free(hash);
    if (!success)
        return false;

    for (index = 0; index < sizeof(digest); index++)
    {
        assessment->key[2 * index] = hex[digest[index] >> 4];
        assessment->key[2 * index + 1] = hex[digest[index] & 15];
    }
    return true;
}

static void
qi_bound_apply_policy(QiBoundAssessment *assessment)
{
    int index;

    if (qi_bound_policy == NULL)
        return;
    for (index = 0; index < qi_bound_policy->count; index++)
    {
        const QiBoundRule *rule = &qi_bound_policy->rules[index];
        uint32 saving;

        if (strcmp(rule->key, assessment->key) != 0)
            continue;
        if ((int64) timestamptz_to_time_t(GetCurrentTimestamp()) >= rule->expires_at)
            return;
        assessment->semantically_safe = QI_GATE_PASS;
        saving = rule->postgres_ms > rule->duckdb_ms
            ? rule->postgres_ms - rule->duckdb_ms : 0;
        if (saving == 0 || saving < (uint32) qi_bound_min_saving_ms ||
            (uint64) saving * 100 < (uint64) rule->postgres_ms * qi_bound_min_saving_percent)
            assessment->beneficial = QI_GATE_VETO;
        else
            assessment->beneficial = QI_GATE_PASS;
        return;
    }
}

QiBoundAssessment
qi_bound_assess(Query *query, const char *source, ParamListInfo params)
{
    QiBoundAssessment assessment = {0};
    QiBoundWalk walk = {.params = params};
    int index;

    if (params == NULL || params->numParams <= 0)
        return assessment;

    assessment.eligible = QI_GATE_VETO;
    if (params->numParams > QI_BOUND_MAX_PARAMETERS || params->paramFetch != NULL ||
        source == NULL || strnlen(source, QI_BOUND_MAX_SOURCE_BYTES + 1) >
        QI_BOUND_MAX_SOURCE_BYTES)
        return assessment;

    for (index = 0; index < params->numParams; index++)
        if (!qi_bound_scalar(params->params[index].ptype) ||
            !(params->params[index].pflags & PARAM_FLAG_CONST))
            return assessment;

    if (query == NULL || qi_bound_walk((Node *) query, &walk) ||
        !walk.analytical || walk.references == 0 || walk.relation_count == 0)
        return assessment;

    assessment.eligible = QI_GATE_PASS;
    assessment.semantically_safe = QI_GATE_UNKNOWN;
    assessment.beneficial = QI_GATE_UNKNOWN;
    if (qi_bound_make_key(&assessment, query, source, &walk))
        qi_bound_apply_policy(&assessment);
    /* Keep the prepared statement reusable with the next binding. */
    if (assessment.beneficial == QI_GATE_PASS)
        assessment.provider_query = (Query *) qi_bound_materialise((Node *) query, params);
    return assessment;
}
