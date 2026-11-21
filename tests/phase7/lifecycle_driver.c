/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libpq-fe.h>

static void
fail(PGconn *connection, const char *message)
{
    fprintf(stderr, "%s: %s", message, PQerrorMessage(connection));
    PQfinish(connection);
    exit(EXIT_FAILURE);
}

static PGresult *
command(PGconn *connection, const char *sql, ExecStatusType expected)
{
    PGresult *result = PQexec(connection, sql);

    if (result == NULL || PQresultStatus(result) != expected)
    {
        if (result != NULL)
            PQclear(result);
        fail(connection, sql);
    }
    return result;
}

static char *
snapshot(PGconn *connection)
{
    PGresult *result = command(connection, "SHOW pg_qi.decision_v2",
                               PGRES_TUPLES_OK);
    const char *source = PQgetvalue(result, 0, 0);
    size_t length = strlen(source);
    char *value = malloc(length + 1);

    if (value == NULL)
        fail(connection, "copy snapshot");
    memcpy(value, source, length + 1);
    PQclear(result);
    if (strstr(value, ";lifecycle=fresh_execution") == NULL)
    {
        fprintf(stderr, "snapshot lacks proven lifecycle: %s\n", value);
        free(value);
        PQfinish(connection);
        exit(EXIT_FAILURE);
    }
    return value;
}

static void
execute_prepared(PGconn *connection, const char *name, const char *value)
{
    const char *values[1] = {value};
    PGresult *result = PQexecPrepared(connection, name, 1, values, NULL, NULL, 0);

    if (result == NULL || PQresultStatus(result) != PGRES_TUPLES_OK)
    {
        if (result != NULL)
            PQclear(result);
        fail(connection, name);
    }
    PQclear(result);
}

int
main(void)
{
    const size_t large_length = 16 * 1024 * 1024;
    PGconn *connection = PQconnectdb("dbname=postgres");
    PGresult *result;
    char *large_value;
    const char *large_values[1];
    char *generic_first;
    char *generic_second;
    char *generic_replanned;
    char *custom_first;
    char *custom_second;
    char *large_snapshot;

    if (PQstatus(connection) != CONNECTION_OK)
        fail(connection, "connect");
    PQclear(command(connection, "SET pg_qi.routing_mode = shadow_p0",
                    PGRES_COMMAND_OK));
    PQclear(command(connection, "SET plan_cache_mode = force_generic_plan",
                    PGRES_COMMAND_OK));
    result = PQprepare(connection, "generic_q",
                       "SELECT id FROM lifecycle_probe WHERE id = $1", 1, NULL);
    if (PQresultStatus(result) != PGRES_COMMAND_OK)
    {
        PQclear(result);
        fail(connection, "PQprepare generic_q");
    }
    PQclear(result);
    execute_prepared(connection, "generic_q", "1");
    generic_first = snapshot(connection);
    execute_prepared(connection, "generic_q", "1");
    generic_second = snapshot(connection);
    if (strcmp(generic_first, generic_second) != 0)
        fail(connection, "cached generic execution published a new event");
    PQclear(command(connection, "ALTER TABLE lifecycle_probe ADD COLUMN marker integer",
                    PGRES_COMMAND_OK));
    execute_prepared(connection, "generic_q", "1");
    generic_replanned = snapshot(connection);
    if (strcmp(generic_second, generic_replanned) == 0)
        fail(connection, "invalidated generic plan did not publish a new event");

    PQclear(command(connection, "SET plan_cache_mode = force_custom_plan",
                    PGRES_COMMAND_OK));
    result = PQprepare(connection, "custom_q",
                       "SELECT id FROM lifecycle_probe WHERE id = $1", 1, NULL);
    if (PQresultStatus(result) != PGRES_COMMAND_OK)
    {
        PQclear(result);
        fail(connection, "PQprepare custom_q");
    }
    PQclear(result);
    execute_prepared(connection, "custom_q", "1");
    custom_first = snapshot(connection);
    execute_prepared(connection, "custom_q", "1");
    custom_second = snapshot(connection);
    if (strcmp(custom_first, custom_second) == 0)
        fail(connection, "fresh custom plan did not publish a new event");

    large_value = malloc(large_length + 1);
    if (large_value == NULL)
        fail(connection, "allocate large parameter");
    memset(large_value, 'x', large_length);
    large_value[large_length] = '\0';
    large_values[0] = large_value;
    result = PQexecParams(connection, "SELECT length($1::text)", 1, NULL,
                          large_values, NULL, NULL, 0);
    if (result == NULL || PQresultStatus(result) != PGRES_TUPLES_OK ||
        strcmp(PQgetvalue(result, 0, 0), "16777216") != 0)
    {
        if (result != NULL)
            PQclear(result);
        free(large_value);
        fail(connection, "large parameter execution");
    }
    PQclear(result);
    free(large_value);
    large_snapshot = snapshot(connection);
    if (strcmp(custom_second, large_snapshot) == 0)
        fail(connection, "large parameter plan did not publish a new event");

    free(generic_first);
    free(generic_second);
    free(generic_replanned);
    free(custom_first);
    free(custom_second);
    free(large_snapshot);
    PQfinish(connection);
    puts("PQ-39 libpq lifecycle verification PASS");
    return EXIT_SUCCESS;
}
