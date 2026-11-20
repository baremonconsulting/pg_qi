/* Deterministic sanitizer harness for the production PQ7-10 parser. */

#include "pg_qi/qi_profile_parser.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint64_t state = UINT64_C(0x9e3779b97f4a7c15);

static uint64_t
next_random(void)
{
    state ^= state << 7;
    state ^= state >> 9;
    return state;
}

static QiProfileParseReason
check_one(const char *data, size_t length)
{
    QiProfileData first;
    QiProfileData second;
    QiProfileData zero = {0};
    char *bounded = malloc(length == 0 ? 1U : length);
    QiProfileParseReason a;
    QiProfileParseReason b;

    if (bounded == NULL)
        abort();
    if (length > 0)
        memcpy(bounded, data, length);
    a = qi_profile_parse(bounded, length, &first);
    b = qi_profile_parse(bounded, length, &second);

    if (a != b || memcmp(&first, &second, sizeof(first)) != 0)
        abort();
    if (a != QI_PROFILE_PARSE_OK &&
        memcmp(&first, &zero, sizeof(first)) != 0)
        abort();
    free(bounded);
    return a;
}

int
main(int argc, char **argv)
{
    const char *seed;
    size_t seed_length;
    char *buffer;
    size_t iteration;

    if (argc != 2)
        return 2;
    seed = argv[1];
    seed_length = strlen(seed);
    if (qi_profile_parse(seed, seed_length, &(QiProfileData) {0}) !=
        QI_PROFILE_PARSE_OK)
        return 3;
    for (iteration = 0; iteration < seed_length; iteration++)
        if (check_one(seed, iteration) == QI_PROFILE_PARSE_OK)
            abort();
    if (check_one(seed, seed_length) != QI_PROFILE_PARSE_OK)
        abort();

    buffer = malloc(QI_PROFILE_MAX_BYTES + 2U);
    if (buffer == NULL)
        return 4;
    memcpy(buffer, seed, seed_length);
    {
        char *generation = strstr(buffer, ";gen=");

        if (generation == NULL)
            abort();
        generation[5] = generation[5] == '9' ? '8' : '9';
        if (check_one(buffer, seed_length) !=
            QI_PROFILE_PARSE_CHECKSUM_MISMATCH)
            abort();
    }
    for (iteration = 0; iteration < 100000U; iteration++)
    {
        size_t length = seed_length;
        size_t changes = 1U + next_random() % 8U;
        size_t change;

        memset(buffer, 0xa5, QI_PROFILE_MAX_BYTES + 2U);
        memcpy(buffer, seed, seed_length);
        for (change = 0; change < changes; change++)
        {
            uint64_t action = next_random() % 4U;
            size_t position;

            if (action == 0 && length > 0)
            {
                position = next_random() % length;
                buffer[position] ^= (char) (1U << (next_random() % 7U));
            }
            else if (action == 1 && length > 0)
            {
                position = next_random() % length;
                memmove(buffer + position, buffer + position + 1,
                        length - position - 1U);
                length--;
            }
            else if (action == 2 && length < QI_PROFILE_MAX_BYTES + 1U)
            {
                position = length == 0 ? 0 : next_random() % (length + 1U);
                memmove(buffer + position + 1, buffer + position,
                        length - position);
                buffer[position] = (char) (next_random() & 0xffU);
                length++;
            }
            else
                length = next_random() % (QI_PROFILE_MAX_BYTES + 2U);
        }
        check_one(buffer, length);
    }
    memset(buffer, 'x', QI_PROFILE_MAX_BYTES + 1U);
    if (check_one(buffer, QI_PROFILE_MAX_BYTES) !=
            QI_PROFILE_PARSE_MALFORMED ||
        check_one(buffer, QI_PROFILE_MAX_BYTES + 1U) !=
            QI_PROFILE_PARSE_OVERSIZE)
        abort();
    free(buffer);
    puts("PQ-34 profile parser fuzz PASS");
    return 0;
}
