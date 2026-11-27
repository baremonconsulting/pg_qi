PG_CONFIG ?= pg_config
PG_CLIENT_CONFIG ?= $(PG_CONFIG)
CC ?= cc
CPPFLAGS += -I$(shell $(PG_CLIENT_CONFIG) --includedir)
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -Wpedantic
LDFLAGS += -L$(shell $(PG_CLIENT_CONFIG) --libdir) -Wl,-rpath,$(shell $(PG_CLIENT_CONFIG) --libdir)
LDLIBS += -lpq -lcrypto

.PHONY: all clean

all: differential-driver

differential-driver: differential_driver.c ../phase5/protocol_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ differential_driver.c $(LDFLAGS) $(LDLIBS)

clean:
	rm -f differential-driver
