#define _POSIX_C_SOURCE 200809L

#include <stdbool.h>

#define main phase5_protocol_driver_main
#include "../phase5/protocol_driver.c"
#undef main

static void
phase7_usage(const char *program)
{
    fprintf(stderr,
            "usage: %s --mode exporter|arm --conninfo STR [arm options]\n",
            program);
}

static int
safe_field(const char *value)
{
    return value != NULL && strchr(value, '\t') == NULL &&
        strchr(value, '\r') == NULL && strchr(value, '\n') == NULL;
}

static int
run_exporter(const char *conninfo, uint64_t deadline_ms)
{
    PGconn *connection;
    char *token = NULL;
    char *identity = NULL;
    int result = 1;
    int control;
    int64_t deadline = now_ms() + (int64_t) deadline_ms;

    connection = connect_bounded(conninfo, deadline);
    if (connection == NULL ||
            !commands_bounded(connection,
                              "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
                              deadline))
        goto done;
    token = query_scalar(connection, "SELECT pg_export_snapshot()", deadline);
    identity = query_scalar(connection,
        "SELECT pg_current_snapshot()::text || E'\\t' || pg_backend_pid()::text || "
        "E'\\t' || current_database() || E'\\t' || current_user || E'\\t' || "
        "(SELECT oid::text FROM pg_database WHERE datname=current_database()) || E'\\t' || "
        "(SELECT oid::text FROM pg_roles WHERE rolname=current_user)", deadline);
    if (!safe_field(token) || identity == NULL || strchr(identity, '\n') != NULL)
        goto done;
    printf("EXPORT\t%s\t%s\n", token, identity);
    fflush(stdout);
    control = getchar();
    if (control == EOF && ferror(stdin))
        goto done;
    if (!commands_bounded(connection, "ROLLBACK", now_ms() + (int64_t) deadline_ms))
        goto done;
    result = 0;
done:
    free(token);
    free(identity);
    if (connection != NULL)
        PQfinish(connection);
    return result;
}

static int
run_command(const char *conninfo, const char *sql_path, uint64_t deadline_ms)
{
    PGconn *connection = NULL;
    char *sql = read_file(sql_path);
    int result = 1;
    int64_t deadline = now_ms() + (int64_t) deadline_ms;

    if (sql == NULL || !*sql || (connection = connect_bounded(conninfo, deadline)) == NULL)
        goto done;
    if (!commands_bounded(connection, sql, deadline))
        goto done;
    puts("COMMAND\tcomplete");
    fflush(stdout);
    result = 0;
done:
    free(sql);
    if (connection != NULL)
        PQfinish(connection);
    return result;
}

static char *
snapshot_import_sql(PGconn *connection, const char *snapshot)
{
    char *literal = PQescapeLiteral(connection, snapshot, strlen(snapshot));
    char *sql;
    size_t length;

    if (literal == NULL)
        return NULL;
    length = strlen(literal) + 32;
    sql = malloc(length);
    if (sql == NULL || snprintf(sql, length, "SET TRANSACTION SNAPSHOT %s", literal) >=
            (int) length)
    {
        free(sql);
        sql = NULL;
    }
    PQfreemem(literal);
    return sql;
}

static const char *
arm_configuration(const char *arm)
{
    if (strcmp(arm, "native") == 0)
        return "SET LOCAL max_parallel_workers_per_gather=0; "
               "SET LOCAL pg_qi.enabled=on; SET LOCAL pg_qi.routing_mode=off; "
               "SET LOCAL duckdb.force_execution=off";
    if (strcmp(arm, "forced") == 0)
        return "SET LOCAL max_parallel_workers_per_gather=0; "
               "SET LOCAL pg_qi.enabled=on; SET LOCAL pg_qi.routing_mode=off; "
               "SET LOCAL duckdb.force_execution=on";
    if (strcmp(arm, "shadow") == 0)
        return "SET LOCAL max_parallel_workers_per_gather=0; "
               "SET LOCAL pg_qi.enabled=on; SET LOCAL pg_qi.routing_mode=shadow_p0; "
               "SET LOCAL duckdb.force_execution=off";
    return NULL;
}

static int
drain_after_cancel(PGconn *connection, int64_t deadline)
{
    PGresult *result;
    for (;;)
    {
        while (PQisBusy(connection))
        {
            if (now_ms() >= deadline || wait_socket(connection, POLLIN, deadline) <= 0 ||
                    !PQconsumeInput(connection))
                return 0;
        }
        if (now_ms() >= deadline)
            return 0;
        result = PQgetResult(connection);
        if (result == NULL)
            break;
        PQclear(result);
    }
    return PQstatus(connection) == CONNECTION_OK;
}

static int
run_arm(const char *conninfo, const char *arm, const char *snapshot,
        const char *sql_path, const char *spool_path, uint64_t deadline_ms,
        bool import_snapshot, const char *subject_token, Limits *limits)
{
    PGconn *connection = NULL;
    FILE *spool = NULL;
    char *sql = NULL;
    char *import = NULL;
    char *identity = NULL;
    char *v2_before = NULL;
    char *v2_after = NULL;
    char *witness_before = NULL;
    char *witness_after = NULL;
    const char *configuration = arm_configuration(arm);
    const char *reason = "setup";
    char sqlstate[6] = "";
    char witness_configuration[160];
    int controlled_error = 0;
    int complete = 0;
    int64_t deadline = now_ms() + (int64_t) deadline_ms;

    if (configuration == NULL || (sql = read_file(sql_path)) == NULL || !*sql ||
            (spool = open_exclusive(spool_path)) == NULL)
        goto done;
    reason = "connect";
    connection = connect_bounded(conninfo, deadline);
    if (connection == NULL)
        goto done;
    reason = "snapshot_import";
    if (!commands_bounded(connection,
                          "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", deadline) ||
            (import_snapshot && ((import = snapshot_import_sql(connection, snapshot)) == NULL ||
                                 !commands_bounded(connection, import, deadline))) ||
            !commands_bounded(connection, "SET LOCAL duckdb.force_execution=off", deadline))
        goto done;

    /* This is deliberately the first snapshot-taking statement in the arm. */
    reason = "identity";
    identity = query_scalar(connection,
        "SELECT pg_current_snapshot()::text || E'\\t' || pg_backend_pid()::text || "
        "E'\\t' || current_database() || E'\\t' || current_user || E'\\t' || "
        "(SELECT oid::text FROM pg_database WHERE datname=current_database()) || E'\\t' || "
        "(SELECT oid::text FROM pg_roles WHERE rolname=current_user)", deadline);
    if (identity == NULL)
        goto done;
    reason = "configuration";
    if (!commands_bounded(connection, configuration, deadline))
        goto done;
    reason = "before_readback";
    v2_before = show_scalar(connection, "pg_qi.decision_v2", deadline);
    witness_before = show_scalar(connection, "pg_qi_witness.last_record", deadline);
    if (identity == NULL || !safe_field(v2_before) || !safe_field(witness_before))
        goto done;
    if (subject_token == NULL || strlen(subject_token) != 64 ||
            snprintf(witness_configuration, sizeof(witness_configuration),
                     "SET LOCAL pg_qi_witness.subject_token='%s'; "
                     "SET LOCAL pg_qi_witness.armed=on", subject_token) >=
                (int) sizeof(witness_configuration) ||
            !commands_bounded(connection, witness_configuration, deadline))
    {
        reason = "witness_arm";
        goto done;
    }

    interval_start(limits);
    if (!send_subject(connection, sql, "extended", NULL, deadline) ||
            !flush_bounded(connection, deadline))
    {
        reason = "send_error";
        interval_finish(limits, 2);
        goto rollback;
    }
    if (!stream(connection, spool, limits, deadline, 2, &reason, sqlstate))
    {
        interval_finish(limits, !strcmp(reason, "query_deadline") ? 3 : 2);
        controlled_error = !strcmp(reason, "server_error") &&
            strcmp(sqlstate, "57014") != 0;
        if (!controlled_error && (!strcmp(reason, "query_deadline") ||
                !strncmp(reason, "max_", 4)))
        {
            cancel_owned_query(connection, limits, deadline_ms);
            if (!limits->cancel_succeeded || !drain_after_cancel(
                    connection, now_ms() + (int64_t) deadline_ms))
            {
                reason = "cancel_or_drain_failed";
                goto done;
            }
        }
        goto rollback;
    }
    interval_finish(limits, 1);
    complete = 1;
    if (!commands_bounded(connection, "SET LOCAL pg_qi_witness.armed=off", deadline))
    {
        complete = 0;
        reason = "witness_disarm";
    }

rollback:
    if (fflush(spool) || fsync(fileno(spool)))
    {
        complete = 0;
        controlled_error = 0;
        reason = "spool_sync";
    }
    if (!commands_bounded(connection, "ROLLBACK", now_ms() + (int64_t) deadline_ms))
    {
        complete = 0;
        controlled_error = 0;
        reason = "rollback";
        goto done;
    }
    v2_after = show_scalar(connection, "pg_qi.decision_v2",
                           now_ms() + (int64_t) deadline_ms);
    witness_after = show_scalar(connection, "pg_qi_witness.last_record",
                                now_ms() + (int64_t) deadline_ms);
    if (!safe_field(v2_after) || !safe_field(witness_after))
    {
        complete = 0;
        controlled_error = 0;
        reason = "readback";
        goto done;
    }
    if (complete)
        reason = "ok";
    else if (controlled_error)
        complete = 1;

done:
    printf("ARM\t%s\t%s\t%s\t%llu\t%s\t%s\t%s\t%s\t%s\t%s\n",
           complete ? "complete" : "incomplete", reason, sqlstate,
           (unsigned long long) limits->rows,
           identity != NULL ? identity : "\t\t\t\t\t",
           v2_before != NULL ? v2_before : "",
           v2_after != NULL ? v2_after : "",
           witness_before != NULL ? witness_before : "",
           witness_after != NULL ? witness_after : "",
           controlled_error ? "error" : (complete ? "complete" : "incomplete"));
    fflush(stdout);
    if (connection != NULL)
        PQfinish(connection);
    if (spool != NULL)
        fclose(spool);
    free(sql);
    free(import);
    free(identity);
    free(v2_before);
    free(v2_after);
    free(witness_before);
    free(witness_after);
    return complete ? 0 : 1;
}

int
main(int argc, char **argv)
{
    const char *mode = NULL;
    const char *conninfo = NULL;
    const char *arm = NULL;
    const char *snapshot = NULL;
    const char *sql_path = NULL;
    const char *spool_path = NULL;
    const char *subject_token = NULL;
    bool import_snapshot = true;
    uint64_t deadline_ms = 30000;
    uint64_t value;
    Limits limits = {0};
    int index;

    limits.max_rows = 10000000;
    limits.max_bytes = 64 * 1024 * 1024;
    limits.max_spool = 64 * 1024 * 1024;
    limits.max_messages = 10000001;
    limits.max_columns = 64;
    limits.max_field = 1024 * 1024;
    for (index = 1; index < argc; index++)
    {
        const char *option = argv[index];
        const char *argument;

        if (++index >= argc)
        {
            phase7_usage(argv[0]);
            return 64;
        }
        argument = argv[index];
        if (!strcmp(option, "--mode")) mode = argument;
        else if (!strcmp(option, "--conninfo")) conninfo = argument;
        else if (!strcmp(option, "--arm")) arm = argument;
        else if (!strcmp(option, "--snapshot")) snapshot = argument;
        else if (!strcmp(option, "--sql-file")) sql_path = argument;
        else if (!strcmp(option, "--spool")) spool_path = argument;
        else if (!strcmp(option, "--subject-token")) subject_token = argument;
        else if (!strcmp(option, "--snapshot-mode") &&
                 (!strcmp(argument, "import") || !strcmp(argument, "skip")))
            import_snapshot = !strcmp(argument, "import");
        else if (!strcmp(option, "--deadline-ms") &&
                 parse_u64(argument, 3600000, &value)) deadline_ms = value;
        else
        {
            phase7_usage(argv[0]);
            return 64;
        }
    }
    if (mode == NULL || conninfo == NULL)
    {
        phase7_usage(argv[0]);
        return 64;
    }
    if (!strcmp(mode, "exporter"))
        return run_exporter(conninfo, deadline_ms);
    if (!strcmp(mode, "command") && sql_path != NULL)
        return run_command(conninfo, sql_path, deadline_ms);
    if (!strcmp(mode, "arm") && arm != NULL && snapshot != NULL &&
            sql_path != NULL && spool_path != NULL && subject_token != NULL)
        return run_arm(conninfo, arm, snapshot, sql_path, spool_path,
                       deadline_ms, import_snapshot, subject_token, &limits);
    phase7_usage(argv[0]);
    return 64;
}
