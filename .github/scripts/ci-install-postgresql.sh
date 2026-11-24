#!/usr/bin/env bash

set -euo pipefail
script_dir=$(cd "$(dirname "$0")" && pwd)

major=${1:?PostgreSQL major version is required}
source_pinned=${PG_SOURCE_PINNED:-0}
case "$source_pinned" in 0|1) ;; *) echo "PG_SOURCE_PINNED must be 0 or 1" >&2; exit 2;; esac
case "$major" in
    16) source_sha=${PG16_SHA:-c0534eb02de050ac1035ad7b3ad7c3f02980f153}; expected_version='PostgreSQL 16.16' ;;
    17) source_sha=${PG17_SHA:-702b3dc86d35bcc08c8e23cffa3c4d9fe9d32c85}; expected_version='PostgreSQL 17.12' ;;
    18) source_sha=${PG18_SHA:-f5c41f6681c910b3f69951bf6fb97c6d74605d25}; expected_version='PostgreSQL 18.7' ;;
    19) source_sha=${PG19_SHA:-5c839a2a08b9b87a349be97371fac6c5a2873b10}; expected_version='PostgreSQL 19.0' ;;
    *) echo "unsupported PostgreSQL major: $major" >&2; exit 2 ;;
esac
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

if [ "$source_pinned" = 1 ] || [ "$major" = 19 ]; then
    source_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/postgresql-$major-source"
    build_dir="${RUNNER_TEMP}/postgresql-$major-build"
    git clone --filter=blob:none https://github.com/postgres/postgres.git \
        "$source_dir"
    git -C "$source_dir" checkout --detach "$source_sha"
    test "$(git -C "$source_dir" rev-parse HEAD)" = "$source_sha"
    mkdir -p "$build_dir"
    (
        cd "$build_dir"
        "$source_dir/configure" --prefix="/usr/lib/postgresql/$major" \
            --with-openssl --with-lz4
        make -j2
        sudo make install
        hash_tool=$script_dir/ci-sha256.sh
        source_postgres_sha=$(bash "$hash_tool" "$build_dir/src/backend/postgres")
        installed_postgres_sha=$(bash "$hash_tool" "/usr/lib/postgresql/$major/bin/postgres")
        test "$source_postgres_sha" = "$installed_postgres_sha"
        for module in file_fdw auto_explain; do
            make -C "$source_dir/contrib/$module" USE_PGXS=1 \
                PG_CONFIG="/usr/lib/postgresql/$major/bin/pg_config" -j2
            sudo make -C "$source_dir/contrib/$module" USE_PGXS=1 \
                PG_CONFIG="/usr/lib/postgresql/$major/bin/pg_config" install
        done
    )
    test "$(/usr/lib/postgresql/$major/bin/pg_config --version)" = \
        "$expected_version"
    test "$(/usr/lib/postgresql/$major/bin/postgres --version)" = \
        "postgres (PostgreSQL) ${expected_version#PostgreSQL }"
else
    sudo apt-get install --yes --no-install-recommends \
        "postgresql-$major" "postgresql-server-dev-$major"
fi

installed_version=$(/usr/lib/postgresql/$major/bin/pg_config --version)
if [ "$source_pinned" = 1 ] || [ "$major" = 19 ]; then
    test "$installed_version" = "$expected_version"
else
    case "$installed_version" in "PostgreSQL $major".*) ;; *)
        echo "unexpected PostgreSQL package version: $installed_version" >&2
        exit 1
    esac
fi

trap - EXIT
