#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg19_sha=${PG19_SHA:-dffcca2e3c81178c64d4c1f44543a3507d0b7508}
pgdg_key_urls=(
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
    https://apt.postgresql.org/pub/repos/apt/ACCC4CF8.asc
)
pgdg_key_fingerprint=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8
pgdg_keyring=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg
pgdg_temp_dir=
pgdg_staged_keyring=

cleanup_pgdg_key() {
    if [ -n "$pgdg_staged_keyring" ]; then
        sudo rm -f "$pgdg_staged_keyring"
    fi
    if [ -n "$pgdg_temp_dir" ]; then
        rm -rf "$pgdg_temp_dir"
    fi
}

trap cleanup_pgdg_key EXIT

sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
    ca-certificates curl gnupg lsb-release
sudo install -d -m 0755 /usr/share/postgresql-common/pgdg
pgdg_temp_dir=$(mktemp -d)
pgdg_download="$pgdg_temp_dir/ACCC4CF8.asc"
pgdg_dearmored="$pgdg_temp_dir/apt.postgresql.org.gpg"

pgdg_downloaded=false
for pgdg_key_url in "${pgdg_key_urls[@]}"; do
    if curl --ipv4 --fail --location --silent --show-error \
        --connect-timeout 10 \
        --max-time 30 \
        --retry 2 \
        --retry-all-errors \
        --retry-delay 2 \
        --retry-max-time 90 \
        --output "$pgdg_download" \
        "$pgdg_key_url"; then
        pgdg_downloaded=true
        break
    fi
    echo "PGDG signing-key download failed; trying next official endpoint" >&2
done
if [ "$pgdg_downloaded" != true ]; then
    echo "unable to download the PGDG signing key from official endpoints" >&2
    exit 1
fi
test -s "$pgdg_download"

downloaded_fingerprint=$(
    gpg --batch --show-keys --with-colons "$pgdg_download" 2>/dev/null \
        | awk -F: '$1 == "fpr" { print toupper($10); exit }'
)
if [ "$downloaded_fingerprint" != "$pgdg_key_fingerprint" ]; then
    echo "unexpected PostgreSQL repository signing-key fingerprint" >&2
    exit 1
fi

gpg --batch --yes --dearmor --output "$pgdg_dearmored" "$pgdg_download"
test -s "$pgdg_dearmored"
pgdg_staged_keyring="${pgdg_keyring}.tmp.$$"
sudo install -m 0644 "$pgdg_dearmored" "$pgdg_staged_keyring"
sudo mv "$pgdg_staged_keyring" "$pgdg_keyring"
pgdg_staged_keyring=
rm -rf "$pgdg_temp_dir"
pgdg_temp_dir=
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
    bison build-essential clang cmake flex libcurl4-openssl-dev liblz4-dev \
    libreadline-dev libssl-dev ninja-build pkg-config unzip zlib1g-dev

if [ "$major" = 19 ]; then
    source_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/postgresql-19-source"
    build_dir="${RUNNER_TEMP}/postgresql-19-build"
    git clone --filter=blob:none https://github.com/postgres/postgres.git \
        "$source_dir"
    git -C "$source_dir" checkout --detach "$pg19_sha"
    test "$(git -C "$source_dir" rev-parse HEAD)" = "$pg19_sha"
    mkdir -p "$build_dir"
    (
        cd "$build_dir"
        "$source_dir/configure" --prefix=/usr/lib/postgresql/19 \
            --with-openssl --with-lz4
        make -j2
        sudo make install
    )
    test "$(/usr/lib/postgresql/19/bin/pg_config --version)" = \
        "PostgreSQL 19rc2"
else
    sudo apt-get install --yes --no-install-recommends \
        "postgresql-$major" "postgresql-server-dev-$major"
fi

trap - EXIT
