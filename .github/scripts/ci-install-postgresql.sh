#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg19_sha=${PG19_SHA:-0b8bff042a997fe50fcd1c4c83b9ef1cc9ed8a89}

sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
    ca-certificates curl gnupg lsb-release
sudo install -d -m 0755 /usr/share/postgresql-common/pgdg
curl --fail --silent --show-error \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | sudo gpg --dearmor --yes \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg
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
        "PostgreSQL 19rc1"
else
    sudo apt-get install --yes --no-install-recommends \
        "postgresql-$major" "postgresql-server-dev-$major"
fi
