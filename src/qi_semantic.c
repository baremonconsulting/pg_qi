/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/xact.h"
#include "access/htup_details.h"
#include "catalog/pg_aggregate.h"
#include "catalog/pg_authid.h"
#include "catalog/pg_class.h"
#include "catalog/pg_collation_d.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_operator.h"
#include "catalog/pg_proc.h"
#include "catalog/pg_type_d.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "nodes/nodeFuncs.h"
#include "nodes/makefuncs.h"
#include "nodes/primnodes.h"
#include "pg_qi/qi_semantic.h"
#include "utils/acl.h"
#include "utils/lsyscache.h"
#include "utils/fmgroids.h"
#include "utils/syscache.h"

#define QI_SEMANTIC_BASE_BLOCKERS \
    (QI_SEMANTIC_BLOCKER_INACTIVE | \
     QI_SEMANTIC_BLOCKER_BUILD_IDENTITY_UNVERIFIED | \
     QI_SEMANTIC_BLOCKER_SNAPSHOT_UNPROVEN)
#define QI_EXPECTED_PG_DUCKDB_SHA \
    "8756099e267c47f0b86c24de4cf995327f1f4279"
#define QI_EXPECTED_DUCKDB_SHA \
    "d54cbbe3df17e8e5ce68312f483b4155836112aa"

typedef struct QiSemanticDeclaration
{
    QiShadowP0Capability capability;
    QiSemanticProfileId profile;
    Oid aggregate_oid;
    Oid formal_input_oid;
    Oid actual_input_oid;
    Oid result_oid;
    Oid aggregate_sort_operator;
    int32 input_typmod;
    int32 result_typmod;
    Oid input_collation;
    Oid result_collation;
    uint8 argument_count;
    uint16 postgres_major;
    const char *postgres_version;
    const char *expected_pg_duckdb_sha;
    const char *expected_duckdb_sha;
} QiSemanticDeclaration;

/*
 * These declarations describe a future certificate boundary.  They are not
 * certificates and cannot be activated by configuration or by a caller.
 */
static const QiSemanticDeclaration qi_semantic_declarations[] = {
    {QI_SHADOW_P0_CAPABILITY_COUNT_STAR, QI_SEMANTIC_PROFILE_COUNT_STAR,
     2803, InvalidOid, InvalidOid, INT8OID, InvalidOid,
     -1, -1, InvalidOid, InvalidOid, 0, PG_VERSION_NUM / 10000, PG_VERSION,
     QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA},
    {QI_SHADOW_P0_CAPABILITY_COUNT_INT4, QI_SEMANTIC_PROFILE_COUNT_INT4,
     2147, ANYOID, INT4OID, INT8OID, InvalidOid,
     -1, -1, InvalidOid, InvalidOid, 1, PG_VERSION_NUM / 10000, PG_VERSION,
     QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA},
    {QI_SHADOW_P0_CAPABILITY_MIN_INT4, QI_SEMANTIC_PROFILE_MIN_INT4,
     2132, INT4OID, INT4OID, INT4OID, 97,
     -1, -1, InvalidOid, InvalidOid, 1, PG_VERSION_NUM / 10000, PG_VERSION,
     QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA},
    {QI_SHADOW_P0_CAPABILITY_MAX_INT4, QI_SEMANTIC_PROFILE_MAX_INT4,
     2116, INT4OID, INT4OID, INT4OID, 521,
     -1, -1, InvalidOid, InvalidOid, 1, PG_VERSION_NUM / 10000, PG_VERSION,
     QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA}
};

typedef struct QiSemanticBuildIdentity
{
    bool present;
    uint16 postgres_major;
    const char *postgres_version;
    const char *pg_duckdb_sha;
    const char *duckdb_sha;
} QiSemanticBuildIdentity;

static bool
qi_semantic_sha_valid(const char *sha)
{
    Size index;

    if (sha == NULL || strlen(sha) != 40)
        return false;
    for (index = 0; index < 40; index++)
        if (!((sha[index] >= '0' && sha[index] <= '9') ||
              (sha[index] >= 'a' && sha[index] <= 'f')))
            return false;
    return true;
}

static bool
qi_semantic_identity_matches(const QiSemanticDeclaration *declaration,
                             const QiSemanticBuildIdentity *observed)
{
    return declaration != NULL && observed != NULL && observed->present &&
        observed->postgres_version != NULL &&
        qi_semantic_sha_valid(observed->pg_duckdb_sha) &&
        qi_semantic_sha_valid(observed->duckdb_sha) &&
        observed->postgres_major == declaration->postgres_major &&
        strcmp(observed->postgres_version, declaration->postgres_version) == 0 &&
        strcmp(observed->pg_duckdb_sha,
               declaration->expected_pg_duckdb_sha) == 0 &&
        strcmp(observed->duckdb_sha, declaration->expected_duckdb_sha) == 0;
}

static const QiSemanticDeclaration *
qi_semantic_declaration(QiShadowP0Capability capability)
{
    Size i;

    for (i = 0; i < lengthof(qi_semantic_declarations); i++)
        if (qi_semantic_declarations[i].capability == capability)
            return &qi_semantic_declarations[i];
    return NULL;
}

static bool
qi_semantic_catalog_matches(const Query *query,
                            const QiSemanticDeclaration *declaration,
                            const QiShadowP0Facts *p0_facts)
{
    const TargetEntry *target;
    const Aggref *aggregate;
    HeapTuple tuple;
    Form_pg_proc procedure;
    Form_pg_aggregate aggregate_catalog;
    bool matches = true;

    if (query->targetList == NIL || !IsA(query->targetList, List) ||
        list_length(query->targetList) != 1 ||
        !IsA(linitial(query->targetList), TargetEntry))
        return false;
    target = linitial_node(TargetEntry, query->targetList);
    if (target->resjunk || target->expr == NULL ||
        !IsA(target->expr, Aggref))
        return false;
    aggregate = (const Aggref *) target->expr;
    if ((aggregate->args != NIL && !IsA(aggregate->args, List)) ||
        (aggregate->aggargtypes != NIL &&
         !IsA(aggregate->aggargtypes, OidList)))
        return false;
    if (aggregate->aggfnoid != declaration->aggregate_oid ||
        aggregate->aggtype != declaration->result_oid ||
        exprTypmod((const Node *) aggregate) != declaration->result_typmod ||
        aggregate->aggcollid != declaration->result_collation ||
        aggregate->inputcollid != declaration->input_collation ||
        aggregate->aggkind != AGGKIND_NORMAL || aggregate->aggstar !=
            (declaration->argument_count == 0) ||
        aggregate->aggvariadic || aggregate->aggdistinct != NIL ||
        aggregate->aggorder != NIL || aggregate->aggfilter != NULL ||
        aggregate->aggdirectargs != NIL || aggregate->agglevelsup != 0 ||
        aggregate->aggsplit != AGGSPLIT_SIMPLE || aggregate->aggpresorted ||
        list_length(aggregate->args) != declaration->argument_count ||
        list_length(aggregate->aggargtypes) != declaration->argument_count)
        matches = false;
    if (declaration->argument_count == 1)
    {
        const TargetEntry *argument;
        const Var *variable;

        if (aggregate->args == NIL ||
            !IsA(linitial(aggregate->args), TargetEntry))
            matches = false;
        else
        {
            argument = linitial_node(TargetEntry, aggregate->args);
            if (argument->expr == NULL || !IsA(argument->expr, Var))
                matches = false;
            else
            {
                variable = (const Var *) argument->expr;
                if (variable->vartype != declaration->actual_input_oid ||
                    variable->vartypmod != declaration->input_typmod ||
                    variable->varcollid != declaration->input_collation ||
                    variable->varattno != p0_facts->argument_attno)
                    matches = false;
            }
        }
        if (aggregate->aggargtypes == NIL ||
            linitial_oid(aggregate->aggargtypes) !=
                declaration->actual_input_oid)
            matches = false;
    }

    tuple = SearchSysCache1(PROCOID,
                            ObjectIdGetDatum(declaration->aggregate_oid));
    if (!HeapTupleIsValid(tuple))
        matches = false;
    else
    {
        procedure = (Form_pg_proc) GETSTRUCT(tuple);
        if (procedure->pronamespace != PG_CATALOG_NAMESPACE ||
            procedure->prokind != PROKIND_AGGREGATE || procedure->proretset ||
            OidIsValid(procedure->provariadic) ||
            procedure->prorettype != declaration->result_oid ||
            procedure->pronargs != declaration->argument_count ||
            (declaration->argument_count == 1 &&
             procedure->proargtypes.values[0] !=
                 declaration->formal_input_oid))
            matches = false;
        ReleaseSysCache(tuple);
    }

    tuple = SearchSysCache1(AGGFNOID,
                            ObjectIdGetDatum(declaration->aggregate_oid));
    if (!HeapTupleIsValid(tuple))
        matches = false;
    else
    {
        aggregate_catalog = (Form_pg_aggregate) GETSTRUCT(tuple);
        if (aggregate_catalog->aggkind != AGGKIND_NORMAL ||
            aggregate_catalog->aggnumdirectargs != 0 ||
            aggregate_catalog->aggsortop !=
                declaration->aggregate_sort_operator)
            matches = false;
        ReleaseSysCache(tuple);
    }

    if (OidIsValid(declaration->aggregate_sort_operator))
    {
        Form_pg_operator operator;

        tuple = SearchSysCache1(OPEROID, ObjectIdGetDatum(
            declaration->aggregate_sort_operator));
        if (!HeapTupleIsValid(tuple))
            matches = false;
        else
        {
            operator = (Form_pg_operator) GETSTRUCT(tuple);
            if (operator->oprnamespace != PG_CATALOG_NAMESPACE ||
                operator->oprleft != INT4OID || operator->oprright != INT4OID ||
                operator->oprresult != BOOLOID)
                matches = false;
            ReleaseSysCache(tuple);
        }
    }
    return matches;
}

static bool
qi_semantic_acl_ok(const QiSemanticDeclaration *declaration,
                   Oid relation_oid, AttrNumber argument_attno)
{
    Oid role = GetUserId();

    if (pg_class_aclcheck(relation_oid, role, ACL_SELECT) == ACLCHECK_OK)
        return true;
    if (declaration->profile != QI_SEMANTIC_PROFILE_COUNT_STAR)
        return argument_attno > 0 &&
            pg_attribute_aclcheck(relation_oid, argument_attno, role,
                                  ACL_SELECT) == ACLCHECK_OK;

    return pg_attribute_aclcheck_all(relation_oid, role, ACL_SELECT,
                                     ACLMASK_ANY) == ACLCHECK_OK;
}

QiSemanticFacts
qi_semantic_observe_query(const Query *query, const QiShadowP0Facts *p0_facts)
{
    QiSemanticFacts result = {QI_SEMANTIC_NOT_EVALUATED,
                              QI_SEMANTIC_PROFILE_NONE, 0};
    const QiSemanticDeclaration *declaration;
    RangeTblEntry *rte = NULL;
    RTEPermissionInfo *perminfo = NULL;
    QiSemanticBuildIdentity runtime_identity = {
        false, 0, NULL, NULL, NULL
    };

    if (query == NULL || p0_facts == NULL || !IsA(query, Query) ||
        p0_facts->state != QI_SHADOW_P0_FACTS_BOUNDED ||
        p0_facts->reason != QI_SHADOW_P0_FACTS_OK)
        return result;
    declaration = qi_semantic_declaration(p0_facts->capability);
    if (declaration == NULL)
        return result;

    result.status = QI_SEMANTIC_UNKNOWN;
    result.profile = declaration->profile;
    result.blockers = QI_SEMANTIC_BLOCKER_INACTIVE |
        QI_SEMANTIC_BLOCKER_SNAPSHOT_UNPROVEN;
    if (!qi_semantic_identity_matches(declaration, &runtime_identity))
        result.blockers |= QI_SEMANTIC_BLOCKER_BUILD_IDENTITY_UNVERIFIED;

    if (!qi_semantic_catalog_matches(query, declaration, p0_facts))
        result.blockers |= QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH;

    if (query->rtable == NIL || !IsA(query->rtable, List) ||
        list_length(query->rtable) != 1 ||
        !IsA(linitial(query->rtable), RangeTblEntry))
        result.blockers |= QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH;
    else
    {
        rte = linitial_node(RangeTblEntry, query->rtable);
        if (rte->rtekind != RTE_RELATION ||
            rte->relid != p0_facts->relation_oid ||
            !OidIsValid(rte->relid))
            result.blockers |= QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH;
        if (rte->securityQuals != NIL)
            result.blockers |= QI_SEMANTIC_BLOCKER_SECURITY_QUAL;
        if (rte->security_barrier)
            result.blockers |= QI_SEMANTIC_BLOCKER_SECURITY_QUAL;
        if ((query->rteperminfos != NIL &&
             !IsA(query->rteperminfos, List)) ||
            rte->perminfoindex <= 0 ||
            rte->perminfoindex > list_length(query->rteperminfos))
            result.blockers |= QI_SEMANTIC_BLOCKER_PERMISSION_METADATA;
        else
        {
            Node *node = list_nth(query->rteperminfos,
                                  rte->perminfoindex - 1);
            if (node == NULL || !IsA(node, RTEPermissionInfo))
                result.blockers |= QI_SEMANTIC_BLOCKER_PERMISSION_METADATA;
            else
            {
                perminfo = (RTEPermissionInfo *) node;
                if (perminfo->relid != rte->relid ||
                    perminfo->requiredPerms != ACL_SELECT ||
                    perminfo->insertedCols != NULL ||
                    perminfo->updatedCols != NULL)
                    result.blockers |= QI_SEMANTIC_BLOCKER_PERMISSION_METADATA;
                if (OidIsValid(perminfo->checkAsUser))
                    result.blockers |= QI_SEMANTIC_BLOCKER_CHECK_AS_USER;
            }
        }
    }

    if (GetUserId() != GetSessionUserId())
        result.blockers |= QI_SEMANTIC_BLOCKER_ROLE_CONTEXT;
    if (query->hasRowSecurity)
        result.blockers |= QI_SEMANTIC_BLOCKER_RLS;
    if (rte != NULL && rte->rtekind == RTE_RELATION)
    {
        HeapTuple relation_tuple = SearchSysCache1(
            RELOID, ObjectIdGetDatum(rte->relid));

        if (!HeapTupleIsValid(relation_tuple))
            result.blockers |= QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH;
        else
        {
            Form_pg_class relation = (Form_pg_class) GETSTRUCT(relation_tuple);

            if (relation->relrowsecurity || relation->relforcerowsecurity)
                result.blockers |= QI_SEMANTIC_BLOCKER_RLS;
            ReleaseSysCache(relation_tuple);
        }
    }
    if (rte == NULL || rte->rtekind != RTE_RELATION)
        result.blockers |= QI_SEMANTIC_BLOCKER_ACL;
    else if ((result.blockers & QI_SEMANTIC_BLOCKER_CATALOG_MISMATCH) == 0 &&
             !qi_semantic_acl_ok(declaration, p0_facts->relation_oid,
                                 p0_facts->argument_attno))
        result.blockers |= QI_SEMANTIC_BLOCKER_ACL;
    if (IsolationIsSerializable())
        result.blockers |= QI_SEMANTIC_BLOCKER_SERIALIZABLE;
    if (TransactionIdIsValid(GetTopTransactionIdIfAny()))
        result.blockers |= QI_SEMANTIC_BLOCKER_OWN_XID;
    return result;
}

QiGateState
qi_semantic_gate_state(const QiSemanticFacts *facts)
{
    if (facts != NULL && facts->status == QI_SEMANTIC_NOT_EVALUATED &&
        facts->profile == QI_SEMANTIC_PROFILE_NONE)
        return QI_GATE_NOT_EVALUATED;
    return QI_GATE_UNKNOWN;
}

#if PG_QI_TESTING
Datum
qi_test_semantic_contract(PG_FUNCTION_ARGS)
{
    QiSemanticFacts none = {QI_SEMANTIC_NOT_EVALUATED,
                            QI_SEMANTIC_PROFILE_NONE, 0};
    QiSemanticFacts matched = {QI_SEMANTIC_UNKNOWN,
                               QI_SEMANTIC_PROFILE_COUNT_STAR,
                               QI_SEMANTIC_BASE_BLOCKERS};
    QiSemanticFacts corrupt = {(QiSemanticStatus) 99,
                               QI_SEMANTIC_PROFILE_COUNT_STAR, 0};
    Size i;
    bool declarations_exact = lengthof(qi_semantic_declarations) == 4;
    uint32 blocker;
    uint32 all_blockers = 0;
    QiSemanticBuildIdentity observed = {
        true, PG_VERSION_NUM / 10000, PG_VERSION,
        QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA
    };
    QiSemanticBuildIdentity drifted;

    for (i = 0; i < lengthof(qi_semantic_declarations); i++)
        declarations_exact = declarations_exact &&
            qi_semantic_declarations[i].profile == (int) i + 1 &&
            qi_semantic_declarations[i].input_typmod == -1 &&
            qi_semantic_declarations[i].result_typmod == -1 &&
            !OidIsValid(qi_semantic_declarations[i].input_collation) &&
            !OidIsValid(qi_semantic_declarations[i].result_collation) &&
            qi_semantic_declarations[i].postgres_major ==
                PG_VERSION_NUM / 10000 &&
            strcmp(qi_semantic_declarations[i].postgres_version,
                   PG_VERSION) == 0 &&
            strlen(qi_semantic_declarations[i].expected_pg_duckdb_sha) == 40 &&
            strlen(qi_semantic_declarations[i].expected_duckdb_sha) == 40;

    declarations_exact = declarations_exact &&
        qi_semantic_identity_matches(&qi_semantic_declarations[0], &observed);
    drifted = observed;
    drifted.present = false;
    declarations_exact = declarations_exact &&
        !qi_semantic_identity_matches(&qi_semantic_declarations[0], &drifted);
    drifted = observed;
    drifted.postgres_major++;
    declarations_exact = declarations_exact &&
        !qi_semantic_identity_matches(&qi_semantic_declarations[0], &drifted);
    drifted = observed;
    drifted.postgres_version = "drift";
    declarations_exact = declarations_exact &&
        !qi_semantic_identity_matches(&qi_semantic_declarations[0], &drifted);
    drifted = observed;
    drifted.pg_duckdb_sha = "partial";
    declarations_exact = declarations_exact &&
        !qi_semantic_identity_matches(&qi_semantic_declarations[0], &drifted);
    drifted = observed;
    drifted.duckdb_sha = "D54CBBE3DF17E8E5CE68312F483B4155836112AA";
    declarations_exact = declarations_exact &&
        !qi_semantic_identity_matches(&qi_semantic_declarations[0], &drifted);

    declarations_exact = declarations_exact &&
        qi_semantic_declarations[0].aggregate_oid == 2803 &&
        qi_semantic_declarations[0].argument_count == 0 &&
        qi_semantic_declarations[0].result_oid == INT8OID &&
        qi_semantic_declarations[1].aggregate_oid == 2147 &&
        qi_semantic_declarations[1].formal_input_oid == ANYOID &&
        qi_semantic_declarations[1].actual_input_oid == INT4OID &&
        qi_semantic_declarations[1].result_oid == INT8OID &&
        qi_semantic_declarations[2].aggregate_oid == 2132 &&
        qi_semantic_declarations[2].aggregate_sort_operator == 97 &&
        qi_semantic_declarations[3].aggregate_oid == 2116 &&
        qi_semantic_declarations[3].aggregate_sort_operator == 521;

    for (blocker = QI_SEMANTIC_BLOCKER_INACTIVE;
         blocker <= QI_SEMANTIC_BLOCKER_OWN_XID; blocker <<= 1)
    {
        QiSemanticFacts blocked = matched;

        blocked.blockers |= blocker;
        all_blockers |= blocker;
        declarations_exact = declarations_exact &&
            qi_semantic_gate_state(&blocked) == QI_GATE_UNKNOWN;
    }

    PG_RETURN_BOOL(declarations_exact &&
                   qi_semantic_gate_state(&none) == QI_GATE_NOT_EVALUATED &&
                   qi_semantic_gate_state(&matched) == QI_GATE_UNKNOWN &&
                   qi_semantic_gate_state(&corrupt) == QI_GATE_UNKNOWN &&
                   qi_semantic_gate_state(NULL) == QI_GATE_UNKNOWN &&
                   all_blockers == ((QI_SEMANTIC_BLOCKER_OWN_XID << 1) - 1) &&
                   (matched.blockers & QI_SEMANTIC_BASE_BLOCKERS) ==
                       QI_SEMANTIC_BASE_BLOCKERS);
}

Datum
qi_test_semantic_probe(PG_FUNCTION_ARGS)
{
    Oid relation_oid = PG_GETARG_OID(0);
    AttrNumber attno = (AttrNumber) PG_GETARG_INT32(1);
    QiShadowP0Capability capability =
        (QiShadowP0Capability) PG_GETARG_INT32(2);
    int32 mutations = PG_GETARG_INT32(3);
    const QiSemanticDeclaration *declaration =
        qi_semantic_declaration(capability);
    Query *query;
    RangeTblEntry *rte;
    RTEPermissionInfo *perminfo;
    Aggref *aggregate;
    TargetEntry *target;
    QiShadowP0Facts p0_facts;
    QiSemanticFacts semantic;

    if (declaration == NULL || !OidIsValid(relation_oid))
        PG_RETURN_INT64(0);

    query = makeNode(Query);
    query->commandType = CMD_SELECT;
    query->querySource = QSRC_ORIGINAL;
    query->canSetTag = true;
    rte = makeNode(RangeTblEntry);
    rte->rtekind = RTE_RELATION;
    rte->relid = relation_oid;
    rte->relkind = get_rel_relkind(relation_oid);
    rte->rellockmode = AccessShareLock;
    rte->inh = false;
    rte->inFromCl = true;
    rte->perminfoindex = 1;
    query->rtable = list_make1(rte);
    perminfo = makeNode(RTEPermissionInfo);
    perminfo->relid = relation_oid;
    perminfo->requiredPerms = ACL_SELECT;
    query->rteperminfos = list_make1(perminfo);
    if ((mutations & 1) != 0)
        query->rteperminfos = list_make1(NULL);
    if ((mutations & 2) != 0)
        perminfo->checkAsUser = BOOTSTRAP_SUPERUSERID;
    if ((mutations & 4) != 0)
        perminfo->requiredPerms = ACL_UPDATE;
    if ((mutations & 8) != 0)
        rte->securityQuals = list_make1(makeBoolConst(true, false));
    if ((mutations & 16) != 0)
        query->hasRowSecurity = true;

    aggregate = makeNode(Aggref);
    aggregate->aggfnoid = declaration->aggregate_oid;
    aggregate->aggtype = declaration->result_oid;
    aggregate->aggcollid = declaration->result_collation;
    aggregate->inputcollid = declaration->input_collation;
    aggregate->aggkind = AGGKIND_NORMAL;
    aggregate->aggsplit = AGGSPLIT_SIMPLE;
    aggregate->aggstar = declaration->argument_count == 0;
    aggregate->location = -1;
    if ((mutations & 32) != 0)
        aggregate->aggfnoid = InvalidOid;
    if ((mutations & 64) != 0)
        rte->security_barrier = true;
    if ((mutations & 128) != 0)
        aggregate->aggtype = InvalidOid;
    if ((mutations & 256) != 0)
        aggregate->aggcollid = DEFAULT_COLLATION_OID;
    if ((mutations & 512) != 0)
        aggregate->aggfilter = (Expr *) makeBoolConst(true, false);
    if ((mutations & 1024) != 0)
        aggregate->aggkind = AGGKIND_ORDERED_SET;
    if ((mutations & 4096) != 0)
        aggregate->aggdirectargs = list_make1(makeBoolConst(true, false));
    if (declaration->argument_count == 1)
    {
        Var *variable = makeVar(1, attno, declaration->actual_input_oid,
                                declaration->input_typmod,
                                declaration->input_collation, 0);
        TargetEntry *argument = makeTargetEntry((Expr *) variable, 1,
                                                NULL, false);

        aggregate->args = list_make1(argument);
        aggregate->aggargtypes = list_make1_oid(
            declaration->actual_input_oid);
        if ((mutations & 2048) != 0)
            argument->expr = (Expr *) makeConst(INT4OID, -1, InvalidOid,
                                                sizeof(int32),
                                                Int32GetDatum(1), false, true);
    }
    target = makeTargetEntry((Expr *) aggregate, 1, NULL, false);
    query->targetList = list_make1(target);

    MemSet(&p0_facts, 0, sizeof(p0_facts));
    p0_facts.state = QI_SHADOW_P0_FACTS_BOUNDED;
    p0_facts.reason = QI_SHADOW_P0_FACTS_OK;
    p0_facts.capability = capability;
    p0_facts.relation_oid = relation_oid;
    p0_facts.argument_attno = declaration->argument_count == 0
        ? InvalidAttrNumber : attno;
    semantic = qi_semantic_observe_query(query, &p0_facts);
    PG_RETURN_INT64((int64) semantic.blockers);
}
#endif
