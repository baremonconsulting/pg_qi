#!/usr/bin/env bash

set -euo pipefail

file=${1:?file is required}
tool=${SHA256SUM:-sha256sum}
value=$($tool -- "$file")
digest=${value%% *}
test "${#digest}" -eq 64
[[ "$digest" =~ ^[0-9a-f]{64}$ ]]
printf '%s\n' "$digest"
