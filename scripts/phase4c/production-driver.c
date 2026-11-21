#define _POSIX_C_SOURCE 200809L

#include <libpq-fe.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static void fail(PGconn *c, PGresult *r, const char *what)
{
    fprintf(stderr, "%s: %s", what, c ? PQerrorMessage(c) : "failure\n");
    if (r) PQclear(r);
    if (c) PQfinish(c);
    exit(1);
}

static PGresult *run(PGconn *c, const char *sql, ExecStatusType wanted)
{
    PGresult *r = PQexec(c, sql);
    if (PQresultStatus(r) != wanted) fail(c, r, sql);
    return r;
}

static void command(PGconn *c, const char *sql)
{
    PGresult *r = run(c, sql, PGRES_COMMAND_OK);
    PQclear(r);
}

static char *show_value(PGconn *c, const char *name)
{
    char sql[160];
    PGresult *r;
    char *out;
    if (snprintf(sql, sizeof(sql), "SHOW %s", name) >= (int) sizeof(sql)) fail(c, NULL, "SHOW name too long");
    r = run(c, sql, PGRES_TUPLES_OK);
    if (PQntuples(r) != 1 || PQnfields(r) != 1) fail(c, r, sql);
    out = strdup(PQgetvalue(r, 0, 0));
    PQclear(r);
    if (!out) fail(c, NULL, "strdup");
    return out;
}

static void expect_show(PGconn *c, const char *name, const char *expected)
{
    char *actual = show_value(c, name);
    if (strcmp(actual, expected) != 0) {
        fprintf(stderr, "%s: expected '%s', got '%s'\n", name, expected, actual);
        free(actual); PQfinish(c); exit(1);
    }
    free(actual);
}

static void validate_rows(PGconn *c, PGresult *r)
{
    unsigned char seen[100] = {0};
    int row;
    if (PQntuples(r) != 100 || PQnfields(r) != 2)
        fail(c, r, "result oracle");
    for (row = 0; row < PQntuples(r); row++)
    {
        char *end = NULL;
        long id;
        if (PQgetisnull(r, row, 0) || PQgetisnull(r, row, 1) ||
            strcmp(PQgetvalue(r, row, 1), "1") != 0)
            fail(c, r, "result oracle value");
        errno = 0;
        id = strtol(PQgetvalue(r, row, 0), &end, 10);
        if (errno || *end || id < 1 || id > 9901 || (id - 1) % 100 != 0 ||
            seen[(id - 1) / 100])
            fail(c, r, "result oracle identity");
        seen[(id - 1) / 100] = 1;
    }
    for (row = 0; row < 100; row++)
        if (!seen[row]) fail(c, r, "result oracle missing row");
}

static void snapshot(PGconn *c, FILE *out, const char *case_id)
{
    static const char *fields[] = {
        "pg_qi.shadow_contract_version", "pg_qi.shadow_decision_id", "pg_qi.shadow_context",
        "pg_qi.shadow_policy_intent", "pg_qi.shadow_reason",
        "pg_qi.shadow_force_origin", "pg_qi.shadow_classifier_status",
        "pg_qi.shadow_classifier_reason", "pg_qi.shadow_observed_engine",
        "pg_qi.shadow_verification", "pg_qi.shadow_request_applied",
        "pg_qi.shadow_p0_facts_state", "pg_qi.shadow_p0_facts_reason",
        "duckdb.force_execution"
    };
    size_t i;
    for (i = 0; i < sizeof(fields) / sizeof(fields[0]); i++)
    {
        char *value = show_value(c, fields[i]);
        fprintf(out, "snapshot\t%s\t%s\t%s\n", case_id, fields[i], value);
        free(value);
    }
}

static void case_pass(FILE *out, const char *case_id, const char *detail)
{
    fprintf(out, "case\t%s\tPASS\t%s\n", case_id, detail);
}

static void case_not_run(FILE *out, const char *case_id, const char *detail)
{
    fprintf(out, "case\t%s\tNOT_RUN\t%s\n", case_id, detail);
}

static int extension_available(PGconn *c, const char *name)
{
    const char *values[1] = {name};
    PGresult *r = PQexecParams(c,
        "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name=$1)",
        1, NULL, values, NULL, NULL, 0);
    int available;
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1)
        fail(c, r, "extension availability");
    available = !strcmp(PQgetvalue(r, 0, 0), "t");
    PQclear(r);
    return available;
}

static void parameter_case(PGconn *c, FILE *out, const char *case_id,
                           const char *cache_mode)
{
    PGresult *r;
    const char *values[1] = {"1"};
    char setting[96];
    if (snprintf(setting, sizeof(setting), "SET plan_cache_mode=%s", cache_mode) >= (int) sizeof(setting))
        fail(c, NULL, "plan cache mode");
    command(c, setting);
    r = PQprepare(c, case_id, "SELECT id FROM qi_pilot_t WHERE id=$1", 1, NULL);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) fail(c, r, "prepare parameter");
    PQclear(r);
    r = PQexecPrepared(c, case_id, 1, values, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "1"))
        fail(c, r, "execute parameter");
    PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    snapshot(c, out, case_id);
    case_pass(out, case_id, "parameterized extended protocol remained conservative");
    { char deallocate[96]; snprintf(deallocate, sizeof(deallocate), "DEALLOCATE %s", case_id); command(c, deallocate); }
}

static void negative_case(PGconn *c, FILE *out, const char *case_id,
                          const char *sql)
{
    PGresult *r = run(c, sql, PGRES_TUPLES_OK);
    PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    expect_show(c, "duckdb.force_execution", "off");
    snapshot(c, out, case_id);
    case_pass(out, case_id, "production classifier/context rejected delegation");
}

static uint64_t elapsed_us(struct timespec a, struct timespec b)
{
    int64_t ns = ((int64_t)b.tv_sec - a.tv_sec) * 1000000000LL + b.tv_nsec - a.tv_nsec;
    return (uint64_t)(ns / 1000);
}

static void set_mode(PGconn *c, const char *mode)
{
    if (!strcmp(mode, "allowlist")) { command(c, "SET pg_qi.enabled=on"); command(c, "SET pg_qi.routing_mode=allowlist"); }
    else if (!strcmp(mode, "shadow_p0")) { command(c, "SET pg_qi.enabled=on"); command(c, "SET pg_qi.routing_mode=shadow_p0"); }
    else if (!strcmp(mode, "off")) { command(c, "SET pg_qi.enabled=on"); command(c, "SET pg_qi.routing_mode=off"); }
    else if (!strcmp(mode, "disabled")) { command(c, "SET pg_qi.routing_mode=off"); command(c, "SET pg_qi.enabled=off"); }
    else fail(c, NULL, "invalid mode");
}

static void smoke(PGconn *c, FILE *out)
{
    PGresult *r;
    char *before, *after, *prepared_id, *reuse_id;
    command(c, "SET statement_timeout='30s'");
    fprintf(out, "record\tcase\tfield\tvalue\n");
    set_mode(c, "allowlist");
    r = run(c, "SELECT id,v FROM qi_pilot_t WHERE v = 1", PGRES_TUPLES_OK);
    validate_rows(c, r); PQclear(r);
    expect_show(c, "pg_qi.shadow_reason", "delegation_verified");
    expect_show(c, "pg_qi.shadow_request_applied", "on");
    expect_show(c, "pg_qi.shadow_observed_engine", "duckdb");
    expect_show(c, "pg_qi.shadow_verification", "verified");
    expect_show(c, "pg_qi.shadow_context", "proven_client");
    expect_show(c, "pg_qi.shadow_policy_intent", "request_delegation");
    expect_show(c, "pg_qi.shadow_force_origin", "qi_stack");
    expect_show(c, "pg_qi.shadow_classifier_status", "candidate");
    expect_show(c, "duckdb.force_execution", "off");
    before = show_value(c, "pg_qi.shadow_decision_id");
    snapshot(c, out, "eligible_literal");
    case_pass(out, "eligible_literal", "verified DuckDB request and exact result multiset");

    parameter_case(c, out, "parameter_custom", "force_custom_plan");
    parameter_case(c, out, "parameter_generic", "force_generic_plan");

    command(c, "SET plan_cache_mode=auto");
    r = PQprepare(c, "pgqi4c_reuse", "SELECT id,v FROM qi_pilot_t WHERE v = 1", 0, NULL);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) fail(c, r, "prepare reuse");
    PQclear(r);
    r = PQexecPrepared(c, "pgqi4c_reuse", 0, NULL, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK) fail(c, r, "execute reuse");
    validate_rows(c, r);
    PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "on");
    prepared_id = show_value(c, "pg_qi.shadow_decision_id");
    r = PQexecPrepared(c, "pgqi4c_reuse", 0, NULL, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK) fail(c, r, "execute cached reuse");
    validate_rows(c, r);
    PQclear(r);
    reuse_id = show_value(c, "pg_qi.shadow_decision_id");
    if (strcmp(prepared_id, reuse_id)) fail(c, NULL, "cached execution changed decision id");
    snapshot(c, out, "prepared_reuse");
    case_pass(out, "prepared_reuse", "delegated cached execution retained completed decision id");
    free(prepared_id); free(reuse_id);

    command(c, "ALTER TABLE qi_pilot_t ALTER COLUMN nullable SET DEFAULT 0");
    reuse_id = show_value(c, "pg_qi.shadow_decision_id");
    r = PQexecPrepared(c, "pgqi4c_reuse", 0, NULL, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK) fail(c, r, "execute invalidated plan");
    validate_rows(c, r); PQclear(r);
    prepared_id = show_value(c, "pg_qi.shadow_decision_id");
    if (!strcmp(prepared_id, reuse_id)) fail(c, NULL, "invalidated plan did not publish a new decision");
    expect_show(c, "pg_qi.shadow_request_applied", "on");
    expect_show(c, "pg_qi.shadow_verification", "verified");
    snapshot(c, out, "prepared_invalidation");
    case_pass(out, "prepared_invalidation", "invalidated prepared statement replanned and delegated safely");
    command(c, "DEALLOCATE pgqi4c_reuse");

    free(prepared_id); free(reuse_id);
    prepared_id = show_value(c, "pg_qi.shadow_decision_id");
    r = PQexec(c, "SELECT 1 / 0");
    if (PQresultStatus(r) != PGRES_FATAL_ERROR || !PQresultErrorField(r, PG_DIAG_SQLSTATE) ||
        strcmp(PQresultErrorField(r, PG_DIAG_SQLSTATE), "22012"))
        fail(c, r, "planning error SQLSTATE");
    PQclear(r);
    reuse_id = show_value(c, "pg_qi.shadow_decision_id");
    if (strcmp(prepared_id, reuse_id)) fail(c, NULL, "planning error replaced completed snapshot");
    snapshot(c, out, "planning_error");
    case_pass(out, "planning_error", "SQLSTATE 22012 retained prior completed snapshot");
    free(prepared_id); free(reuse_id);

    negative_case(c, out, "join_rejected", "SELECT a.id FROM qi_pilot_t a JOIN qi_pilot_t b ON a.id=b.id WHERE a.id=1");
    negative_case(c, out, "cast_rejected", "SELECT id::bigint FROM qi_pilot_t WHERE id=1");
    negative_case(c, out, "function_rejected", "SELECT abs(id) FROM qi_pilot_t WHERE id=1");
    command(c, "CREATE TEMP TABLE qi_pilot_temp(id integer)");
    command(c, "INSERT INTO qi_pilot_temp VALUES (1)");
    negative_case(c, out, "temporary_relation_rejected", "SELECT id FROM qi_pilot_temp WHERE id=1");
    negative_case(c, out, "unlogged_relation_rejected", "SELECT id FROM qi_pilot_unlogged WHERE id=1");
    negative_case(c, out, "partitioned_relation_rejected", "SELECT id FROM qi_pilot_partitioned WHERE id=1");
    if (extension_available(c, "file_fdw"))
        negative_case(c, out, "foreign_relation_rejected", "SELECT id FROM qi_pilot_foreign WHERE id=1");
    else
        case_not_run(out, "foreign_relation_rejected", "file_fdw is unavailable in this PostgreSQL installation");
    command(c, "SET ROLE qi_pilot_role");
    negative_case(c, out, "rls_ordinary_role_rejected", "SELECT id FROM qi_pilot_rls WHERE id=1");
    r = PQexec(c, "SET pg_qi.routing_mode=allowlist");
    if (PQresultStatus(r) != PGRES_FATAL_ERROR || !PQresultErrorField(r, PG_DIAG_SQLSTATE) ||
        strcmp(PQresultErrorField(r, PG_DIAG_SQLSTATE), "42501"))
        fail(c, r, "ordinary role routing control");
    PQclear(r);
    command(c, "RESET ROLE");
    case_pass(out, "ordinary_role_control_denied", "SQLSTATE 42501");

    set_mode(c, "off");
    r = run(c, "SELECT id,v FROM qi_pilot_t WHERE v = 1", PGRES_TUPLES_OK);
    validate_rows(c, r); PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    after = show_value(c, "pg_qi.shadow_decision_id");
    snapshot(c, out, "policy_off");
    case_pass(out, "policy_off", "request off");
    set_mode(c, "disabled");
    r = run(c, "SELECT id,v FROM qi_pilot_t WHERE v = 1", PGRES_TUPLES_OK);
    validate_rows(c, r); PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    snapshot(c, out, "extension_disabled");
    case_pass(out, "extension_disabled", "request off");
    fprintf(out, "info\tdecision_range\tbefore\t%s\ninfo\tdecision_range\tafter\t%s\n", before, after);
    free(before); free(after);
}

static void benchmark(PGconn *c, FILE *out, const char *mode, long warmup, long samples)
{
    long i;
    PGresult *r;
    char *shadow_before = NULL;
    char *shadow_after = NULL;
    long shadow_before_id = 0;
    long shadow_after_id = 0;
    char *end = NULL;
    set_mode(c, mode);
    command(c, "SET statement_timeout='30s'");
    for (i=0; i<warmup; i++) { r=run(c,"SELECT id,v FROM qi_pilot_t WHERE v = 1",PGRES_TUPLES_OK); validate_rows(c,r); PQclear(r); }
    if (!strcmp(mode, "shadow_p0"))
    {
        shadow_before = show_value(c, "pg_qi.shadow_decision_id");
        errno = 0;
        shadow_before_id = strtol(shadow_before, &end, 10);
        if (errno || !end || *end) fail(c, NULL, "invalid pre-benchmark decision id");
    }
    fprintf(out, "sample\tlatency_us\n");
    for (i=0; i<samples; i++) {
        struct timespec a,b;
        if (clock_gettime(CLOCK_MONOTONIC,&a)) fail(c,NULL,"clock_gettime");
        r=run(c,"SELECT id,v FROM qi_pilot_t WHERE v = 1",PGRES_TUPLES_OK);
        if (clock_gettime(CLOCK_MONOTONIC,&b)) fail(c,r,"clock_gettime");
        validate_rows(c,r); PQclear(r);
        fprintf(out, "%ld\t%llu\n", i+1, (unsigned long long)elapsed_us(a,b));
    }
    if (!strcmp(mode,"allowlist")) { expect_show(c,"pg_qi.shadow_reason","delegation_verified"); expect_show(c,"pg_qi.shadow_request_applied","on"); }
    else {
        expect_show(c,"pg_qi.shadow_request_applied","off");
        if (!strcmp(mode,"shadow_p0"))
        {
            shadow_after = show_value(c, "pg_qi.shadow_decision_id");
            errno = 0;
            shadow_after_id = strtol(shadow_after, &end, 10);
            if (errno || !end || *end || shadow_after_id - shadow_before_id != samples)
                fail(c, NULL, "timed shadow decisions did not match sample count");
            expect_show(c,"pg_qi.routing_mode","shadow_p0");
            expect_show(c,"pg_qi.shadow_reason","decision_only");
            expect_show(c,"pg_qi.shadow_p0_facts_state","unknown");
            expect_show(c,"pg_qi.shadow_p0_facts_reason","unsupported_shape");
            fprintf(out, "info\tshadow_decision_before\t%s\ninfo\tshadow_decision_after\t%s\n",
                    shadow_before, shadow_after);
            free(shadow_before);
            free(shadow_after);
        }
    }
    fprintf(out, "info\tmode\t%s\ninfo\twarmup\t%ld\ninfo\tsamples\t%ld\n", mode, warmup, samples);
    snapshot(c, out, mode);
}

static void rollback_probe(PGconn *c, FILE *out)
{
    PGresult *r;
    char *planned_id, *cached_id;
    command(c, "SET statement_timeout='30s'");
    set_mode(c, "allowlist");
    r = PQprepare(c, "pgqi4c_cached", "SELECT id FROM qi_pilot_t WHERE id=1", 0, NULL);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) fail(c, r, "rollback prepare");
    PQclear(r);
    r = PQexecPrepared(c, "pgqi4c_cached", 0, NULL, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "1")) fail(c, r, "rollback initial execute");
    PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "on");
    planned_id = show_value(c, "pg_qi.shadow_decision_id");
    set_mode(c, "off");
    r = PQexecPrepared(c, "pgqi4c_cached", 0, NULL, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "1")) fail(c, r, "rollback cached execute");
    PQclear(r);
    cached_id = show_value(c, "pg_qi.shadow_decision_id");
    if (strcmp(planned_id, cached_id)) fail(c, NULL, "cached plan unexpectedly replanned");
    snapshot(c, out, "cached_after_disable");
    command(c, "DEALLOCATE pgqi4c_cached");
    r = run(c, "SELECT id FROM qi_pilot_t WHERE id=2", PGRES_TUPLES_OK);
    if (PQntuples(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "2")) fail(c, r, "rollback fresh off query");
    PQclear(r);
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    snapshot(c, out, "fresh_after_disable");
    command(c, "SET duckdb.force_execution=on");
    r = run(c, "SELECT id FROM qi_pilot_t WHERE id=3", PGRES_TUPLES_OK);
    if (PQntuples(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "3")) fail(c, r, "external force probe");
    PQclear(r);
    expect_show(c, "pg_qi.shadow_reason", "force_external");
    expect_show(c, "pg_qi.shadow_force_origin", "external");
    expect_show(c, "pg_qi.shadow_request_applied", "off");
    expect_show(c, "duckdb.force_execution", "on");
    snapshot(c, out, "external_force");
    command(c, "SET duckdb.force_execution=off");
    expect_show(c, "duckdb.force_execution", "off");
    case_pass(out, "rollback_reconnect", "cached-plan limit, fresh disable and external force separation verified");
    free(planned_id); free(cached_id);
}

int main(int argc, char **argv)
{
    PGconn *c; FILE *out; char *end; long warmup=0,samples=0;
    if (argc < 4 || argc > 7) { fprintf(stderr,"usage: %s CONNINFO smoke|rollback OUTPUT | benchmark MODE OUTPUT WARMUP SAMPLES\n",argv[0]); return 2; }
    if (!strcmp(argv[2],"benchmark")) {
        if (argc != 7) return 2;
        errno=0; warmup=strtol(argv[5],&end,10); if(errno||*end||warmup<0||warmup>100000) return 2;
        errno=0; samples=strtol(argv[6],&end,10); if(errno||*end||samples<1||samples>1000000) return 2;
    } else if ((strcmp(argv[2],"smoke") && strcmp(argv[2],"rollback")) || argc != 4) return 2;
    out=fopen(strcmp(argv[2],"benchmark")?argv[3]:argv[4],"w"); if(!out){perror(strcmp(argv[2],"benchmark")?argv[3]:argv[4]);return 1;}
    c=PQconnectdb(argv[1]); if(PQstatus(c)!=CONNECTION_OK) fail(c,NULL,"connect");
    if(!strcmp(argv[2],"smoke")) smoke(c,out);
    else if (!strcmp(argv[2],"rollback")) rollback_probe(c,out);
    else benchmark(c,out,argv[3],warmup,samples);
    if (fclose(out))
        fail(c, NULL, "close output");
    PQfinish(c);
    return 0;
}
