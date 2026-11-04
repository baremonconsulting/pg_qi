EXTENSION = pg_qi
MODULE_big = pg_qi
OBJS = src/pg_qi.o src/qi_delegation.o src/qi_detector.o src/qi_reenter.o \
	src/qi_diag.o

DATA = sql/pg_qi--0.0.1.sql
REGRESS = pg_qi_basic

PGFILEDESC = "pg_qi - PostgreSQL Query Intelligence"
PG_CPPFLAGS += -Isrc/include
PG_CFLAGS += -Werror

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
