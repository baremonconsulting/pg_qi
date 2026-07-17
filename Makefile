EXTENSION = pg_qi
MODULE_big = pg_qi
OBJS = src/pg_qi.o

DATA = sql/pg_qi--0.0.1.sql
REGRESS = pg_qi_basic

PGFILEDESC = "pg_qi - PostgreSQL Query Intelligence"
PG_CFLAGS += -Wall -Wextra

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
