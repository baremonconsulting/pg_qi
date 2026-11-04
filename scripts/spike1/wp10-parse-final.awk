BEGIN { bad = 0 }
/PG_QI_FINAL app=wp10_pgbench / {
    pid = pushed = popped = unwound = depth = cleanup = pgplans = duckplans = ""
    for (i = 1; i <= NF; i++) {
        split($i, pair, "=")
        if (pair[1] == "pid") pid = pair[2]
        else if (pair[1] == "pushed") pushed = pair[2]
        else if (pair[1] == "popped") popped = pair[2]
        else if (pair[1] == "unwound") unwound = pair[2]
        else if (pair[1] == "depth") depth = pair[2]
        else if (pair[1] == "cleanup_broken") cleanup = pair[2]
        else if (pair[1] == "pg_plans") pgplans = pair[2]
        else if (pair[1] == "duckdb_plans") duckplans = pair[2]
    }
    # pg_duckdb helpers inherit application_name but never complete a
    # delegated plan.  WP10 evaluates only the eight persistent pgbench
    # client backends, each of which alternates both plan types.
    if (duckplans > 0) {
        valid = (pid != "" && pushed == popped + unwound && depth == 0 &&
                 cleanup == "false" && pgplans > 0)
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", pid, pushed, popped,
               unwound, depth, cleanup, pgplans, duckplans
        if (!valid || seen[pid]++) bad = 1
        rows++
    }
}
END {
    if (rows != 8 || bad) exit 1
}
