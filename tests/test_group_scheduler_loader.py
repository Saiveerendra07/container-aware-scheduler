"""Synthetic loader lifecycle tests; the fake libbpf never loads BPF."""

import os
import select
import signal
import subprocess
import tempfile
import unittest
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "group_scheduler.c"
INCLUDE = ROOT / "include"


LIBBPF_HEADER = r"""
#ifndef TEST_LIBBPF_H
#define TEST_LIBBPF_H
#include <stdarg.h>
enum libbpf_print_level { LIBBPF_WARN, LIBBPF_INFO, LIBBPF_DEBUG };
struct bpf_map;
struct bpf_link;
typedef int (*libbpf_print_fn_t)(enum libbpf_print_level, const char *, va_list);
void libbpf_set_print(libbpf_print_fn_t callback);
long libbpf_get_error(const void *pointer);
int bpf_link__destroy(struct bpf_link *link);
struct bpf_link *bpf_map__attach_struct_ops(struct bpf_map *map);
#endif
"""

SCX_HEADER = r"""
#ifndef TEST_SCX_COMMON_H
#define TEST_SCX_COMMON_H
#include <stdint.h>
typedef uint32_t __u32;
#endif
"""

SKELETON_HEADER = r"""
#ifndef TEST_GROUP_SCHEDULER_SKEL_H
#define TEST_GROUP_SCHEDULER_SKEL_H
#include <bpf/libbpf.h>
#include "group_scheduler.h"
struct group_scheduler {
    struct { struct scheduler_config scheduler_config; } *rodata;
    struct { struct bpf_map *group_scheduler_ops; } maps;
    void *data;
};
struct group_scheduler *fake_group_scheduler_open(void);
int group_scheduler__load(struct group_scheduler *skel);
void group_scheduler__destroy(struct group_scheduler *skel);
int fake_uei_exited(struct group_scheduler *skel);
long fake_uei_report(struct group_scheduler *skel);
#define SCX_OPS_OPEN(ops, name) fake_group_scheduler_open()
#define UEI_SET_SIZE(skel, ops, uei) ((void)0)
#define UEI_EXITED(skel, uei) fake_uei_exited(skel)
#define UEI_REPORT(skel, uei) fake_uei_report(skel)
#endif
"""

FAKE_LIBBPF_SOURCE = r"""
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <bpf/libbpf.h>
#include "group_scheduler.skel.h"

struct bpf_map { int unused; };
struct bpf_link { int unused; };
static struct bpf_map fake_map;
static struct bpf_link fake_link;

static void record(const char *event)
{
    const char *path = getenv("FAKE_LOADER_LOG");
    FILE *stream;

    if (!path || !(stream = fopen(path, "a")))
        return;
    fprintf(stream, "%s\n", event);
    fclose(stream);
}

static int configured_event(const char *event)
{
    const char *selected = getenv("FAKE_LOADER_EVENT");
    return selected && strcmp(selected, event) == 0;
}

void libbpf_set_print(libbpf_print_fn_t callback)
{
    (void)callback;
    record("print");
    if (configured_event("before_open_signal"))
        raise(SIGTERM);
}

long libbpf_get_error(const void *pointer)
{
    intptr_t value = (intptr_t)pointer;
    return value < 0 && value >= -4095 ? (long)value : 0;
}

struct group_scheduler *fake_group_scheduler_open(void)
{
    struct group_scheduler *skel = calloc(1, sizeof(*skel));

    record("open");
    if (!skel)
        return NULL;
    skel->rodata = calloc(1, sizeof(*skel->rodata));
    if (!skel->rodata) {
        free(skel);
        return NULL;
    }
    skel->maps.group_scheduler_ops = &fake_map;
    skel->data = skel;
    return skel;
}

int group_scheduler__load(struct group_scheduler *skel)
{
    (void)skel;
    record("load");
    if (configured_event("load_signal"))
        raise(SIGTERM);
    return 0;
}

struct bpf_link *bpf_map__attach_struct_ops(struct bpf_map *map)
{
    (void)map;
    record("attach");
    if (configured_event("attach_signal"))
        raise(SIGTERM);
    if (configured_event("attach_failure"))
        return (struct bpf_link *)(intptr_t)-EPERM;
    return &fake_link;
}

int bpf_link__destroy(struct bpf_link *link)
{
    (void)link;
    record("detach");
    return configured_event("detach_failure") ? -EIO : 0;
}

void group_scheduler__destroy(struct group_scheduler *skel)
{
    record("destroy_skeleton");
    free(skel->rodata);
    free(skel);
}

int fake_uei_exited(struct group_scheduler *skel)
{
    (void)skel;
    return 0;
}

long fake_uei_report(struct group_scheduler *skel)
{
    (void)skel;
    record("uei_report");
    return 0;
}
"""


def executable(path, source):
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


class GroupSchedulerLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="synthetic_test_only_")
        root = Path(cls.temp.name)
        fake_include = root / "include"
        (fake_include / "bpf").mkdir(parents=True)
        (fake_include / "scx").mkdir()
        (fake_include / "bpf/libbpf.h").write_text(LIBBPF_HEADER)
        (fake_include / "scx/common.h").write_text(SCX_HEADER)
        (fake_include / "group_scheduler.skel.h").write_text(SKELETON_HEADER)
        stub_source = root / "fake_libbpf.c"
        stub_source.write_text(FAKE_LIBBPF_SOURCE)
        cls.binary = root / "group_scheduler_fake"
        subprocess.run([
            "gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            f"-I{fake_include}", f"-I{INCLUDE}", str(SOURCE),
            str(stub_source), "-o", str(cls.binary),
        ], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_loader(self, event, parent_pid=None):
        temp = tempfile.TemporaryDirectory(prefix="synthetic_test_only_")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        log_path = root / "calls.log"
        env = os.environ.copy()
        env.pop("CAS_PARENT_PID", None)
        env["FAKE_LOADER_LOG"] = str(log_path)
        if parent_pid is not None:
            env["CAS_PARENT_PID"] = str(parent_pid)
        if event:
            env["FAKE_LOADER_EVENT"] = event
        process = subprocess.Popen(
            [str(self.binary), "--fallback-share", "100",
             "--group", "101:100", "--group", "202:200"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        return process, log_path

    @staticmethod
    def log_events(path):
        return path.read_text(encoding="ascii").splitlines() if path.exists() else []

    def test_startup_signal_before_open_skips_bpf_load_and_attach(self):
        process, log_path = self.run_loader("before_open_signal")
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("open", self.log_events(log_path))
        self.assertNotIn("load", self.log_events(log_path))
        self.assertNotIn("attach", self.log_events(log_path))
        self.assertNotIn("GROUP_SCHEDULER_ATTACHED", stdout)

    def test_stale_parent_identity_aborts_before_libbpf_startup(self):
        process, log_path = self.run_loader(None, parent_pid=os.getpid() + 100000)
        _stdout, stderr = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("parent lifecycle guard", stderr)
        self.assertEqual(self.log_events(log_path), [])

    def test_parent_exit_delivers_signal_and_detaches_fake_loader(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            log_path = root / "calls.log"
            pid_path = root / "loader.pid"
            launcher = executable(root / "runner", """
import os, pathlib, subprocess, sys
environment = os.environ.copy()
environment['CAS_PARENT_PID'] = str(os.getpid())
child = subprocess.Popen(
    [sys.argv[1], '--fallback-share', '100', '--group', '101:100',
     '--group', '202:200'],
    env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[2]).write_text(str(child.pid), encoding='ascii')
""")
            env = os.environ.copy()
            env.pop("CAS_PARENT_PID", None)
            env.pop("FAKE_LOADER_EVENT", None)
            env["FAKE_LOADER_LOG"] = str(log_path)
            launched = subprocess.run(
                [str(launcher), str(self.binary), str(pid_path)], env=env,
                capture_output=True, text=True, timeout=3)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertTrue(pid_path.exists())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if "detach" in self.log_events(log_path):
                    break
                time.sleep(0.01)
            self.assertIn("detach", self.log_events(log_path))

    def test_signal_during_bpf_load_skips_attachment(self):
        process, log_path = self.run_loader("load_signal")
        stdout, stderr = process.communicate(timeout=3)
        events = self.log_events(log_path)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("load", events)
        self.assertNotIn("attach", events)
        self.assertIn("destroy_skeleton", events)
        self.assertNotIn("GROUP_SCHEDULER_ATTACHED", stdout)

    def test_signal_around_attach_destroys_link_without_readiness(self):
        process, log_path = self.run_loader("attach_signal")
        stdout, stderr = process.communicate(timeout=3)
        events = self.log_events(log_path)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("attach", events)
        self.assertIn("detach", events)
        self.assertIn("Scheduler attachment succeeded", stderr)
        self.assertIn("Scheduler link destroyed successfully", stderr)
        self.assertNotIn("GROUP_SCHEDULER_ATTACHED", stdout)

    def test_sigterm_after_readiness_destroys_attached_link(self):
        process, log_path = self.run_loader(None)
        ready = select.select([process.stdout], [], [], 3)[0]
        self.assertTrue(ready, "loader did not publish attach readiness")
        self.assertEqual(process.stdout.readline().strip(), "GROUP_SCHEDULER_ATTACHED")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("detach", self.log_events(log_path))
        self.assertIn("Scheduler link destroyed successfully", stderr)
        self.assertEqual(stdout, "")

    def test_attach_failure_does_not_report_readiness_or_detach(self):
        process, log_path = self.run_loader("attach_failure")
        stdout, stderr = process.communicate(timeout=3)
        events = self.log_events(log_path)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Failed to attach sched_ext operations", stderr)
        self.assertNotIn("GROUP_SCHEDULER_ATTACHED", stdout)
        self.assertNotIn("detach", events)

    def test_detach_failure_is_reported_as_failure(self):
        process, _log_path = self.run_loader("detach_failure")
        ready = select.select([process.stdout], [], [], 3)[0]
        self.assertTrue(ready, "loader did not publish attach readiness")
        self.assertEqual(process.stdout.readline().strip(), "GROUP_SCHEDULER_ATTACHED")
        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Failed to destroy scheduler link", stderr)


if __name__ == "__main__":
    unittest.main()