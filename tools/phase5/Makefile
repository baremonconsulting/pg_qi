PG_CONFIG ?= pg_config
CC ?= cc
CPPFLAGS += -I$(shell $(PG_CONFIG) --includedir)
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -Wpedantic
LDFLAGS += -L$(shell $(PG_CONFIG) --libdir) -Wl,-rpath,$(shell $(PG_CONFIG) --libdir)
LDLIBS += -lpq -lcrypto

.PHONY: all clean protocol-driver-fault

all: protocol-driver site-preflight-driver audited-session-driver

protocol-driver: protocol_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

# Test-only binary. Fault points are absent from the production target.
protocol-driver-fault: protocol_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -DPGQI5_FAULTPOINTS -o $@ $< $(LDFLAGS) $(LDLIBS)

site-preflight-driver: site_preflight_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

audited-session-driver: audited_session_driver.c protocol_driver.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ audited_session_driver.c $(LDFLAGS) $(LDLIBS)

clean:
	rm -f protocol-driver protocol-driver-fault site-preflight-driver audited-session-driver
