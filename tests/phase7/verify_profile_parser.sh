#!/usr/bin/env bash

set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
seed=$(python3 "$repo/tools/phase7/profile_generator.py" \
    --generation 1 --postgres-version-num 170000 \
    --postgres-build eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee \
    --pg-qi-build bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    --site-build aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    --issued-at 1000 --expires-at 2000)

${CC:-cc} -std=c11 -O1 -g -Wall -Wextra -Werror \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I"$repo/src/include" "$repo/src/qi_profile_parser.c" \
    "$repo/tests/phase7/profile_parser_fuzz.c" -o "$work/profile_parser_fuzz"
ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 \
UBSAN_OPTIONS=halt_on_error=1 \
    "$work/profile_parser_fuzz" "$seed"
