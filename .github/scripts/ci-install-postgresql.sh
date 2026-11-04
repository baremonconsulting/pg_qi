#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}

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
    build-essential libcurl4-openssl-dev liblz4-dev unzip \
    "postgresql-$major" "postgresql-server-dev-$major"
