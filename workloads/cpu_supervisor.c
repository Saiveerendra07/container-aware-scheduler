#define _GNU_SOURCE

#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t caught_signal;
static volatile sig_atomic_t signal_count;

struct child_error {
    int stage;
    int error_number;
};

static void record_signal(int signal_number)
{
    int saved_errno = errno;

    if (caught_signal == 0) {
        caught_signal = signal_number;
    }
    if (signal_count < 2) {
        signal_count++;
    }
    errno = saved_errno;
}

static int set_close_on_exec(int fd)
{
    int flags = fcntl(fd, F_GETFD);

    if (flags == -1 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC) == -1) {
        return -1;
    }
    return 0;
}

static int write_all(int fd, const void *buffer, size_t length)
{
    const char *cursor = buffer;

    while (length > 0) {
        ssize_t written = write(fd, cursor, length);
        if (written > 0) {
            cursor += written;
            length -= (size_t)written;
        } else if (written == -1 && errno == EINTR) {
            continue;
        } else {
            return -1;
        }
    }
    return 0;
}

static uint64_t timeval_to_microseconds(const struct timeval *value)
{
    return (uint64_t)value->tv_sec * UINT64_C(1000000)
         + (uint64_t)value->tv_usec;
}

static int write_metadata(const char *path,
                          pid_t worker_pid,
                          int have_worker_pid,
                          uint64_t elapsed_ns,
                          int have_elapsed,
                          const struct rusage *usage,
                          int have_usage,
                          int worker_exit_status,
                          int have_worker_exit_status,
                          int worker_signal,
                          int exec_errno,
                          int supervisor_errno)
{
    int fd = open(path, O_WRONLY | O_TRUNC | O_CLOEXEC);
    if (fd == -1) {
        return -1;
    }

    char pid_text[32], elapsed_text[32], user_text[32], system_text[32];
    char exit_text[16], line[256];
    if (have_worker_pid) {
        (void)snprintf(pid_text, sizeof(pid_text), "%ld", (long)worker_pid);
    } else {
        (void)snprintf(pid_text, sizeof(pid_text), "NA");
    }
    if (have_elapsed) {
        (void)snprintf(elapsed_text, sizeof(elapsed_text), "%" PRIu64,
                       elapsed_ns);
    } else {
        (void)snprintf(elapsed_text, sizeof(elapsed_text), "NA");
    }
    if (have_usage) {
        (void)snprintf(user_text, sizeof(user_text), "%" PRIu64,
                       timeval_to_microseconds(&usage->ru_utime));
        (void)snprintf(system_text, sizeof(system_text), "%" PRIu64,
                       timeval_to_microseconds(&usage->ru_stime));
    } else {
        (void)snprintf(user_text, sizeof(user_text), "NA");
        (void)snprintf(system_text, sizeof(system_text), "NA");
    }
    if (have_worker_exit_status) {
        (void)snprintf(exit_text, sizeof(exit_text), "%d", worker_exit_status);
    } else {
        (void)snprintf(exit_text, sizeof(exit_text), "NA");
    }
    int length = snprintf(line, sizeof(line),
                          "%s\t%s\t%s\t%s\t%s\t%d\t%d\t%d\n",
                          pid_text, elapsed_text, user_text, system_text,
                          exit_text, worker_signal, exec_errno,
                          supervisor_errno);
    if (length < 0 || (size_t)length >= sizeof(line)) {
        close(fd);
        errno = EOVERFLOW;
        return -1;
    }

    int result = write_all(fd, line, (size_t)length);
    int saved_errno = errno;
    if (close(fd) == -1 && result == 0) {
        return -1;
    }
    errno = saved_errno;
    return result;
}

static int write_failure_metadata(const char *path, int error_number)
{
    int fd = open(path, O_WRONLY | O_TRUNC | O_CLOEXEC);
    if (fd == -1) {
        return -1;
    }

    int failed = dprintf(fd, "NA\tNA\tNA\tNA\tNA\t0\t0\t%d\n",
                         error_number) < 0;
    int saved_errno = errno;
    if (close(fd) == -1 && !failed) {
        failed = 1;
        saved_errno = errno;
    }
    errno = saved_errno;
    return failed ? -1 : 0;
}

static int monotonic_now(struct timespec *value)
{
    return clock_gettime(CLOCK_MONOTONIC, value);
}

static int cancellation_requested(const char *path)
{
    char value;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd == -1) {
        return errno == ENOENT ? 0 : -1;
    }
    ssize_t amount;
    do {
        amount = read(fd, &value, 1);
    } while (amount == -1 && errno == EINTR);
    int saved_errno = errno;
    (void)close(fd);
    errno = saved_errno;
    if (amount == -1) {
        return -1;
    }
    return amount == 1 && value == '1';
}

/*
 * Return 1 if a live member remains, 0 only after a complete empty scan, and
 * -1 if the scan was incomplete. The unreaped worker leader pins the PGID.
 */
static int group_has_live_descendant(pid_t group_id, pid_t worker_pid)
{
    DIR *proc = opendir("/proc");
    if (proc == NULL) {
        return -1;
    }

    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(proc);
        if (entry == NULL) {
            int read_error = errno;
            if (closedir(proc) == -1 && read_error == 0) {
                read_error = errno;
            }
            if (read_error != 0) {
                errno = read_error;
                return -1;
            }
            return 0;
        }

        char *end = NULL;
        long candidate = strtol(entry->d_name, &end, 10);
        if (end == entry->d_name || *end != '\0' || candidate <= 0
            || candidate == (long)worker_pid) {
            continue;
        }

        char path[64];
        int length = snprintf(path, sizeof(path), "/proc/%ld/stat", candidate);
        if (length < 0 || (size_t)length >= sizeof(path)) {
            (void)closedir(proc);
            errno = EOVERFLOW;
            return -1;
        }
        FILE *stat_file = fopen(path, "r");
        if (stat_file == NULL) {
            /* ENOENT is the normal race where a task exited after readdir. */
            if (errno == ENOENT) {
                continue;
            }
            int open_error = errno;
            (void)closedir(proc);
            errno = open_error;
            return -1;
        }

        char line[4096];
        errno = 0;
        if (fgets(line, sizeof(line), stat_file) == NULL) {
            int read_error = ferror(stat_file) && errno != 0 ? errno : EIO;
            (void)fclose(stat_file);

            /* Recheck EOF/read errors: ENOENT means the task vanished. */
            FILE *probe = fopen(path, "r");
            if (probe == NULL && errno == ENOENT) {
                continue;
            }
            if (probe != NULL) {
                (void)fclose(probe);
            }
            (void)closedir(proc);
            errno = read_error;
            return -1;
        }

        char *after_command = strrchr(line, ')');
        char state = '\0';
        long parent = 0;
        long process_group = 0;
        if (after_command == NULL
            || sscanf(after_command + 1, " %c %ld %ld", &state,
                      &parent, &process_group) != 3) {
            (void)fclose(stat_file);
            (void)closedir(proc);
            errno = EIO;
            return -1;
        }
        (void)fclose(stat_file);
        if (process_group == (long)group_id
            && state != 'Z' && state != 'X') {
            (void)closedir(proc);
            return 1;
        }
    }
}

static int signal_worker_group(pid_t group_id, int signal_number)
{
    if (kill(-group_id, signal_number) == -1 && errno != ESRCH) {
        return -1;
    }
    return 0;
}

static uint64_t timespec_difference_ns(const struct timespec *end,
                                       const struct timespec *start)
{
    int64_t seconds = (int64_t)end->tv_sec - (int64_t)start->tv_sec;
    int64_t nanoseconds = (int64_t)end->tv_nsec - (int64_t)start->tv_nsec;

    return (uint64_t)(seconds * INT64_C(1000000000) + nanoseconds);
}

static struct timespec timespec_add_seconds(struct timespec value, time_t seconds)
{
    value.tv_sec += seconds;
    return value;
}

static int timespec_is_after_or_equal(const struct timespec *left,
                                      const struct timespec *right)
{
    return left->tv_sec > right->tv_sec
        || (left->tv_sec == right->tv_sec && left->tv_nsec >= right->tv_nsec);
}

static int forward_pending_signal(pid_t worker_pid, int worker_group_ready)
{
    int signal_number = caught_signal;

    if (signal_number == 0) {
        return 0;
    }

    int result = worker_group_ready
        ? signal_worker_group(worker_pid, signal_number)
        : (kill(worker_pid, signal_number) == -1 && errno != ESRCH ? -1 : 0);
    if (result == -1) {
        return -1;
    }
    return 0;
}

static int wait_for_worker(pid_t worker_pid,
                           int *wait_status,
                           struct rusage *usage,
                           int *supervisor_errno,
                           const char *cancel_path,
                           int initial_signal,
                           int worker_group_ready)
{
    int termination_started = 0;
    int force_killed = 0;
    struct timespec kill_deadline = {0, 0};

    for (;;) {
        int requested_signal = initial_signal;
        if (requested_signal == 0 && caught_signal != 0) {
            requested_signal = caught_signal;
        }
        if (requested_signal == 0) {
            int cancelled = cancellation_requested(cancel_path);
            if (cancelled == -1) {
                *supervisor_errno = errno;
                requested_signal = SIGTERM;
            } else if (cancelled == 1) {
                requested_signal = SIGTERM;
            }
        }

        if (requested_signal != 0 && !termination_started) {
            int sent = worker_group_ready
                ? signal_worker_group(worker_pid, requested_signal)
                : (kill(worker_pid, requested_signal) == -1 && errno != ESRCH
                    ? -1 : 0);
            if (sent == -1) {
                *supervisor_errno = errno;
            }
            termination_started = 1;
            struct timespec now;
            if (monotonic_now(&now) == 0) {
                kill_deadline = timespec_add_seconds(now, 2);
            } else {
                if (worker_group_ready) {
                    (void)signal_worker_group(worker_pid, SIGKILL);
                } else {
                    (void)kill(worker_pid, SIGKILL);
                }
                force_killed = 1;
            }
        } else if (caught_signal != 0 && signal_count >= 2 && !force_killed) {
            if (worker_group_ready) {
                (void)signal_worker_group(worker_pid, SIGKILL);
            } else {
                (void)kill(worker_pid, SIGKILL);
            }
            force_killed = 1;
        }

        if (termination_started && !force_killed) {
            struct timespec now;
            if (monotonic_now(&now) == 0
                && timespec_is_after_or_equal(&now, &kill_deadline)) {
                if (worker_group_ready) {
                    (void)signal_worker_group(worker_pid, SIGKILL);
                } else {
                    (void)kill(worker_pid, SIGKILL);
                }
                force_killed = 1;
            }
        }

        siginfo_t child_info;
        memset(&child_info, 0, sizeof(child_info));
        if (waitid(P_PID, (id_t)worker_pid, &child_info,
                   WEXITED | WNOHANG | WNOWAIT) == -1) {
            if (errno == EINTR) {
                continue;
            }
            int wait_error = errno;
            *supervisor_errno = wait_error;
            if (worker_group_ready) {
                (void)signal_worker_group(worker_pid, SIGKILL);
            } else {
                (void)kill(worker_pid, SIGKILL);
            }
            if (wait_error == ECHILD) {
                return -1;
            }
            for (;;) {
                pid_t waited = wait4(worker_pid, wait_status, 0, usage);
                if (waited == worker_pid) {
                    return 0;
                }
                if (waited == -1 && errno == EINTR) {
                    continue;
                }
                *supervisor_errno = errno;
                return -1;
            }
        }

        if (child_info.si_pid == worker_pid) {
            if (!termination_started) {
                if (worker_group_ready
                    && signal_worker_group(worker_pid, SIGTERM) == -1) {
                    *supervisor_errno = errno;
                }
                termination_started = 1;
                struct timespec now;
                if (monotonic_now(&now) == -1) {
                    if (worker_group_ready) {
                        (void)signal_worker_group(worker_pid, SIGKILL);
                    }
                    force_killed = 1;
                } else {
                    kill_deadline = timespec_add_seconds(now, 2);
                    if (!worker_group_ready) {
                        force_killed = 1;
                    }
                }
            }

            int descendants = worker_group_ready
                ? group_has_live_descendant(worker_pid, worker_pid) : 0;
            if (descendants < 0) {
                if (*supervisor_errno == 0) {
                    *supervisor_errno = errno;
                }
                if (worker_group_ready) {
                    (void)signal_worker_group(worker_pid, SIGKILL);
                }
                /*
                 * Do not treat an incomplete scan as an empty group. SIGKILL
                 * is the bounded best-effort cleanup; metadata records that
                 * descendant cleanup could not be verified.
                 */
                force_killed = 1;
                for (;;) {
                    pid_t waited = wait4(worker_pid, wait_status, 0, usage);
                    if (waited == worker_pid) {
                        return 0;
                    }
                    if (waited == -1 && errno == EINTR) {
                        continue;
                    }
                    *supervisor_errno = errno;
                    return -1;
                }
            }
            if (descendants == 0) {
                for (;;) {
                    pid_t waited = wait4(worker_pid, wait_status, 0, usage);
                    if (waited == worker_pid) {
                        return 0;
                    }
                    if (waited == -1 && errno == EINTR) {
                        continue;
                    }
                    *supervisor_errno = errno;
                    return -1;
                }
            }
        }

        struct timespec pause = {0, 10000000};
        while (nanosleep(&pause, &pause) == -1 && errno == EINTR) {
            break;
        }
    }
}

int main(int argc, char **argv)
{
    if (argc != 5) {
        fprintf(stderr,
                "Usage: %s metadata_path worker_path duration_seconds cancel_path\n",
                argv[0]);
        return 2;
    }

    const char *metadata_path = argv[1];
    const char *worker_path = argv[2];
    const char *duration = argv[3];
    const char *cancel_path = argv[4];
    int exec_pipe[2];

    if (pipe(exec_pipe) == -1) {
        int error_number = errno;
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: pipe: %s\n", strerror(error_number));
        return 125;
    }
    if (set_close_on_exec(exec_pipe[1]) == -1) {
        int error_number = errno;
        close(exec_pipe[0]);
        close(exec_pipe[1]);
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: fcntl: %s\n", strerror(error_number));
        return 125;
    }

    sigset_t handled_signals;
    sigset_t original_mask;
    sigemptyset(&handled_signals);
    sigaddset(&handled_signals, SIGINT);
    sigaddset(&handled_signals, SIGTERM);

    if (sigprocmask(SIG_BLOCK, &handled_signals, &original_mask) == -1) {
        int error_number = errno;
        close(exec_pipe[0]);
        close(exec_pipe[1]);
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: sigprocmask: %s\n",
                strerror(error_number));
        return 125;
    }

    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = record_signal;
    sigemptyset(&action.sa_mask);
    sigaddset(&action.sa_mask, SIGINT);
    sigaddset(&action.sa_mask, SIGTERM);
    action.sa_flags = 0;
    if (sigaction(SIGINT, &action, NULL) == -1
        || sigaction(SIGTERM, &action, NULL) == -1) {
        int error_number = errno;
        close(exec_pipe[0]);
        close(exec_pipe[1]);
        (void)sigprocmask(SIG_SETMASK, &original_mask, NULL);
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: sigaction: %s\n",
                strerror(error_number));
        return 125;
    }

    struct timespec start_time;
    if (monotonic_now(&start_time) == -1) {
        int error_number = errno;
        close(exec_pipe[0]);
        close(exec_pipe[1]);
        (void)sigprocmask(SIG_SETMASK, &original_mask, NULL);
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: clock_gettime: %s\n",
                strerror(error_number));
        return 125;
    }

    pid_t worker_pid = fork();
    if (worker_pid == -1) {
        int error_number = errno;
        close(exec_pipe[0]);
        close(exec_pipe[1]);
        (void)sigprocmask(SIG_SETMASK, &original_mask, NULL);
        (void)write_failure_metadata(metadata_path, error_number);
        fprintf(stderr, "cpu_supervisor: fork: %s\n", strerror(error_number));
        return 125;
    }

    if (worker_pid == 0) {
        close(exec_pipe[0]);

        struct sigaction default_action;
        memset(&default_action, 0, sizeof(default_action));
        default_action.sa_handler = SIG_DFL;
        sigemptyset(&default_action.sa_mask);
        (void)sigaction(SIGINT, &default_action, NULL);
        (void)sigaction(SIGTERM, &default_action, NULL);
        (void)sigprocmask(SIG_SETMASK, &original_mask, NULL);

        if (setpgid(0, 0) == -1) {
            struct child_error child_failure = {1, errno};
            (void)write_all(exec_pipe[1], &child_failure,
                            sizeof(child_failure));
            _exit(125);
        }

        char *const worker_argv[] = {
            (char *)worker_path,
            (char *)duration,
            NULL
        };
        execv(worker_path, worker_argv);

        struct child_error child_failure = {2, errno};
        (void)write_all(exec_pipe[1], &child_failure, sizeof(child_failure));
        _exit(child_failure.error_number == ENOENT ? 127 : 126);
    }

    close(exec_pipe[1]);
    int worker_group_ready = 1;
    int supervisor_errno = 0;
    if (setpgid(worker_pid, worker_pid) == -1
        && errno != EACCES && errno != ESRCH) {
        worker_group_ready = 0;
        supervisor_errno = errno;
    }
    int initial_signal = 0;
    if (sigprocmask(SIG_SETMASK, &original_mask, NULL) == -1) {
        /* Keep the child owned and reapable even if unblocking fails. */
        int error_number = errno;
        supervisor_errno = error_number;
        initial_signal = SIGTERM;
        fprintf(stderr, "cpu_supervisor: restore signal mask: %s\n",
                strerror(error_number));
    }

    struct child_error child_failure = {0, 0};
    size_t exec_error_bytes = 0;
    while (exec_error_bytes < sizeof(child_failure)) {
        ssize_t amount = read(exec_pipe[0],
                              (char *)&child_failure + exec_error_bytes,
                              sizeof(child_failure) - exec_error_bytes);
        if (amount > 0) {
            exec_error_bytes += (size_t)amount;
        } else if (amount == 0) {
            break;
        } else if (errno == EINTR) {
            if (caught_signal != 0) {
                (void)forward_pending_signal(worker_pid, worker_group_ready);
            }
        } else {
            int error_number = errno;
            close(exec_pipe[0]);
            struct rusage ignored_usage;
            int ignored_status;
            int wait_error = 0;
            (void)wait_for_worker(worker_pid, &ignored_status, &ignored_usage,
                                  &wait_error, cancel_path, SIGTERM,
                                  worker_group_ready);
            (void)write_failure_metadata(metadata_path, error_number);
            fprintf(stderr, "cpu_supervisor: exec status pipe: %s\n",
                    strerror(error_number));
            return 125;
        }
    }
    close(exec_pipe[0]);

    int exec_errno = 0;
    if (exec_error_bytes != 0 && exec_error_bytes != sizeof(child_failure)) {
        supervisor_errno = EIO;
    } else if (exec_error_bytes == sizeof(child_failure)) {
        if (child_failure.stage == 1) {
            supervisor_errno = child_failure.error_number;
        } else if (child_failure.stage == 2) {
            exec_errno = child_failure.error_number;
        } else {
            supervisor_errno = EIO;
        }
    }
    if (exec_errno != 0) {
        fprintf(stderr, "cpu_supervisor: exec %s: %s\n", worker_path,
                strerror(exec_errno));
    }

    int wait_status = 0;
    struct rusage usage;
    memset(&usage, 0, sizeof(usage));
    int waited = wait_for_worker(worker_pid, &wait_status, &usage,
                                 &supervisor_errno, cancel_path,
                                 initial_signal, worker_group_ready);

    struct timespec end_time;
    int have_elapsed = monotonic_now(&end_time) == 0;
    uint64_t elapsed_ns = have_elapsed
        ? timespec_difference_ns(&end_time, &start_time)
        : 0;
    if (!have_elapsed && supervisor_errno == 0) {
        supervisor_errno = errno;
    }

    int worker_exit_status = 0;
    int have_worker_exit_status = 0;
    int worker_signal = 0;
    if (waited == 0 && WIFEXITED(wait_status)) {
        worker_exit_status = WEXITSTATUS(wait_status);
        have_worker_exit_status = 1;
    } else if (waited == 0 && WIFSIGNALED(wait_status)) {
        worker_signal = WTERMSIG(wait_status);
    } else if (supervisor_errno == 0) {
        supervisor_errno = ECHILD;
    }

    if (write_metadata(metadata_path, worker_pid, 1, elapsed_ns,
                       have_elapsed, &usage, waited == 0,
                       worker_exit_status, have_worker_exit_status,
                       worker_signal, exec_errno, supervisor_errno) == -1) {
        int error_number = errno;
        fprintf(stderr, "cpu_supervisor: write metadata: %s\n",
                strerror(error_number));
        return 125;
    }

    if (supervisor_errno != 0 || waited != 0) {
        return 125;
    }
    if (exec_errno != 0) {
        return exec_errno == ENOENT ? 127 : 126;
    }
    if (WIFEXITED(wait_status)) {
        return WEXITSTATUS(wait_status);
    }
    if (WIFSIGNALED(wait_status)) {
        int status = 128 + WTERMSIG(wait_status);
        return status > 255 ? 255 : status;
    }
    return 125;
}
