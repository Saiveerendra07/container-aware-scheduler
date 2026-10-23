CC ?= cc
CLANG ?= clang
BPFTOOL ?= bpftool
PKG_CONFIG ?= pkg-config

SCX_ROOT ?= $(HOME)/scx-c-examples
BUILD_DIR ?= build
VMLINUX_BTF ?= /sys/kernel/btf/vmlinux

ARCH := $(shell uname -m)
ifeq ($(ARCH),x86_64)
TARGET_ARCH := x86
else ifeq ($(ARCH),aarch64)
TARGET_ARCH := arm64
else ifeq ($(ARCH),s390x)
TARGET_ARCH := s390
else
TARGET_ARCH := $(ARCH)
endif

SCX_INCLUDE := $(SCX_ROOT)/scheds/include
SCX_BPF_HEADER := $(SCX_INCLUDE)/scx/common.bpf.h
SCX_USER_HEADER := $(SCX_INCLUDE)/scx/common.h
LIBBPF_CFLAGS := $(shell $(PKG_CONFIG) --cflags libbpf)
LIBBPF_LIBS := $(shell $(PKG_CONFIG) --libs libbpf)

BPF_CFLAGS := -g -O2 -target bpf -mcpu=v3 -D__TARGET_ARCH_$(TARGET_ARCH) \
	-I$(BUILD_DIR) -Iinclude -I$(SCX_INCLUDE) \
	-I$(SCX_INCLUDE)/bpf-compat -I$(SCX_INCLUDE)/lib \
	$(LIBBPF_CFLAGS)
USER_CFLAGS := -std=gnu11 -Wall -Wextra -Werror -Iinclude \
	-I$(BUILD_DIR) -isystem $(SCX_INCLUDE) $(LIBBPF_CFLAGS)
USER_LIBS := $(LIBBPF_LIBS) -lelf -lz -lzstd -lpthread

.PHONY: all workloads clean check-deps

all: $(BUILD_DIR)/group_scheduler

workloads: $(BUILD_DIR)/cpu_worker $(BUILD_DIR)/cpu_supervisor

check-deps:
	@$(PKG_CONFIG) --atleast-version=1.5 libbpf || { \
		echo "libbpf 1.5 or newer is required (found $$(pkg-config --modversion libbpf))." >&2; \
		exit 1; \
	}
	@test -f "$(SCX_BPF_HEADER)" || { \
		echo "Missing sched_ext headers: $(SCX_BPF_HEADER)" >&2; \
		echo "Set SCX_ROOT to a sched-ext source tree with scheds/include." >&2; \
		exit 1; \
	}
	@test -f "$(SCX_USER_HEADER)" || { \
		echo "Missing sched_ext userspace header: $(SCX_USER_HEADER)" >&2; \
		exit 1; \
	}
	@test -r "$(VMLINUX_BTF)" || { \
		echo "Missing kernel BTF: $(VMLINUX_BTF)" >&2; \
		exit 1; \
	}

$(BUILD_DIR):
	mkdir -p "$@"

$(BUILD_DIR)/vmlinux.h: | $(BUILD_DIR) check-deps
	$(BPFTOOL) btf dump file "$(VMLINUX_BTF)" format c > "$@"

$(BUILD_DIR)/group_scheduler.bpf.o: src/group_scheduler.bpf.c \
		include/group_scheduler.h $(BUILD_DIR)/vmlinux.h | check-deps
	$(CLANG) $(BPF_CFLAGS) -c "$<" -o "$@"

$(BUILD_DIR)/group_scheduler.skel.h: $(BUILD_DIR)/group_scheduler.bpf.o
	$(BPFTOOL) gen skeleton "$<" name group_scheduler > "$@"

$(BUILD_DIR)/group_scheduler: src/group_scheduler.c \
		include/group_scheduler.h $(BUILD_DIR)/group_scheduler.skel.h | check-deps
	$(CC) $(USER_CFLAGS) "$<" -o "$@" $(USER_LIBS)

$(BUILD_DIR)/cpu_worker: workloads/cpu_worker.c | $(BUILD_DIR)
	$(CC) -O2 -Wall -Wextra -Werror -std=c11 "$<" -o "$@"

$(BUILD_DIR)/cpu_supervisor: workloads/cpu_supervisor.c | $(BUILD_DIR)
	$(CC) -O2 -Wall -Wextra -Werror -std=c11 "$<" -o "$@"

clean:
	@if [ "$(abspath $(BUILD_DIR))" != "$(CURDIR)/build" ]; then \
		echo "Refusing to clean outside the project build/ directory." >&2; \
		exit 1; \
	fi
	rm -rf -- "$(CURDIR)/build"
