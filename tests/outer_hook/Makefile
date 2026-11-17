MODULE_big = pg_qi_outer_test
OBJS = pg_qi_outer_test.o

PG_CPPFLAGS += -I../../src/include
PG_CFLAGS += -Werror

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
