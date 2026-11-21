#!/usr/bin/env bash

set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

${CC:-cc} -std=c11 -Wall -Wextra -Werror -O1 -g \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I"$repo/src/include" \
    "$repo/src/qi_benefit_model.c" \
    "$repo/tests/phase7/benefit_model_contract.c" \
    -o "$work/benefit-model-contract"
ASAN_OPTIONS=halt_on_error=1:abort_on_error=1 \
UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 \
    "$work/benefit-model-contract"
