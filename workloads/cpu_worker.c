#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static volatile uint64_t result = 1;

static uint64_t now_ns(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        perror("clock_gettime");
        exit(EXIT_FAILURE);
    }

    return (uint64_t)ts.tv_sec * 1000000000ULL
         + (uint64_t)ts.tv_nsec;
}

int main(int argc, char **argv)
{
    double duration = 10.0;

    if (argc > 1) {
        char *end = NULL;
        duration = strtod(argv[1], &end);

        if (end == argv[1] || *end != '\0' || duration <= 0.0) {
            fprintf(stderr, "Usage: %s [duration_seconds]\n", argv[0]);
            return EXIT_FAILURE;
        }
    }

    const uint64_t start = now_ns();
    const uint64_t duration_ns =
        (uint64_t)(duration * 1000000000.0);
    const uint64_t deadline = start + duration_ns;

    uint64_t iterations = 0;
    uint64_t x = 1;

    while (now_ns() < deadline) {
        x = x * 1664525ULL + 1013904223ULL;
        x ^= x >> 13;
        x *= 0x9e3779b97f4a7c15ULL;

        iterations++;
    }

    result = x;

    printf("duration_seconds=%.3f\n", duration);
    printf("iterations=%" PRIu64 "\n", iterations);
    printf("result=%" PRIu64 "\n", result);

    return EXIT_SUCCESS;
}
