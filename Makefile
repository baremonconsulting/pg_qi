PG_QI_TESTING ?= 0
PG_QI_BUILD_ID ?= 0000000000000000000000000000000000000000
PG_QI_POSTGRES_BUILD_ID ?= 0000000000000000000000000000000000000000000000000000000000000000

include deps/pg_duckdb.env

EXTENSION = pg_qi
MODULE_big = pg_qi
OBJS = src/pg_qi.o src/qi_classify.o src/qi_context.o src/qi_decision.o \
       src/qi_decision_v2.o \
       src/qi_delegation.o src/qi_detector.o src/qi_eligibility.o \
       src/qi_shadow_p0.o src/qi_semantic.o src/qi_plan_features.o \
       src/qi_stats_confidence.o src/qi_profile_parser.o src/qi_profile.o \
       src/qi_count_cert_parser.o src/qi_count_certificate.o \
       src/qi_benefit_model.o src/qi_benefit.o src/qi_spill.o \
       src/qi_snapshot.o src/qi_lifecycle.o src/qi_explain_guard.o

DATA = sql/pg_qi--0.0.1.sql

ifeq ($(PG_QI_TESTING),1)
EXTENSION += pg_qi_spike_test
OBJS += src/qi_reenter.o src/qi_diag.o
DATA += sql/pg_qi_spike_test--0.0.1.sql
REGRESS = pg_qi_basic
else
OBJS += src/qi_bound.o
REGRESS = pg_qi_shadow
endif

PGFILEDESC = "pg_qi - PostgreSQL Query Intelligence"
PG_CPPFLAGS += -Isrc/include -DPG_QI_TESTING=$(PG_QI_TESTING) \
               -DPG_QI_BUILD_ID=\"$(PG_QI_BUILD_ID)\" \
               -DPG_QI_POSTGRES_BUILD_ID=\"$(PG_QI_POSTGRES_BUILD_ID)\" \
               -DQI_EXPECTED_PG_DUCKDB_SHA=\"$(PG_DUCKDB_SHA)\" \
               -DQI_EXPECTED_DUCKDB_SHA=\"$(DUCKDB_SHA)\"
PG_CFLAGS += -Werror

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

ifeq ($(with_ssl),openssl)
SHLIB_LINK += -lcrypto
endif

.PHONY: pg_qi_build_identity_force
pg_qi_build_identity_force:

# Refresh each build-bound object when identities change between invocations.
src/qi_profile.o src/qi_count_certificate.o src/qi_semantic.o: pg_qi_build_identity_force
src/qi_profile.bc src/qi_count_certificate.bc src/qi_semantic.bc: pg_qi_build_identity_force
