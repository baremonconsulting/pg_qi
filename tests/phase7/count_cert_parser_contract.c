/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "pg_qi/qi_count_cert_parser.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#define DIGEST_BLOCK "123456789abcdef0"
static const char sha1[] = DIGEST_BLOCK DIGEST_BLOCK "12345678";
static const char sha256[] = DIGEST_BLOCK DIGEST_BLOCK DIGEST_BLOCK DIGEST_BLOCK;

static void
assert_rejected(const char *text, size_t length)
{
    QiCountCertificate result;

    memset(&result, 0xff, sizeof(result));
    assert(!qi_count_cert_parse(text, length, &result));
    assert(result.postgres_version_num == 0);
    assert(result.evidence_digest[0] == '\0');
}

int
main(void)
{
    char record[QI_COUNT_CERT_MAX_BYTES + 1];
    char altered[sizeof(record)];
    QiCountCertificate result;
    size_t length;
    size_t i;
    int written = snprintf(record, sizeof(record), "v=1;scope=" QI_COUNT_CERT_SCOPE
        ";pg=190000;cluster=18446744073709551615;site=%s;pg_build=%s"
        ";qi_build=%s;provider_src=%s;duckdb_src=%s;pg_bin=%s;qi_bin=%s"
        ";provider_bin=%s;duckdb_bin=%s;proof=%s;issued=1795000000;expires=1795000101",
        sha256, sha256, sha1, sha1, sha1, sha256, sha256, sha256, sha256, sha256);

    assert(written > 0 && (size_t) written < sizeof(record));
    length = (size_t) written;
    assert(qi_count_cert_parse(record, length, &result));
    assert(result.postgres_version_num == 190000 && result.cluster_identifier == UINT64_MAX);
    assert(result.expires_at_unix == UINT64_C(1795000101));
    assert(strcmp(result.evidence_digest, sha256) == 0);

    assert_rejected(NULL, 0);
    assert_rejected(record, 0);
    assert_rejected(record, sizeof(record));
    assert_rejected(record, length + 1);
    /* The final decimal field can form a different valid complete record if
     * truncated exactly at a digit. Its authorisation digest must therefore
     * be checked separately, even if the parser accepts it. This fixture
     * makes those abbreviated times strictly earlier than issue. */
    for (i = 0; i < length; i++)
    {
        assert_rejected(record, i);
        memcpy(altered, record, length + 1);
        altered[i] = '\0';
        assert_rejected(altered, length);
    }
    memcpy(altered, record, length + 1);
    strstr(altered, "cluster=")[strlen("cluster=") + 19] = '6';
    assert_rejected(altered, length);
    memcpy(altered, record, length + 1);
    strstr(altered, "proof=")[strlen("proof=")] = 'A';
    assert_rejected(altered, length);
    memcpy(altered, record, length + 1);
    memset(strstr(altered, "proof=") + strlen("proof="), '0', 64);
    assert_rejected(altered, length);
    memcpy(altered, record, length + 1);
    strstr(altered, "expires=")[strlen("expires=")] = '9';
    assert_rejected(altered, length);
    puts("canonical record accepted; all truncations, embedded NULs, overflow and invalid digest/window rejected");
    return 0;
}
