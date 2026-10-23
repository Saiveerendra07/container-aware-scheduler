// SPDX-License-Identifier: GPL-2.0
#include <scx/common.bpf.h>
#include "group_scheduler.h"

char _license[] SEC("license") = "GPL";

const volatile struct scheduler_config scheduler_config;

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, GROUP_SCHEDULER_MAX_SCHED_GROUPS);
	__type(key, __u32);
	__type(value, __u64);
} group_vtime SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, GROUP_SCHEDULER_MAX_SCHED_GROUPS);
	__type(key, __u32);
	__type(value, __u64);
} group_running_count SEC(".maps");

struct cpu_runtime_ctx {
	__u64 start_runtime;
	__u32 group_index;
	__u32 running;
};

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct cpu_runtime_ctx);
} cpu_runtime SEC(".maps");

UEI_DEFINE(uei);

#define FALLBACK_GROUP_INDEX GROUP_SCHEDULER_MAX_GROUPS

static __u32 find_group_index(struct task_struct *p)
{
	struct cgroup *cgrp;
	__u64 cgroup_id;
	__u32 i;

	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		return FALLBACK_GROUP_INDEX;

	cgroup_id = cgrp->kn->id;
	bpf_cgroup_release(cgrp);

	bpf_for(i, 0, GROUP_SCHEDULER_MAX_GROUPS) {
		if (i >= scheduler_config.nr_groups)
			break;
		if (scheduler_config.groups[i].cgroup_id == cgroup_id)
			return i;
	}

	return FALLBACK_GROUP_INDEX;
}

static __u64 group_share(__u32 index)
{
	if (index == FALLBACK_GROUP_INDEX)
		return scheduler_config.fallback_share;
	return scheduler_config.groups[index].share;
}

/*
 * A group which has been idle must not keep an old virtual-time advantage (or
 * penalty). Rebase an empty DSQ to the minimum virtual time among groups which
 * are queued or still running. The atomic compare/exchange avoids losing a
 * concurrent runtime charge from stopping().
 */
static bool rebase_idle_group(__u32 index)
{
	__u64 *own_vtime;
	__u64 min_vtime = 0;
	__u32 own_key = index;
	__u32 i;
	bool have_active_group = false;
	s32 queued;

	queued = scx_bpf_dsq_nr_queued(index);
	if (queued < 0) {
		scx_bpf_error("failed to inspect group queue during enqueue");
		return false;
	}
	if (queued)
		return true;

	own_vtime = bpf_map_lookup_elem(&group_vtime, &own_key);
	if (!own_vtime) {
		scx_bpf_error("group virtual-time lookup failed during enqueue");
		return false;
	}

	/* A running task keeps the group's virtual time active even with an empty DSQ. */
	{
		__u64 *running_count;

		running_count = bpf_map_lookup_elem(&group_running_count, &own_key);
		if (!running_count) {
			scx_bpf_error("group running-count lookup failed during enqueue");
			return false;
		}
		if (*running_count)
			return true;
	}

	bpf_for(i, 0, GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
		__u64 *vtime;
		__u64 *running_count;
		__u32 key = i;

		if (i == index)
			continue;
		queued = scx_bpf_dsq_nr_queued(i);
		if (queued < 0) {
			scx_bpf_error("failed to inspect group queue during rebase");
			return false;
		}
		running_count = bpf_map_lookup_elem(&group_running_count, &key);
		if (!running_count) {
			scx_bpf_error("group running-count lookup failed during rebase");
			return false;
		}
		if (!queued && !*running_count)
			continue;

		vtime = bpf_map_lookup_elem(&group_vtime, &key);
		if (!vtime) {
			scx_bpf_error("group virtual-time lookup failed during rebase");
			return false;
		}
		if (!have_active_group || time_before(*vtime, min_vtime)) {
			min_vtime = *vtime;
			have_active_group = true;
		}
	}

	/* No competitors means no old virtual-time state should carry forward. */
	if (!have_active_group)
		min_vtime = 0;

	/* Retry a few races with stopping(); failure only postpones this rebase. */
	for (int retry = 0; retry < 4; retry++) {
		__u64 current = *own_vtime;

		if (current == min_vtime)
			return true;
		if ((__u64)__sync_val_compare_and_swap(own_vtime, current,
							 min_vtime) == current)
			return true;
	}
	return true;
}

static void mark_group_stopped(__u32 index)
{
	__u32 key = index;
	__u64 *running_count;

	running_count = bpf_map_lookup_elem(&group_running_count, &key);
	if (!running_count || !*running_count) {
		scx_bpf_error("group running count is invalid during stopping");
		return;
	}
	__sync_fetch_and_sub(running_count, 1);
}

s32 BPF_STRUCT_OPS(group_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
{
	bool is_idle = false;

	return scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
}

void BPF_STRUCT_OPS(group_enqueue, struct task_struct *p, u64 enq_flags)
{
	__u32 index = find_group_index(p);

	if (index >= GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
		scx_bpf_error("invalid group index during enqueue");
		return;
	}
	if (!rebase_idle_group(index))
		return;

	scx_bpf_dsq_insert(p, index, SCX_SLICE_DFL, enq_flags);
}

void BPF_STRUCT_OPS(group_dispatch, s32 cpu, struct task_struct *prev)
{
	bpf_repeat(GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
		__u32 best_index = GROUP_SCHEDULER_MAX_SCHED_GROUPS;
		__u32 i;
		__u64 best_vtime = 0;

		bpf_for(i, 0, GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
			__u64 *vtime;
			__u32 key = i;

			if (scx_bpf_dsq_nr_queued(i) <= 0)
				continue;

			vtime = bpf_map_lookup_elem(&group_vtime, &key);
			if (!vtime) {
				scx_bpf_error("group virtual-time lookup failed during dispatch");
				return;
			}
			if (best_index == GROUP_SCHEDULER_MAX_SCHED_GROUPS ||
			    time_before(*vtime, best_vtime)) {
				best_index = i;
				best_vtime = *vtime;
			}
		}

		if (best_index == GROUP_SCHEDULER_MAX_SCHED_GROUPS)
			return;
		if (scx_bpf_dsq_move_to_local(best_index))
			return;
	}
}

void BPF_STRUCT_OPS(group_running, struct task_struct *p)
{
	struct cpu_runtime_ctx *runtime_ctx;
	__u32 key = 0;

	runtime_ctx = bpf_map_lookup_elem(&cpu_runtime, &key);
	if (!runtime_ctx) {
		scx_bpf_error("CPU runtime context lookup failed during running");
		return;
	}

	runtime_ctx->group_index = find_group_index(p);
	runtime_ctx->start_runtime = p->se.sum_exec_runtime;
	runtime_ctx->running = 0;
	{
		__u32 running_key = runtime_ctx->group_index;
		__u64 *running_count;

		if (running_key >= GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
			scx_bpf_error("invalid group index during running");
			return;
		}
		running_count = bpf_map_lookup_elem(&group_running_count, &running_key);
		if (!running_count) {
			scx_bpf_error("group running-count lookup failed during running");
			return;
		}
		__sync_fetch_and_add(running_count, 1);
	}
	runtime_ctx->running = 1;
}

void BPF_STRUCT_OPS(group_stopping, struct task_struct *p, bool runnable)
{
	struct cpu_runtime_ctx *runtime_ctx;
	__u64 *vtime;
	__u64 runtime;
	__u64 share;
	__u32 key = 0;
	__u32 group_key;
	__u32 index;

	runtime_ctx = bpf_map_lookup_elem(&cpu_runtime, &key);
	if (!runtime_ctx || !runtime_ctx->running) {
		scx_bpf_error("CPU runtime context missing during stopping");
		return;
	}

	index = runtime_ctx->group_index;
	runtime_ctx->running = 0;
	if (index >= GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
		scx_bpf_error("invalid group index during stopping");
		return;
	}
	runtime = p->se.sum_exec_runtime - runtime_ctx->start_runtime;
	if (!runtime) {
		mark_group_stopped(index);
		return;
	}

	share = group_share(index);
	if (!share) {
		scx_bpf_error("zero group share during stopping");
		return;
	}

	group_key = index;
	vtime = bpf_map_lookup_elem(&group_vtime, &group_key);
	if (!vtime) {
		scx_bpf_error("group virtual-time lookup failed during stopping");
		return;
	}

	__sync_fetch_and_add(vtime,
			     runtime * scheduler_config.total_share / share);
	mark_group_stopped(index);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(group_init)
{
	__u32 i;

	bpf_for(i, 0, GROUP_SCHEDULER_MAX_SCHED_GROUPS) {
		s32 ret = scx_bpf_create_dsq(i, -1);

		if (ret)
			return ret;
	}
	return 0;
}

void BPF_STRUCT_OPS(group_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(group_scheduler_ops,
	       .select_cpu	= (void *)group_select_cpu,
	       .enqueue		= (void *)group_enqueue,
	       .dispatch	= (void *)group_dispatch,
	       .running		= (void *)group_running,
	       .stopping	= (void *)group_stopping,
	       .init		= (void *)group_init,
	       .exit		= (void *)group_exit,
	       .name		= "grp_cpu");
