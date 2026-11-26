/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>
#include "access/htup_details.h"
#include "access/tupmacs.h"
#include "access/xact.h"
#include "catalog/objectaccess.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_collation_d.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_proc_d.h"
#include "catalog/pg_type_d.h"
#include "common/cryptohash.h"
#include "executor/executor.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_bound.h"
#include "utils/acl.h"
#include "utils/datum.h"
#include "utils/guc.h"
#include "utils/guc_tables.h"
#include "utils/json.h"
#include "utils/jsonb.h"
#if PG_VERSION_NUM >= 170000
#include "utils/jsonpath.h"
#endif
#include "utils/lsyscache.h"
#include "utils/pg_locale.h"
#include "utils/syscache.h"
#include "utils/timestamp.h"

#define QI_BOUND_MAX_RULES 64
#define QI_BOUND_MAX_PARAMETERS 64
#define QI_BOUND_MAX_SOURCE_BYTES 65536
#define QI_BOUND_MAX_VALUE_BYTES 65536
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

typedef struct QiBoundParameter
{
    uint32 bytes;
    int16 length;
    bool by_value;
} QiBoundParameter;

typedef struct QiBoundWalk
{
    ParamListInfo params;
    Oid provider_role;
    bool in_transaction_block;
    int nodes;
    int depth;
    int relation_count;
    Size copied_bytes;
    QiBoundRelation relations[QI_BOUND_MAX_RELATIONS];
    QiBoundParameter parameters[QI_BOUND_MAX_PARAMETERS];
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
qi_bound_safe_type(Oid type)
{
    return OidIsValid(type) && type < FirstNormalObjectId;
}

static Oid
qi_bound_provider_role(void)
{
    const char *name = GetConfigOption("duckdb.postgres_role", true, false);

    if (name == NULL)
        return InvalidOid;
    return name[0] == '\0' ? BOOTSTRAP_SUPERUSERID : get_role_oid(name, true);
}

static bool
qi_bound_unsafe_function(Oid function, void *context)
{
    return !OidIsValid(function) || function >= FirstNormalObjectId ||
        get_func_namespace(function) != PG_CATALOG_NAMESPACE ||
        func_volatile(function) != PROVOLATILE_IMMUTABLE ||
        object_aclcheck(ProcedureRelationId, function, GetUserId(), ACL_EXECUTE) != ACLCHECK_OK;
}

static bool
qi_bound_safe_operator(Oid op)
{
    return OidIsValid(op) && op < FirstNormalObjectId &&
        !qi_bound_unsafe_function(get_opcode(op), NULL);
}

static bool
qi_bound_safe_collation(Oid collation)
{
#if PG_VERSION_NUM >= 180000
    pg_locale_t locale;
#endif

    if (!OidIsValid(collation) || collation == C_COLLATION_OID)
        return true;
    /* Inspect the loaded database default without opening an arbitrary locale. */
    if (collation != DEFAULT_COLLATION_OID)
        return false;
#if PG_VERSION_NUM >= 180000
    locale = pg_newlocale_from_collation(collation);
    return locale->collate_is_c && locale->ctype_is_c;
#else
    return lc_collate_is_c(collation) && lc_ctype_is_c(collation);
#endif
}

static bool
qi_bound_parameter_sizes(QiBoundWalk *walk)
{
    Size total = 0;
    int index;

    for (index = 0; index < walk->params->numParams; index++)
    {
        const ParamExternData *value = &walk->params->params[index];
        QiBoundParameter *parameter = &walk->parameters[index];
        Size bytes = 0;

        if (!qi_bound_safe_type(value->ptype) ||
            get_typtype(value->ptype) != TYPTYPE_BASE ||
            !(value->pflags & PARAM_FLAG_CONST))
            return false;
        get_typlenbyval(value->ptype, &parameter->length, &parameter->by_value);
        if (parameter->length == 0 || parameter->length < -1)
            return false;
        if (!value->isnull)
        {
            if (parameter->length == -1)
            {
                const struct varlena *datum = (const struct varlena *) DatumGetPointer(value->value);

                /* Protocol values must carry their data in the datum. */
                if (VARATT_IS_EXTERNAL(datum) || VARATT_IS_COMPRESSED(datum))
                    return false;
                bytes = VARSIZE_ANY_EXHDR(datum);
            }
            else
                bytes = parameter->length;
        }
        if (bytes > QI_BOUND_MAX_VALUE_BYTES - total)
            return false;
        parameter->bytes = (uint32) bytes;
        total += bytes;
    }
    return true;
}

static bool
qi_bound_relation(const RangeTblEntry *entry, QiBoundWalk *walk)
{
    HeapTuple tuple;
    Form_pg_class relation;
    bool supported;

    if (entry->tablesample != NULL ||
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
        query->hasRowSecurity || query->rowMarks != NIL || query->hasForUpdate)
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

    return query_tree_walker(query, qi_bound_walk, walk,
                             QTW_EXAMINE_RTES_BEFORE | QTW_EXAMINE_SORTGROUP);
}

static bool
qi_bound_unsafe_sort(const SortGroupClause *sort)
{
    return !qi_bound_safe_operator(sort->eqop) ||
        (OidIsValid(sort->sortop) && !qi_bound_safe_operator(sort->sortop));
}

static bool
qi_bound_unsafe_json_constructor(const JsonConstructorExpr *constructor,
                                 QiBoundWalk *walk)
{
    bool as_jsonb = constructor->returning->format->format_type == JS_FORMAT_JSONB;
    ListCell *cell;

    if (!qi_bound_safe_type(constructor->returning->typid))
        return true;
#if PG_VERSION_NUM >= 190000
    /* The provider deparses this subtree, which the core walker skips. */
    if (qi_bound_walk(constructor->orig_query, walk))
        return true;
#endif
    foreach(cell, constructor->args)
    {
        Oid type = exprType(lfirst(cell));

        if (!qi_bound_safe_type(type) ||
            (as_jsonb ? !to_jsonb_is_immutable(type) : !to_json_is_immutable(type)))
            return true;
    }
    return false;
}

#if PG_VERSION_NUM >= 170000
static bool
qi_bound_unsafe_json_path(const JsonExpr *json)
{
    const Const *path;

    if (!qi_bound_safe_type(json->returning->typid) || json->path_spec == NULL ||
        !IsA(json->path_spec, Const))
        return true;
    if (json->use_io_coercion)
    {
        Oid input_function;
        Oid input_parameter;

        getTypeInputInfo(json->returning->typid, &input_function, &input_parameter);
        if (qi_bound_unsafe_function(input_function, NULL))
            return true;
    }
    path = (const Const *) json->path_spec;
    return !path->constisnull &&
        jspIsMutable(DatumGetJsonPathP(path->constvalue),
                     json->passing_names, json->passing_values);
}
#endif

/*
 * Check effects and permissions here. PostgreSQL walks the other expression
 * nodes, and the provider decides whether it can plan the resulting query.
 */
static bool
qi_bound_walk_node(Node *node, QiBoundWalk *walk)
{
    if (!qi_bound_safe_collation(exprInputCollation(node)))
        return true;

    switch (nodeTag(node))
    {
        case T_Query:
            return qi_bound_query_walk((Query *) node, walk);
        case T_RangeTblEntry:
        {
            const RangeTblEntry *entry = (const RangeTblEntry *) node;

            return entry->security_barrier || entry->securityQuals != NIL ||
                entry->rtekind == RTE_TABLEFUNC || entry->rtekind == RTE_NAMEDTUPLESTORE ||
                (entry->rtekind == RTE_RELATION && !qi_bound_relation(entry, walk));
        }
        case T_Var:
        {
            const Var *var = (const Var *) node;

            return var->varattno <= 0 || !qi_bound_safe_type(var->vartype) ||
                !qi_bound_safe_collation(var->varcollid);
        }
        case T_Const:
        {
            const Const *constant = (const Const *) node;

            return !qi_bound_safe_type(constant->consttype) ||
                !qi_bound_safe_collation(constant->constcollid);
        }
        case T_Param:
        {
            const Param *param = (const Param *) node;
            Size bytes;

            if (!qi_bound_safe_type(param->paramtype) ||
                !qi_bound_safe_collation(param->paramcollid))
                return true;
            /* The analyser uses these slots for subquery output columns. */
            if (param->paramkind == PARAM_SUBLINK)
                return false;
            if (param->paramkind != PARAM_EXTERN || param->paramid <= 0 ||
                param->paramid > walk->params->numParams ||
                param->paramtype != walk->params->params[param->paramid - 1].ptype)
                return true;

            bytes = walk->parameters[param->paramid - 1].bytes + VARHDRSZ;
            if (bytes > QI_BOUND_MAX_VALUE_BYTES - walk->copied_bytes)
                return true;
            walk->copied_bytes += bytes;
            return false;
        }
        case T_TargetEntry:
            if (!qi_bound_safe_type(exprType((Node *) ((TargetEntry *) node)->expr)) ||
                !qi_bound_safe_collation(exprCollation((Node *) ((TargetEntry *) node)->expr)))
                return true;
            break;
        case T_CoerceToDomain:
        case T_CurrentOfExpr:
        case T_NextValueExpr:
        case T_SQLValueFunction:
        case T_XmlExpr:
            return true;
        case T_OpExpr:
        case T_DistinctExpr:
        case T_NullIfExpr:
            /* Resolve the function without filling in the original node. */
            return !qi_bound_safe_operator(((OpExpr *) node)->opno) ||
                expression_tree_walker(node, qi_bound_walk, walk);
        case T_ScalarArrayOpExpr:
            return !qi_bound_safe_operator(((ScalarArrayOpExpr *) node)->opno) ||
                expression_tree_walker(node, qi_bound_walk, walk);
        case T_RowCompareExpr:
        {
            const RowCompareExpr *comparison = (const RowCompareExpr *) node;
            ListCell *cell;

            foreach(cell, comparison->opnos)
                if (!qi_bound_safe_operator(lfirst_oid(cell)))
                    return true;
            foreach(cell, comparison->opfamilies)
                if (lfirst_oid(cell) >= FirstNormalObjectId)
                    return true;
            foreach(cell, comparison->inputcollids)
                if (!qi_bound_safe_collation(lfirst_oid(cell)))
                    return true;
            break;
        }
        case T_SortGroupClause:
            return qi_bound_unsafe_sort((SortGroupClause *) node);
        case T_SetOperationStmt:
        {
            const SetOperationStmt *set = (const SetOperationStmt *) node;
            ListCell *cell;

            /* The core walker visits the two inputs, but not their grouping clauses. */
            foreach(cell, set->groupClauses)
                if (qi_bound_unsafe_sort(lfirst_node(SortGroupClause, cell)))
                    return true;
            break;
        }
        case T_WindowClause:
        {
            const WindowClause *window = (const WindowClause *) node;

            if (!qi_bound_safe_collation(window->inRangeColl) ||
                (OidIsValid(window->startInRangeFunc) &&
                 qi_bound_unsafe_function(window->startInRangeFunc, NULL)) ||
                (OidIsValid(window->endInRangeFunc) &&
                 qi_bound_unsafe_function(window->endInRangeFunc, NULL)))
                return true;
            break;
        }
        case T_SubscriptingRef:
            if (!qi_bound_safe_type(((SubscriptingRef *) node)->refcontainertype))
                return true;
            break;
        case T_JsonConstructorExpr:
            if (qi_bound_unsafe_json_constructor((JsonConstructorExpr *) node, walk))
                return true;
            break;
#if PG_VERSION_NUM >= 170000
        case T_JsonExpr:
            if (qi_bound_unsafe_json_path((JsonExpr *) node))
                return true;
            break;
#endif
        case T_CoerceViaIO:
        case T_ArrayCoerceExpr:
        case T_ArrayExpr:
        case T_ConvertRowtypeExpr:
        case T_MinMaxExpr:
        case T_RelabelType:
        case T_RowExpr:
            if (!qi_bound_safe_type(exprType(node)))
                return true;
            break;
        default:
            break;
    }
    return check_functions_in_node(node, qi_bound_unsafe_function, NULL) ||
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
qi_bound_replace_parameters(Node *node, void *context)
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
        return (Node *) query_tree_mutator((Query *) node, qi_bound_replace_parameters,
                                          context, 0);
#if PG_VERSION_NUM >= 190000
    if (IsA(node, JsonConstructorExpr))
    {
        JsonConstructorExpr *constructor = (JsonConstructorExpr *)
            expression_tree_mutator(node, qi_bound_replace_parameters, context);

        constructor->orig_query =
            qi_bound_replace_parameters(constructor->orig_query, context);
        return (Node *) constructor;
    }
#endif
    if (!IsA(node, Param) || ((Param *) node)->paramkind != PARAM_EXTERN)
        return expression_tree_mutator(node, qi_bound_replace_parameters, context);

    parameter = (const Param *) node;
    Assert(parameter->paramid > 0 && parameter->paramid <= params->numParams);
    value = &params->params[parameter->paramid - 1];
    get_typlenbyval(parameter->paramtype, &length, &by_value);
    datum = value->isnull ? (Datum) 0 : datumCopy(value->value, by_value, length);
    return (Node *) makeConst(parameter->paramtype, parameter->paramtypmod,
                              parameter->paramcollid, length, datum,
                              value->isnull, by_value);
}

Query *
qi_bound_materialise(Query *query, ParamListInfo params)
{
    return (Query *) qi_bound_replace_parameters((Node *) copyObject(query), params);
}

static bool
qi_bound_hash_values(pg_cryptohash_ctx *hash, const QiBoundWalk *walk)
{
    ParamListInfo params = walk->params;
    int index;

    if (pg_cryptohash_update(hash, (const uint8 *) &params->numParams,
                             sizeof(params->numParams)) != 0)
        return false;
    for (index = 0; index < params->numParams; index++)
    {
        const ParamExternData *param = &params->params[index];
        const QiBoundParameter *parameter = &walk->parameters[index];
        const unsigned char *bytes = NULL;
        Datum by_value = 0;

        if (!param->isnull)
        {
            if (parameter->by_value)
            {
                store_att_byval(&by_value, param->value, parameter->length);
                bytes = (const unsigned char *) &by_value;
            }
            else if (parameter->length == -1)
                bytes = (const unsigned char *) VARDATA_ANY(DatumGetPointer(param->value));
            else
                bytes = (const unsigned char *) DatumGetPointer(param->value);
        }
        if (pg_cryptohash_update(hash, (const uint8 *) &param->ptype, sizeof(param->ptype)) != 0 ||
            pg_cryptohash_update(hash, (const uint8 *) &param->isnull, sizeof(param->isnull)) != 0 ||
            pg_cryptohash_update(hash, (const uint8 *) &parameter->bytes, sizeof(parameter->bytes)) != 0 ||
            (parameter->bytes != 0 && pg_cryptohash_update(hash, bytes, parameter->bytes) != 0))
            return false;
    }
    return true;
}

static bool
qi_bound_hash_setting(pg_cryptohash_ctx *hash, const char *name)
{
    const char *value = GetConfigOption(name, true, false);
    const bool present = value != NULL;

    return pg_cryptohash_update(hash, (const uint8 *) name, strlen(name) + 1) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &present, sizeof(present)) == 0 &&
        (!present ||
         pg_cryptohash_update(hash, (const uint8 *) value, strlen(value) + 1) == 0);
}

static int
qi_bound_setting_name_cmp(const void *left, const void *right)
{
    const struct config_generic *const *first = left;
    const struct config_generic *const *second = right;

    return strcmp((*first)->name, (*second)->name);
}

static bool
qi_bound_hash_planner_settings(pg_cryptohash_ctx *hash)
{
    int count;
    struct config_generic **settings = get_explain_guc_options(&count);
    bool success = pg_cryptohash_update(hash, (const uint8 *) &count, sizeof(count)) == 0;
    int index;

    qsort(settings, count, sizeof(*settings), qi_bound_setting_name_cmp);
    for (index = 0; success && index < count; index++)
        success = qi_bound_hash_setting(hash, settings[index]->name);
    pfree(settings);
    return success;
}

static bool
qi_bound_make_key(QiBoundAssessment *assessment, Query *query,
                  const char *source, const QiBoundWalk *walk)
{
    static const char *const settings[] = {
        "max_parallel_workers", "effective_io_concurrency",
        "TimeZone", "DateStyle", "IntervalStyle", "extra_float_digits",
        "client_encoding", "search_path", "standard_conforming_strings", "bytea_output",
        "duckdb.memory_limit", "duckdb.threads",
        "duckdb.max_workers_per_postgres_scan", "duckdb.threads_for_postgres_scan",
        "duckdb.postgres_role", "duckdb.convert_unsupported_numeric_to_double",
        "duckdb.enable_external_access", "duckdb.disabled_filesystems",
        "duckdb.default_collation", "duckdb.temporary_directory",
        "duckdb.max_temp_directory_size"
    };
    static const char key_version[] = "pg_qi bound v2";
    static const char hex[] = "0123456789abcdef";
    Oid identity[] = {MyDatabaseId, GetSessionUserId(), GetUserId(), walk->provider_role};
    uint8 digest[QI_BOUND_KEY_LENGTH / 2];
    char *tree = nodeToString(query);
    pg_cryptohash_ctx *hash = pg_cryptohash_create(PG_SHA256);
    bool success = hash != NULL && pg_cryptohash_init(hash) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) key_version, sizeof(key_version)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) identity, sizeof(identity)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &PgStartTime, sizeof(PgStartTime)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &XactIsoLevel, sizeof(XactIsoLevel)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &XactReadOnly, sizeof(XactReadOnly)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) &walk->in_transaction_block,
                             sizeof(walk->in_transaction_block)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) source, strlen(source) + 1) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) tree, strlen(tree) + 1) == 0 &&
        qi_bound_hash_values(hash, walk) &&
        pg_cryptohash_update(hash, (const uint8 *) &walk->relation_count,
                             sizeof(walk->relation_count)) == 0 &&
        pg_cryptohash_update(hash, (const uint8 *) walk->relations,
                             walk->relation_count * sizeof(QiBoundRelation)) == 0;
    Size index;

    pfree(tree);
    for (index = 0; success && index < lengthof(settings); index++)
        success = qi_bound_hash_setting(hash, settings[index]);
    success = success && qi_bound_hash_planner_settings(hash) &&
        pg_cryptohash_final(hash, digest, sizeof(digest)) == 0;
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

    if (params == NULL || params->numParams <= 0)
        return assessment;

    walk.in_transaction_block = IsTransactionBlock();
    assessment.eligible = QI_GATE_VETO;
    if (IsSubTransaction() || IsInParallelMode() || IsolationIsSerializable() ||
        (walk.in_transaction_block && !XactReadOnly) ||
        object_access_hook != NULL || needs_fmgr_hook != NULL ||
        TransactionIdIsValid(GetTopTransactionIdIfAny()) ||
        params->numParams > QI_BOUND_MAX_PARAMETERS || params->paramFetch != NULL ||
        params->paramCompile != NULL ||
        source == NULL || strnlen(source, QI_BOUND_MAX_SOURCE_BYTES + 1) >
        QI_BOUND_MAX_SOURCE_BYTES)
        return assessment;

#if PG_VERSION_NUM < 190000
    /* These provider deparsers escape backslashes for the PostgreSQL setting. */
    if (strcmp(GetConfigOption("standard_conforming_strings", false, false), "off") == 0)
        return assessment;
#endif
    walk.provider_role = qi_bound_provider_role();
    if (!OidIsValid(walk.provider_role) ||
        !has_privs_of_role(GetUserId(), walk.provider_role))
        return assessment;

    if (query == NULL || !qi_bound_parameter_sizes(&walk) ||
        qi_bound_walk((Node *) query, &walk))
        return assessment;

    assessment.eligible = QI_GATE_UNKNOWN;
    assessment.semantically_safe = QI_GATE_UNKNOWN;
    assessment.beneficial = QI_GATE_UNKNOWN;
    if (qi_bound_make_key(&assessment, query, source, &walk))
        qi_bound_apply_policy(&assessment);
    return assessment;
}
