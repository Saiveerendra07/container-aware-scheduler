#ifndef PROCESS_LIFECYCLE_H
#define PROCESS_LIFECYCLE_H

#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>

#define PROCESS_PARENT_PID_ENV "CAS_PARENT_PID"

static inline int process_lifecycle_set_parent_death(pid_t expected_parent)
{
	if (expected_parent <= 1) {
		errno = EINVAL;
		return -1;
	}
	if (prctl(PR_SET_PDEATHSIG, SIGTERM) == -1)
		return -1;
	if (getppid() != expected_parent) {
		errno = ESRCH;
		return -1;
	}
	return 0;
}

static inline int process_lifecycle_arm_parent_death(void)
{
	const char *text = getenv(PROCESS_PARENT_PID_ENV);
	char *end;
	long parent_pid;

	if (!text)
		return 0;
	if (!text[0]) {
		errno = EINVAL;
		return -1;
	}
	for (const char *cursor = text; *cursor; cursor++) {
		if (*cursor < '0' || *cursor > '9') {
			errno = EINVAL;
			return -1;
		}
	}

	errno = 0;
	parent_pid = strtol(text, &end, 10);
	if (errno || *end || parent_pid <= 1 || parent_pid > INT_MAX) {
		errno = EINVAL;
		return -1;
	}
	return process_lifecycle_set_parent_death((pid_t)parent_pid);
}

#endif
