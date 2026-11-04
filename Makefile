PG_QI_TESTING ?= 0

EXTENSION = pg_qi
MODULE_big = pg_qi
OBJS = src/pg_qi.o src/qi_classify.o src/qi_delegation.o src/qi_detector.o

DATA = sql/pg_qi--0.0.1.sql

ifeq ($(PG_QI_TESTING),1)
EXTENSION += pg_qi_spike_test
OBJS += src/qi_reenter.o src/qi_diag.o
DATA += sql/pg_qi_spike_test--0.0.1.sql
REGRESS = pg_qi_basic
endif

PGFILEDESC = "pg_qi - PostgreSQL Query Intelligence"
PG_CPPFLAGS += -Isrc/include -DPG_QI_TESTING=$(PG_QI_TESTING)
PG_CFLAGS += -Werror

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
