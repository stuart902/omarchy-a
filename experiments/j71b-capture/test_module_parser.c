/* Socket-free driver for the pinned, unchanged conf.c parse_modules().
 * No PipeWire library, context, module, server or connection is created.
 * The Python test extracts the parser into a temporary include; only module
 * loading is replaced with an output recorder. Conditions are not exercised.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <spa/utils/cleanup.h>
#include <spa/utils/dict.h>
#include <spa/utils/json.h>

struct pw_properties { struct spa_dict dict; };
struct pw_context { struct pw_properties *properties; };
struct data {
    struct pw_context *context;
    struct pw_properties *props;
    int count;
};

#define pw_log_warn(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#define pw_log_error(...) pw_log_warn(__VA_ARGS__)

static bool pw_conf_find_match(struct spa_json *array,
                              const struct spa_dict *props, bool condition)
{
    fputs("Unexpected condition in parser-only fixture\n", stderr);
    exit(2);
}

static int load_module(struct pw_context *context, const char *name,
                       const char *args, const char *flags)
{
    /* NUL-separated pairs preserve the actual arguments, including newlines. */
    printf("%s%c%s%c", name, 0, args ? args : "", 0);
    return 0;
}

#include "pinned_parse_modules.inc"

int main(void)
{
    char *input = NULL;
    size_t capacity = 0;
    ssize_t length = getdelim(&input, &capacity, '\0', stdin);
    struct pw_properties properties = {0};
    struct pw_context context = { .properties = &properties };
    struct data data = { .context = &context };
    int result;

    if (length <= 0) {
        free(input);
        return 2;
    }
    result = parse_modules(&data, "parser-only-fixture", "context.modules",
                           input, (size_t)length);
    free(input);
    return result < 0 ? 1 : 0;
}
