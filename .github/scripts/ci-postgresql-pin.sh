# Source this file with the PostgreSQL major to select the CI source and version.

case "${1:?PostgreSQL major version is required}" in
    16) source_sha=${PG16_SHA:-c0534eb02de050ac1035ad7b3ad7c3f02980f153}; expected_version='PostgreSQL 16.16' ;;
    17) source_sha=${PG17_SHA:-702b3dc86d35bcc08c8e23cffa3c4d9fe9d32c85}; expected_version='PostgreSQL 17.12' ;;
    18) source_sha=${PG18_SHA:-f5c41f6681c910b3f69951bf6fb97c6d74605d25}; expected_version='PostgreSQL 18.7' ;;
    19) source_sha=${PG19_SHA:-5c839a2a08b9b87a349be97371fac6c5a2873b10}; expected_version='PostgreSQL 19.0' ;;
    *) echo "unsupported PostgreSQL major: $1" >&2; return 2 ;;
esac
