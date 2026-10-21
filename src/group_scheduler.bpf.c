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
	if (!runtime)
		return;

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
