#ifndef GROUP_SCHEDULER_H
#define GROUP_SCHEDULER_H

#define GROUP_SCHEDULER_MIN_GROUPS 2
#define GROUP_SCHEDULER_MAX_GROUPS 8
#define GROUP_SCHEDULER_MAX_SCHED_GROUPS (GROUP_SCHEDULER_MAX_GROUPS + 1)
#define GROUP_SCHEDULER_MIN_SHARE 1
#define GROUP_SCHEDULER_MAX_SHARE 10000

struct configured_group {
	unsigned long long cgroup_id;
	unsigned int share;
	unsigned int reserved;
};

struct scheduler_config {
	unsigned int nr_groups;
	unsigned int total_share;
	unsigned int fallback_share;
	unsigned int reserved;
	struct configured_group groups[GROUP_SCHEDULER_MAX_GROUPS];
};

#endif
