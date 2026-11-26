/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libpq-fe.h>

static const char subject[] =
    "WITH \"source\" AS ("
    " SELECT \"item\".\"id\", \"item\".\"id\" % 7 AS \"bucket\""
    " FROM \"witness_fixture\" AS \"item\""
    " LEFT JOIN \"witness_fixture\" AS \"previous\" ON \"previous\".\"id\" = \"item\".\"id\" - 1"
    " WHERE \"item\".\"id\" >= $1 AND ($2 IS NULL OR \"item\".\"id\" <= $2)"
    "), \"ranked\" AS ("
    " SELECT \"bucket\", row_number() OVER (PARTITION BY \"bucket\" ORDER BY \"id\") AS \"position\""
    " FROM \"source\""
    "), \"summary\" AS ("
    " SELECT \"bucket\", count(*) AS \"members\", max(\"position\") AS \"last_position\""
    " FROM \"ranked\" GROUP BY \"bucket\""
    ") SELECT \"bucket\", \"members\", \"last_position\" FROM \"summary\" ORDER BY \"bucket\"";

static const char *const bindings[][2] = {
    {"17", NULL}, {"17", "111"}, {"91", "111"}, {"92", "111"}
};
static unsigned int execution_number;

static void
require(int condition, const char *message)
{
    if (condition)
        return;
    fprintf(stderr, "Bound routing test failed. %s\n", message);
    exit(1);
}

static PGresult *
checked(PGconn *connection, PGresult *result, ExecStatusType expected)
{
    require(result != NULL && PQresultStatus(result) == expected,
            PQerrorMessage(connection));
    return result;
}

static void
command(PGconn *connection, const char *sql)
{
    PQclear(checked(connection, PQexec(connection, sql), PGRES_COMMAND_OK));
}

static PGresult *
read_value(PGconn *connection, const char *sql)
{
    PGresult *result = checked(connection, PQexec(connection, sql), PGRES_TUPLES_OK);

    require(PQntuples(result) == 1 && PQnfields(result) == 1 && !PQgetisnull(result, 0, 0), sql);
    return result;
}

static void
expect_value(PGconn *connection, const char *sql, const char *expected)
{
    PGresult *result = read_value(connection, sql);

    require(strcmp(PQgetvalue(result, 0, 0), expected) == 0, PQgetvalue(result, 0, 0));
    PQclear(result);
}

static void
expect_decision(PGconn *connection, const char *gates, const char *policy)
{
    PGresult *result = read_value(connection, "SHOW pg_qi.decision_v2");
    const char *decision = PQgetvalue(result, 0, 0);

    require(strstr(decision, ";mode=bound;") != NULL &&
            strstr(decision, gates) != NULL && strstr(decision, policy) != NULL,
            decision);
    PQclear(result);
}

static PGresult *
execute_with_formats(PGconn *connection, const char *name, int count,
                     const char *const *values, const int *lengths,
                     const int *formats, const char *engine)
{
    char token[65];
    char setup[128];
    char witness_field[80];
    PGresult *result;
    PGresult *readback;
    const char *record;

    snprintf(token, sizeof(token), "%064x", ++execution_number);
    snprintf(setup, sizeof(setup), "SET pg_qi_witness.subject_token = '%s'", token);
    command(connection, setup);
    command(connection, "SET pg_qi_witness.armed = on");
    result = checked(connection, PQexecPrepared(connection, name, count, values,
                                                lengths, formats, 0), PGRES_TUPLES_OK);
    readback = read_value(connection, "SHOW pg_qi_witness.last_record");
    record = PQgetvalue(readback, 0, 0);
    snprintf(witness_field, sizeof(witness_field), ";engine=%s;", engine);
    require(strstr(record, ";status=success;") != NULL &&
            strstr(record, witness_field) != NULL && strstr(record, token) != NULL,
            record);
    PQclear(readback);
    command(connection, "SET pg_qi_witness.armed = off");
    expect_value(connection, "SHOW duckdb.force_execution", "off");
    return result;
}

static PGresult *
execute(PGconn *connection, const char *name, int count,
        const char *const *values, const char *engine)
{
    return execute_with_formats(connection, name, count, values, NULL, NULL, engine);
}

static void
compare(PGresult *actual, PGresult *baseline)
{
    int row;
    int column;

    require(PQntuples(actual) == PQntuples(baseline) &&
            PQnfields(actual) == PQnfields(baseline), "Result dimensions differ.");
    for (column = 0; column < PQnfields(baseline); column++)
    {
        require(PQftype(actual, column) == PQftype(baseline, column) &&
                PQfmod(actual, column) == PQfmod(baseline, column) &&
                PQfformat(actual, column) == PQfformat(baseline, column) &&
                PQftable(actual, column) == PQftable(baseline, column) &&
                PQftablecol(actual, column) == PQftablecol(baseline, column) &&
                strcmp(PQfname(actual, column), PQfname(baseline, column)) == 0,
                "Result column descriptors differ.");
        for (row = 0; row < PQntuples(baseline); row++)
            require(PQgetisnull(actual, row, column) == PQgetisnull(baseline, row, column) &&
                    PQgetlength(actual, row, column) == PQgetlength(baseline, row, column) &&
                    strcmp(PQgetvalue(actual, row, column), PQgetvalue(baseline, row, column)) == 0,
                    "Ordered result values differ.");
    }
    PQclear(actual);
}

static void
key_for_last_execution(PGconn *connection, char key[65])
{
    PGresult *result = read_value(connection, "SHOW pg_qi.bound_key");
    const char *value = PQgetvalue(result, 0, 0);

    require(strlen(value) == 64 && strspn(value, "0123456789abcdef") == 64,
            "Missing qualification key for a custom plan.");
    memcpy(key, value, 65);
    PQclear(result);
}

static void
expect_decline(PGconn *connection, const char *name, const char *sql,
               Oid type, const char *binding, long long expires_at)
{
    const char *values[] = {binding};
    PGresult *native;
    char key[65];
    char policy[128];

    PQclear(checked(connection, PQprepare(connection, name, sql, 1, &type),
                    PGRES_COMMAND_OK));
    native = execute(connection, name, 1, values, "postgres");
    key_for_last_execution(connection, key);
    snprintf(policy, sizeof(policy), "SET LOCAL pg_qi.bound_policy = '%s:20000:10000:%lld'",
             key, expires_at);
    command(connection, policy);
    compare(execute(connection, name, 1, values, "postgres"), native);
    expect_decision(connection, ";eligible=veto;safe=pass;beneficial=pass;",
                    ";policy=request_delegation;");
    PQclear(native);
}

static void
check_provider_role(PGconn *connection, long long expires_at)
{
    const Oid type = 23;
    const char *values[] = {"1"};
    PGresult *native;
    char key[65];
    char policy[128];

    command(connection, "SET plan_cache_mode = force_custom_plan");
    command(connection, "SET max_parallel_workers_per_gather = 0");
    command(connection, "SET duckdb.max_workers_per_postgres_scan = 0");
    command(connection, "SET pg_qi.routing_mode = bound");
    command(connection, "SET ROLE bound_reader");
    PQclear(checked(connection, PQprepare(connection, "role_lookup",
                                         "SELECT id FROM witness_fixture WHERE id = $1",
                                         1, &type), PGRES_COMMAND_OK));
    command(connection, "BEGIN READ WRITE");
    PQclear(execute(connection, "role_lookup", 1, values, "postgres"));
    expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                    ";policy=pass_through;");
    command(connection, "ROLLBACK");
    native = execute(connection, "role_lookup", 1, values, "postgres");
    key_for_last_execution(connection, key);
    command(connection, "RESET ROLE");
    snprintf(policy, sizeof(policy), "SET pg_qi.bound_policy = '%s:20000:10000:%lld'",
             key, expires_at);
    command(connection, policy);
    command(connection, "SET ROLE bound_reader");
    compare(execute(connection, "role_lookup", 1, values, "duckdb"), native);
    command(connection, "RESET ROLE");
    command(connection, "REVOKE bound_duckdb_access FROM bound_reader");
    command(connection, "SET ROLE bound_reader");
    compare(execute(connection, "role_lookup", 1, values, "postgres"), native);
    expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                    ";policy=pass_through;");
    expect_value(connection, "SHOW pg_qi.bound_key", "");
    PQclear(native);
    command(connection, "RESET ROLE");
}

int
main(int argc, char *argv[])
{
    static const Oid parameter_types[] = {23, 23};
    static const Oid text_type[] = {25};
    static const char lookup_label[] = "v-1\\path";
    static const char *const label[] = {lookup_label};
    static const char binary_seventeen[] = {0, 0, 0, 17};
    static const char *const binary_binding[] = {binary_seventeen, NULL};
    static const int binary_lengths[] = {4, 0};
    static const int binary_formats[] = {1, 1};
    PGconn *connection;
    PGresult *baseline[4];
    char keys[4][65];
    char policy[400];
    int index;
    long long expires_at = (long long) time(NULL) + 3600;

    require(argc == 2, "Supply a connection string for the disposable witness server.");
    connection = PQconnectdb(argv[1]);
    require(PQstatus(connection) == CONNECTION_OK, PQerrorMessage(connection));
    command(connection, "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY");
    command(connection, "SET LOCAL plan_cache_mode = force_custom_plan");
    command(connection, "SET LOCAL max_parallel_workers_per_gather = 0");
    command(connection, "SET LOCAL duckdb.max_workers_per_postgres_scan = 0");
    command(connection, "SET LOCAL duckdb.threads = 1");
    command(connection, "SET LOCAL work_mem = '4MB'");
    command(connection, "SET LOCAL pg_qi.routing_mode = bound");
    command(connection, "SET LOCAL pg_qi.bound_policy = ''");
    PQclear(checked(connection, PQprepare(connection, "pipeline", subject, 2,
                                            parameter_types), PGRES_COMMAND_OK));

    for (index = 0; index < 4; index++)
    {
        baseline[index] = execute(connection, "pipeline", 2, bindings[index], "postgres");
        require(PQntuples(baseline[index]) == 7, "Unexpected fixture output.");
        expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                        ";policy=pass_through;");
        key_for_last_execution(connection, keys[index]);
    }
    require(strcmp(keys[0], keys[1]) != 0 && strcmp(keys[1], keys[2]) != 0 &&
            strcmp(keys[2], keys[3]) != 0, "Qualification keys do not distinguish bindings.");

    /* Fixed timing inputs exercise the decision rules independently of runner speed. */
    snprintf(policy, sizeof(policy),
             "SET LOCAL pg_qi.bound_policy = '%s:20000:10000:%lld,%s:20000:10000:%lld,%s:6000:5900:%lld'",
             keys[0], expires_at, keys[1], expires_at, keys[2], expires_at);
    command(connection, policy);

    command(connection, "SET LOCAL duckdb.default_collation = 'nocase'");
    compare(execute(connection, "pipeline", 2, bindings[0], "postgres"), baseline[0]);
    expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                    ";policy=pass_through;");
    command(connection, "SET LOCAL duckdb.default_collation = ''");

    command(connection, "SAVEPOINT before_provider");
    compare(execute(connection, "pipeline", 2, bindings[0], "postgres"), baseline[0]);
    expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                    ";policy=pass_through;");
    command(connection, "RELEASE SAVEPOINT before_provider");

    for (index = 0; index < 2; index++)
    {
        compare(execute(connection, "pipeline", 2, bindings[index], "duckdb"), baseline[index]);
        expect_decision(connection, ";eligible=pass;safe=pass;beneficial=pass;",
                        ";policy=request_delegation;");
    }
    compare(execute_with_formats(connection, "pipeline", 2, binary_binding,
                                 binary_lengths, binary_formats, "duckdb"),
            baseline[0]);
    compare(execute(connection, "pipeline", 2, bindings[2], "postgres"), baseline[2]);
    expect_decision(connection, ";eligible=unknown;safe=pass;beneficial=veto;",
                    ";policy=pass_through;");
    compare(execute(connection, "pipeline", 2, bindings[3], "postgres"), baseline[3]);
    expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                    ";policy=pass_through;");
    compare(execute(connection, "pipeline", 2, bindings[0], "duckdb"), baseline[0]);

    command(connection, "SET LOCAL plan_cache_mode = force_generic_plan");
    for (index = 0; index < 2; index++)
    {
        compare(execute(connection, "pipeline", 2, bindings[index], "postgres"), baseline[index]);
        expect_value(connection, "SHOW pg_qi.bound_key", "");
    }
    expect_value(connection,
                 "SELECT generic_plans::text FROM pg_prepared_statements WHERE name = 'pipeline'", "2");

    command(connection, "SET LOCAL plan_cache_mode = force_custom_plan");
    command(connection, "SET LOCAL work_mem = '8MB'");
    compare(execute(connection, "pipeline", 2, bindings[0], "postgres"), baseline[0]);
    expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                    ";policy=pass_through;");
    command(connection, "SET LOCAL work_mem = '4MB'");
    command(connection, "SET LOCAL enable_indexscan = off");
    compare(execute(connection, "pipeline", 2, bindings[0], "postgres"), baseline[0]);
    expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                    ";policy=pass_through;");
    command(connection, "SET LOCAL enable_indexscan = on");
    snprintf(policy, sizeof(policy), "SET LOCAL pg_qi.bound_policy = '%s:20000:10000:1'", keys[0]);
    command(connection, policy);
    compare(execute(connection, "pipeline", 2, bindings[0], "postgres"), baseline[0]);
    expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                    ";policy=pass_through;");

    PQclear(checked(connection, PQprepare(connection, "volatile_filter",
                                         "SELECT count(*) FROM witness_fixture WHERE label = $1 AND random() >= 0",
                                         1, text_type), PGRES_COMMAND_OK));
    PQclear(execute(connection, "volatile_filter", 1, label, "postgres"));
    expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                    ";policy=pass_through;");
    expect_value(connection, "SHOW pg_qi.bound_key", "");

    {
        const int text_length[] = {sizeof(lookup_label) - 1};
        const int binary_format[] = {1};
        PGresult *native;
        char key[65];

        PQclear(checked(connection, PQprepare(connection, "text_lookup",
                                             "SELECT id, label FROM witness_fixture WHERE label = $1",
                                             1, text_type), PGRES_COMMAND_OK));
        native = execute(connection, "text_lookup", 1, label, "postgres");
        require(PQntuples(native) == 1 && strcmp(PQgetvalue(native, 0, 0), "1") == 0 &&
                PQftable(native, 0) != 0 && PQftablecol(native, 0) == 1,
                "The text lookup did not return the fixture column.");
        key_for_last_execution(connection, key);
        snprintf(policy, sizeof(policy), "SET LOCAL pg_qi.bound_policy = '%s:20000:10000:%lld'",
                 key, expires_at);
        command(connection, policy);
        compare(execute_with_formats(connection, "text_lookup", 1, label,
                                     text_length, binary_format, "duckdb"), native);
        expect_decision(connection, ";eligible=pass;safe=pass;beneficial=pass;",
                        ";policy=request_delegation;");
        if (PQserverVersion(connection) < 190000)
        {
            command(connection, "SET LOCAL standard_conforming_strings = off");
            compare(execute(connection, "text_lookup", 1, label, "postgres"), native);
            expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                            ";policy=pass_through;");
            command(connection, "SET LOCAL standard_conforming_strings = on");
        }
        PQclear(native);
    }

    {
        const Oid type = 17;
        const char *values[] = {"ab"};
        PGresult *native;
        char key[65];

        command(connection, "SET LOCAL bytea_output = escape");
        PQclear(checked(connection, PQprepare(connection, "bytea_lookup",
                                             "SELECT id FROM witness_fixture WHERE payload = $1",
                                             1, &type), PGRES_COMMAND_OK));
        native = execute(connection, "bytea_lookup", 1, values, "postgres");
        require(PQntuples(native) == 1 && strcmp(PQgetvalue(native, 0, 0), "1") == 0,
                "The binary value did not match the fixture.");
        key_for_last_execution(connection, key);
        snprintf(policy, sizeof(policy), "SET LOCAL pg_qi.bound_policy = '%s:20000:10000:%lld'",
                 key, expires_at);
        command(connection, policy);
        compare(execute(connection, "bytea_lookup", 1, values, "duckdb"), native);
        command(connection, "SET LOCAL bytea_output = hex");
        compare(execute(connection, "bytea_lookup", 1, values, "postgres"), native);
        expect_decision(connection, ";eligible=unknown;safe=unknown;beneficial=unknown;",
                        ";policy=pass_through;");
        PQclear(native);
    }

    if (PQserverVersion(connection) >= 170000)
    {
        const char *values[] = {"1"};

        PQclear(checked(connection, PQprepare(connection, "json_domain",
                      "SELECT JSON_VALUE('{\"n\":\"v\"}'::jsonb, '$.n' RETURNING bound_label)"
                      " IS NOT NULL AS present FROM witness_fixture WHERE id = $1",
                      1, parameter_types), PGRES_COMMAND_OK));
        PQclear(execute(connection, "json_domain", 1, values, "postgres"));
        expect_decision(connection, ";eligible=veto;safe=not_evaluated;beneficial=not_evaluated;",
                        ";policy=pass_through;");
    }

    /* The provider declines the catalogue read and changes the character type. */
    expect_decline(connection, "catalogue_read",
                   "SELECT count(*) FROM pg_catalog.pg_class WHERE oid > $1",
                   26, "0", expires_at);
    expect_decline(connection, "character_typmod",
                   "SELECT label::varchar(20) AS label FROM witness_fixture WHERE id = $1",
                   parameter_types[0], "1", expires_at);

    for (index = 0; index < 4; index++)
        PQclear(baseline[index]);
    command(connection, "ROLLBACK");
    check_provider_role(connection, expires_at);
    PQfinish(connection);
    puts("Prepared Bind routing and typed result parity passed.");
    return 0;
}
