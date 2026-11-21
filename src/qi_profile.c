/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "fmgr.h"
#include "miscadmin.h"
#include "pg_qi/qi_build_identity.h"
#include "pg_qi/qi_decision_v2.h"
#include "pg_qi/qi_profile.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/timestamp.h"

typedef struct QiInstalledProfile
{
    QiProfileParseReason parse_reason;
    QiProfileData data;
    uint64 install_sequence;
    uint32 fingerprint;
} QiInstalledProfile;

static char *qi_profile_text = NULL;
static char *qi_site_build_id = NULL;
static QiInstalledProfile qi_installed_profile = {
    .parse_reason = QI_PROFILE_PARSE_MISSING
};

static bool
qi_profile_zero_hex(const char *value, Size length)
{
    Size index;

    for (index = 0; index < length; index++)
        if (value[index] != '0')
            return false;
    return value[length] == '\0';
}

static bool
qi_profile_hex_string(const char *value, Size length)
{
    Size index;

    if (value == NULL || strlen(value) != length)
        return false;
    for (index = 0; index < length; index++)
        if (!((value[index] >= '0' && value[index] <= '9') ||
              (value[index] >= 'a' && value[index] <= 'f')))
            return false;
    return true;
}

static void
qi_profile_assign(const char *new_value, void *extra)
{
    QiInstalledProfile candidate;
    Size length = new_value == NULL ? 0 : strlen(new_value);

    (void) extra;
    MemSet(&candidate, 0, sizeof(candidate));
    candidate.parse_reason = qi_profile_parse(new_value, length,
                                              &candidate.data);
    candidate.fingerprint = length <= QI_PROFILE_MAX_BYTES
        ? qi_profile_crc32c((const unsigned char *)
                                (new_value == NULL ? "" : new_value),
                            length) : 0;
    candidate.install_sequence = qi_installed_profile.install_sequence;
    if (candidate.install_sequence != PG_UINT64_MAX)
        candidate.install_sequence++;
    qi_installed_profile = candidate;
}

static QiProfileReason
qi_profile_parse_reason(QiProfileParseReason reason)
{
    switch (reason)
    {
        case QI_PROFILE_PARSE_OK: return QI_PROFILE_PRESENT;
        case QI_PROFILE_PARSE_MISSING: return QI_PROFILE_MISSING;
        case QI_PROFILE_PARSE_OVERSIZE: return QI_PROFILE_OVERSIZE;
        case QI_PROFILE_PARSE_MALFORMED: return QI_PROFILE_MALFORMED;
        case QI_PROFILE_PARSE_UNSUPPORTED_FORMAT:
            return QI_PROFILE_UNSUPPORTED_FORMAT;
        case QI_PROFILE_PARSE_CHECKSUM_MISMATCH:
            return QI_PROFILE_CHECKSUM_MISMATCH;
        case QI_PROFILE_PARSE_INVALID_INTERVAL:
            return QI_PROFILE_INVALID_INTERVAL;
        case QI_PROFILE_PARSE_INVALID_LIMIT:
            return QI_PROFILE_INVALID_LIMIT;
    }
    return QI_PROFILE_MALFORMED;
}

QiProfileEvidence
qi_profile_not_observed(void)
{
    QiProfileEvidence evidence;

    MemSet(&evidence, 0, sizeof(evidence));
    evidence.reason = QI_PROFILE_NOT_OBSERVED;
    evidence.envelope_hypothetical = true;
    return evidence;
}

static QiProfileEvidence
qi_profile_classify(const QiInstalledProfile *installed, TimestampTz now,
                    const char *site_build_id, const char *pg_qi_build_id,
                    const char *postgres_build_id,
                    const char *pg_duckdb_sha, const char *duckdb_sha,
                    uint32 postgres_version_num)
{
    QiProfileEvidence evidence = qi_profile_not_observed();
    const QiProfileData *profile;
    pg_time_t now_unix;

    evidence.reason = qi_profile_parse_reason(installed->parse_reason);
    evidence.install_sequence = installed->install_sequence;
    evidence.fingerprint = installed->fingerprint;
    if (installed->parse_reason != QI_PROFILE_PARSE_OK)
        return evidence;

    profile = &installed->data;
    evidence.generation = profile->generation;
    evidence.expires_at_unix = profile->expires_at_unix;
    if (profile->postgres_version_num != postgres_version_num)
        evidence.reason = QI_PROFILE_WRONG_POSTGRES_BUILD;
    else if (!qi_profile_hex_string(postgres_build_id,
                                    QI_PROFILE_SHA256_HEX_BYTES) ||
             qi_profile_zero_hex(postgres_build_id,
                                 QI_PROFILE_SHA256_HEX_BYTES) ||
             strcmp(profile->postgres_build, postgres_build_id) != 0)
        evidence.reason = QI_PROFILE_WRONG_POSTGRES_BUILD;
    else if (!qi_profile_hex_string(pg_qi_build_id,
                                    QI_PROFILE_SHA1_HEX_BYTES) ||
             qi_profile_zero_hex(pg_qi_build_id,
                                 QI_PROFILE_SHA1_HEX_BYTES) ||
             strcmp(profile->pg_qi_build, pg_qi_build_id) != 0)
        evidence.reason = QI_PROFILE_WRONG_PG_QI_BUILD;
    else if (strcmp(profile->pg_duckdb_sha, pg_duckdb_sha) != 0 ||
             strcmp(profile->duckdb_sha, duckdb_sha) != 0)
        evidence.reason = QI_PROFILE_WRONG_PROVIDER_BUILD;
    else if (!qi_profile_hex_string(site_build_id,
                                    QI_PROFILE_SHA256_HEX_BYTES) ||
             strcmp(profile->site_build, site_build_id) != 0)
        evidence.reason = QI_PROFILE_WRONG_SITE_BUILD;
    else if (profile->contract_version !=
                 QI_DECISION_V2_CONTRACT_VERSION ||
             profile->rule_version != QI_DECISION_V2_RULE_VERSION ||
             profile->model_version != QI_DECISION_V2_MODEL_VERSION ||
             profile->certificate_version !=
                 QI_DECISION_V2_CERTIFICATE_VERSION)
        evidence.reason = QI_PROFILE_WRONG_VERSIONS;
    else
    {
        now_unix = timestamptz_to_time_t(now);
        if (now_unix < 0 || (uint64) now_unix < profile->issued_at_unix)
            evidence.reason = QI_PROFILE_NOT_YET_VALID;
        else if ((uint64) now_unix >= profile->expires_at_unix)
            evidence.reason = QI_PROFILE_EXPIRED;
        else
        {
            evidence.reason = QI_PROFILE_PRESENT;
            evidence.profile_present = true;
        }
    }
    return evidence;
}

QiProfileEvidence
qi_profile_observe_model(TimestampTz now, QiProfileModel *model)
{
    QiInstalledProfile installed = qi_installed_profile;
    QiProfileEvidence evidence;

    if (model != NULL)
        MemSet(model, 0, sizeof(*model));
    evidence = qi_profile_classify(&installed, now, qi_site_build_id,
                                   PG_QI_BUILD_ID,
                                   PG_QI_POSTGRES_BUILD_ID,
                                   QI_EXPECTED_PG_DUCKDB_SHA,
                                   QI_EXPECTED_DUCKDB_SHA, PG_VERSION_NUM);
    if (model != NULL && evidence.profile_present)
    {
        model->valid = true;
        model->install_sequence = evidence.install_sequence;
        model->fingerprint = evidence.fingerprint;
        model->memory_bytes = installed.data.memory_bytes;
        model->temp_bytes = installed.data.temp_bytes;
        model->threads = installed.data.threads;
        model->concurrency = installed.data.concurrency;
        model->fixed_overhead_us = installed.data.fixed_overhead_us;
        model->postgres_ns_per_cost = installed.data.postgres_ns_per_cost;
        model->scan_ns_per_kb = installed.data.scan_ns_per_kb;
        model->conversion_ns_per_row =
            installed.data.conversion_ns_per_row;
        model->output_ns_per_byte = installed.data.output_ns_per_byte;
        model->p95_dispersion_us = installed.data.p95_dispersion_us;
        model->margin_ppm = installed.data.margin_ppm;
    }
    return evidence;
}

QiProfileEvidence
qi_profile_observe(TimestampTz now)
{
    return qi_profile_observe_model(now, NULL);
}

const char *
qi_profile_reason_name(QiProfileReason reason)
{
    switch (reason)
    {
        case QI_PROFILE_NOT_OBSERVED: return "not_observed";
        case QI_PROFILE_MISSING: return "missing";
        case QI_PROFILE_OVERSIZE: return "oversize";
        case QI_PROFILE_MALFORMED: return "malformed";
        case QI_PROFILE_UNSUPPORTED_FORMAT: return "unsupported_format";
        case QI_PROFILE_CHECKSUM_MISMATCH: return "checksum_mismatch";
        case QI_PROFILE_INVALID_INTERVAL: return "invalid_interval";
        case QI_PROFILE_INVALID_LIMIT: return "invalid_limit";
        case QI_PROFILE_WRONG_POSTGRES_BUILD: return "wrong_postgres_build";
        case QI_PROFILE_WRONG_PG_QI_BUILD: return "wrong_pg_qi_build";
        case QI_PROFILE_WRONG_PROVIDER_BUILD: return "wrong_provider_build";
        case QI_PROFILE_WRONG_SITE_BUILD: return "wrong_site_build";
        case QI_PROFILE_WRONG_VERSIONS: return "wrong_versions";
        case QI_PROFILE_NOT_YET_VALID: return "not_yet_valid";
        case QI_PROFILE_EXPIRED: return "expired";
        case QI_PROFILE_PRESENT: return "present";
    }
    return "invalid";
}

void
qi_profile_init_gucs(void)
{
    if (process_shared_preload_libraries_in_progress)
        DefineCustomStringVariable(
            "pg_qi.site_build_id",
            "Binds profiles to one deployment site build.",
            NULL, &qi_site_build_id, "", PGC_POSTMASTER,
            GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
            NULL, NULL, NULL);
    DefineCustomStringVariable("pg_qi.model_profile",
                               "Installs one immutable model/resource profile.",
                               NULL, &qi_profile_text, "", PGC_SIGHUP,
                               GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL |
                                   GUC_NOT_IN_SAMPLE,
                               NULL, qi_profile_assign, NULL);
}

#if PG_QI_TESTING
Datum
qi_test_profile_parse(PG_FUNCTION_ARGS)
{
    text *value = PG_GETARG_TEXT_PP(0);
    QiProfileData parsed;
    QiProfileParseReason reason = qi_profile_parse(
        VARDATA_ANY(value), VARSIZE_ANY_EXHDR(value), &parsed);

    PG_RETURN_TEXT_P(cstring_to_text(qi_profile_parse_reason_name(reason)));
}

Datum
qi_test_profile_contract(PG_FUNCTION_ARGS)
{
    QiInstalledProfile installed;
    QiProfileEvidence evidence;
    TimestampTz now = time_t_to_timestamptz((pg_time_t) 2000);
    bool ok = true;

    MemSet(&installed, 0, sizeof(installed));
    installed.parse_reason = QI_PROFILE_PARSE_MISSING;
    evidence = qi_profile_classify(
        &installed, now, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        QI_EXPECTED_PG_DUCKDB_SHA, QI_EXPECTED_DUCKDB_SHA, PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_MISSING &&
        !evidence.profile_present && !evidence.limits_verified &&
        evidence.envelope_hypothetical;

    installed.parse_reason = QI_PROFILE_PARSE_OK;
    installed.data.generation = 7;
    installed.data.postgres_version_num = PG_VERSION_NUM;
    strlcpy(installed.data.postgres_build,
            "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            sizeof(installed.data.postgres_build));
    strlcpy(installed.data.pg_qi_build,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            sizeof(installed.data.pg_qi_build));
    strlcpy(installed.data.pg_duckdb_sha, QI_EXPECTED_PG_DUCKDB_SHA,
            sizeof(installed.data.pg_duckdb_sha));
    strlcpy(installed.data.duckdb_sha, QI_EXPECTED_DUCKDB_SHA,
            sizeof(installed.data.duckdb_sha));
    strlcpy(installed.data.site_build,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sizeof(installed.data.site_build));
    installed.data.contract_version = QI_DECISION_V2_CONTRACT_VERSION;
    installed.data.rule_version = QI_DECISION_V2_RULE_VERSION;
    installed.data.model_version = QI_DECISION_V2_MODEL_VERSION;
    installed.data.certificate_version =
        QI_DECISION_V2_CERTIFICATE_VERSION;
    installed.data.issued_at_unix = 1000;
    installed.data.expires_at_unix = 3000;
    evidence = qi_profile_classify(
        &installed, now, installed.data.site_build,
        installed.data.pg_qi_build, installed.data.postgres_build,
        installed.data.pg_duckdb_sha,
        installed.data.duckdb_sha, PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_PRESENT &&
        evidence.profile_present && !evidence.limits_verified &&
        evidence.envelope_hypothetical && evidence.generation == 7;

    installed.data.expires_at_unix = 2000;
    evidence = qi_profile_classify(
        &installed, now, installed.data.site_build,
        installed.data.pg_qi_build, installed.data.postgres_build,
        installed.data.pg_duckdb_sha,
        installed.data.duckdb_sha, PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_EXPIRED &&
        !evidence.profile_present;
    installed.data.expires_at_unix = 3000;
    evidence = qi_profile_classify(
        &installed, now, installed.data.site_build,
        "cccccccccccccccccccccccccccccccccccccccc",
        installed.data.postgres_build,
        installed.data.pg_duckdb_sha, installed.data.duckdb_sha,
        PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_WRONG_PG_QI_BUILD;
    evidence = qi_profile_classify(
        &installed, now, installed.data.site_build,
        installed.data.pg_qi_build,
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        installed.data.pg_duckdb_sha, installed.data.duckdb_sha,
        PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_WRONG_POSTGRES_BUILD;
    evidence = qi_profile_classify(
        &installed, now,
        "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        installed.data.pg_qi_build, installed.data.postgres_build,
        installed.data.pg_duckdb_sha,
        installed.data.duckdb_sha, PG_VERSION_NUM);
    ok = ok && evidence.reason == QI_PROFILE_WRONG_SITE_BUILD;

    PG_RETURN_BOOL(ok);
}
#endif
