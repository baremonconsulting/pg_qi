PG_CONFIG ?= pg_config
CC ?= cc
CPPFLAGS += -I$(shell $(PG_CONFIG) --includedir)
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -Wpedantic
LDFLAGS += -L$(shell $(PG_CONFIG) --libdir) -Wl,-rpath,$(shell $(PG_CONFIG) --libdir)
LDLIBS += -lpq -lcrypto

.PHONY: all clean

all: protocol-driver site-preflight-driver

protocol-driver: protocol_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

site-preflight-driver: site_preflight_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

clean:
	rm -f protocol-driver site-preflight-driver
