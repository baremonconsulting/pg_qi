/* Minimal single-session benchmark driver for Spike 1 WP11. */
#define _POSIX_C_SOURCE 200809L

#include <libpq-fe.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static const char *simple_query = "SELECT count(*) FROM qi_bench_t WHERE v >= 0";
static const char *prepared_query = "SELECT count(*) FROM qi_bench_t WHERE v >= $1";

static void
die_result(PGconn *conn, PGresult *res, const char *operation)
{
    fprintf(stderr, "%s failed: %s", operation, PQerrorMessage(conn));
    if (res != NULL)
        PQclear(res);
    PQfinish(conn);
    exit(1);
}

static void
exec_ok(PGconn *conn, const char *sql)
{
    PGresult *res = PQexec(conn, sql);
    ExecStatusType status = PQresultStatus(res);

    if (status != PGRES_COMMAND_OK && status != PGRES_TUPLES_OK)
        die_result(conn, res, sql);
    PQclear(res);
}

static uint64_t
elapsed_us(const struct timespec *start, const struct timespec *end)
{
    int64_t seconds = (int64_t) end->tv_sec - (int64_t) start->tv_sec;
    int64_t nanos = (int64_t) end->tv_nsec - (int64_t) start->tv_nsec;

    return (uint64_t) (seconds * 1000000 + nanos / 1000);
}

int
main(int argc, char **argv)
{
    PGconn *conn;
    PGresult *res;
    FILE *latencies;
    const char *path;
    int requests;
    int i;

    if (argc != 5)
    {
        fprintf(stderr, "usage: %s CONNINFO simple|custom|generic REQUESTS LATENCY_FILE\n",
                argv[0]);
        return 2;
    }
    path = argv[2];
    requests = atoi(argv[3]);
    if (requests <= 0 || (strcmp(path, "simple") != 0 &&
                          strcmp(path, "custom") != 0 &&
                          strcmp(path, "generic") != 0))
        return 2;

    latencies = fopen(argv[4], "w");
    if (latencies == NULL)
    {
        perror(argv[4]);
        return 1;
    }
    conn = PQconnectdb(argv[1]);
    if (PQstatus(conn) != CONNECTION_OK)
        die_result(conn, NULL, "connect");

    exec_ok(conn, "SET max_parallel_workers_per_gather=0");
    exec_ok(conn, "SET jit=off");
    exec_ok(conn, "SET track_io_timing=off");
    if (strcmp(path, "custom") == 0)
        exec_ok(conn, "SET plan_cache_mode=force_custom_plan");
    else if (strcmp(path, "generic") == 0)
        exec_ok(conn, "SET plan_cache_mode=force_generic_plan");

    if (strcmp(path, "simple") != 0)
    {
        res = PQprepare(conn, "wp11_stmt", prepared_query, 1, NULL);
        if (PQresultStatus(res) != PGRES_COMMAND_OK)
            die_result(conn, res, "prepare");
        PQclear(res);
    }

    for (i = 0; i < requests; i++)
    {
        struct timespec start;
        struct timespec end;

        clock_gettime(CLOCK_MONOTONIC, &start);
        if (strcmp(path, "simple") == 0)
            res = PQexec(conn, simple_query);
        else
        {
            const char *values[1] = {"0"};

            res = PQexecPrepared(conn, "wp11_stmt", 1, values,
                                 NULL, NULL, 0);
        }
        clock_gettime(CLOCK_MONOTONIC, &end);
        if (PQresultStatus(res) != PGRES_TUPLES_OK)
            die_result(conn, res, "benchmark query");
        PQclear(res);
        fprintf(latencies, "%llu\n",
                (unsigned long long) elapsed_us(&start, &end));
    }
    fclose(latencies);

    /* Do not contaminate planner timing with the metadata query itself. */
    exec_ok(conn, "SET pg_qi.spike_benchmark_collect=off");
    if (strcmp(path, "simple") == 0)
        printf("generic=0 custom=0\n");
    else
    {
        res = PQexec(conn,
                     "SELECT generic_plans,custom_plans "
                     "FROM pg_prepared_statements WHERE name='wp11_stmt'");
        if (PQresultStatus(res) != PGRES_TUPLES_OK || PQntuples(res) != 1)
            die_result(conn, res, "plan-cache metadata");
        printf("generic=%s custom=%s\n",
               PQgetvalue(res, 0, 0), PQgetvalue(res, 0, 1));
        PQclear(res);
    }
    PQfinish(conn);
    return 0;
}
