// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <bpf/libbpf.h>
#include <scx/common.h>

#include "group_scheduler.h"
#include "group_scheduler.skel.h"

static volatile sig_atomic_t exit_requested;
static bool verbose;

static void handle_signal(int signal_number)
{
	(void)signal_number;
	exit_requested = 1;
}

static int libbpf_print(enum libbpf_print_level level, const char *format,
			va_list args)
{
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, format, args);
}

static bool parse_decimal(const char *text, uint64_t *value)
{
	char *end;
	unsigned long long parsed;

	if (!text[0])
		return false;
	for (const char *p = text; *p; p++) {
		if (*p < '0' || *p > '9')
			return false;
	}

	errno = 0;
	parsed = strtoull(text, &end, 10);
	if (errno || *end)
		return false;
	*value = parsed;
	return true;
}

static bool parse_share(const char *text, uint64_t *share)
{
	return parse_decimal(text, share) &&
	       *share >= GROUP_SCHEDULER_MIN_SHARE &&
	       *share <= GROUP_SCHEDULER_MAX_SHARE;
}

static bool add_group(struct scheduler_config *config, const char *argument)
{
	char *separator;
	char *copy;
	uint64_t cgroup_id;
	uint64_t share;
	bool valid = false;

	if (config->nr_groups >= GROUP_SCHEDULER_MAX_GROUPS)
		return false;

	copy = strdup(argument);
	if (!copy)
		return false;
	separator = strchr(copy, ':');
	if (!separator || strchr(separator + 1, ':'))
		goto out;
	*separator = '\0';
	if (!parse_decimal(copy, &cgroup_id) ||
	    !parse_share(separator + 1, &share) ||
	    cgroup_id == 0 ||
	    config->total_share + share >
		    GROUP_SCHEDULER_MAX_GROUPS * GROUP_SCHEDULER_MAX_SHARE)
		goto out;

	for (__u32 i = 0; i < config->nr_groups; i++) {
		if (config->groups[i].cgroup_id == cgroup_id)
			goto out;
	}

	config->groups[config->nr_groups].cgroup_id = cgroup_id;
	config->groups[config->nr_groups].share = share;
	config->total_share += share;
	config->nr_groups++;
	valid = true;
out:
	free(copy);
	return valid;
}

static void print_usage(const char *program)
{
	fprintf(stderr,
		"Usage: %s [--verbose] --fallback-share SHARE "
		"--group CGROUP_ID:SHARE --group CGROUP_ID:SHARE "
		"[--group CGROUP_ID:SHARE ...]\n"
		"Configure 2 to %d exact cgroup IDs and one fallback group; "
		"each share must be 1..%d.\n",
		program, GROUP_SCHEDULER_MAX_GROUPS,
		GROUP_SCHEDULER_MAX_SHARE);
}

int main(int argc, char **argv)
{
	struct scheduler_config config = {};
	struct group_scheduler *skel = NULL;
	struct bpf_link *link = NULL;
	struct sigaction action = {
		.sa_handler = handle_signal,
	};
	bool fallback_share_set = false;
	uint64_t fallback_share;
	int load_error;
	long exit_code = 0;
	int ret = EXIT_FAILURE;

	sigemptyset(&action.sa_mask);
	if (sigaction(SIGINT, &action, NULL) ||
	    sigaction(SIGTERM, &action, NULL)) {
		perror("sigaction");
		return EXIT_FAILURE;
	}

	libbpf_set_print(libbpf_print);
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--verbose")) {
			verbose = true;
		} else if (!strcmp(argv[i], "--fallback-share") &&
			   i + 1 < argc && !fallback_share_set) {
			if (!parse_share(argv[++i], &fallback_share)) {
				fprintf(stderr, "Invalid fallback group share\n");
				print_usage(argv[0]);
				return EXIT_FAILURE;
			}
			fallback_share_set = true;
		} else if (!strcmp(argv[i], "--group") && i + 1 < argc) {
			if (!add_group(&config, argv[++i])) {
				fprintf(stderr, "Invalid group specification\n");
				print_usage(argv[0]);
				return EXIT_FAILURE;
			}
		} else {
			print_usage(argv[0]);
			return EXIT_FAILURE;
		}
	}

	if (!fallback_share_set ||
	    config.nr_groups < GROUP_SCHEDULER_MIN_GROUPS) {
		fprintf(stderr,
			"At least %d configured groups and one fallback share are required\n",
			GROUP_SCHEDULER_MIN_GROUPS);
		print_usage(argv[0]);
		return EXIT_FAILURE;
	}
	config.fallback_share = fallback_share;
	if ((uint64_t)config.total_share + fallback_share >
	    GROUP_SCHEDULER_MAX_SCHED_GROUPS * GROUP_SCHEDULER_MAX_SHARE) {
		fprintf(stderr, "Combined group shares exceed the supported total\n");
		return EXIT_FAILURE;
	}
	config.total_share += fallback_share;

	skel = SCX_OPS_OPEN(group_scheduler_ops, group_scheduler);
	if (!skel) {
		fprintf(stderr, "Failed to open BPF skeleton\n");
		goto out;
	}
	skel->rodata->scheduler_config.nr_groups = config.nr_groups;
	skel->rodata->scheduler_config.total_share = config.total_share;
	skel->rodata->scheduler_config.fallback_share = config.fallback_share;
	memcpy(skel->rodata->scheduler_config.groups, config.groups,
	       sizeof(config.groups));

	UEI_SET_SIZE(skel, group_scheduler_ops, uei);
	load_error = group_scheduler__load(skel);
	if (load_error) {
		fprintf(stderr, "Failed to load scheduler BPF programs: %s\n",
			strerror(-load_error));
		goto out;
	}

	link = bpf_map__attach_struct_ops(skel->maps.group_scheduler_ops);
	if (!link || libbpf_get_error(link)) {
		long link_error = link ? -libbpf_get_error(link) :
					 (errno ? errno : EIO);

		fprintf(stderr, "Failed to attach sched_ext operations: %s\n",
			strerror((int)link_error));
		link = NULL;
		goto out;
	}
	/* The experiment runner waits for this exact line before releasing workers. */
	if (puts("GROUP_SCHEDULER_ATTACHED") == EOF || fflush(stdout) == EOF) {
		fprintf(stderr, "Failed to report scheduler attachment readiness\n");
		goto out;
	}

	while (!exit_requested && !UEI_EXITED(skel, uei))
		sleep(1);

	ret = EXIT_SUCCESS;
out:
	if (link) {
		int destroy_error = bpf_link__destroy(link);

		if (destroy_error) {
			fprintf(stderr, "Failed to destroy scheduler link: %s\n",
				strerror(-destroy_error));
			ret = EXIT_FAILURE;
		}
	}
	if (skel && skel->data && UEI_EXITED(skel, uei)) {
		exit_code = UEI_REPORT(skel, uei);
		if (exit_code)
			ret = EXIT_FAILURE;
	}
	group_scheduler__destroy(skel);
	return ret;
}
