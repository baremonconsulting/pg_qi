/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_BUILD_IDENTITY_H
#define PG_QI_QI_BUILD_IDENTITY_H

#ifndef PG_QI_BUILD_ID
#define PG_QI_BUILD_ID "0000000000000000000000000000000000000000"
#endif

#ifndef PG_QI_POSTGRES_BUILD_ID
#define PG_QI_POSTGRES_BUILD_ID \
    "0000000000000000000000000000000000000000000000000000000000000000"
#endif

#ifndef QI_EXPECTED_PG_DUCKDB_SHA
#error "QI_EXPECTED_PG_DUCKDB_SHA must come from the provider build pins"
#endif

#ifndef QI_EXPECTED_DUCKDB_SHA
#error "QI_EXPECTED_DUCKDB_SHA must come from the provider build pins"
#endif

#endif /* PG_QI_QI_BUILD_IDENTITY_H */
