MODULE_big = pg_qi_execution_witness
OBJS = pg_qi_execution_witness.o

PG_CFLAGS += -Werror

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
