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
LIBBPF_CFLAGS = $(shell $(PKG_CONFIG) --cflags libbpf)
LIBBPF_LIBS = $(shell $(PKG_CONFIG) --libs libbpf)

BPF_CFLAGS = -g -O2 -target bpf -mcpu=v3 -D__TARGET_ARCH_$(TARGET_ARCH) \
	-I$(BUILD_DIR) -Iinclude -I$(SCX_INCLUDE) \
	-I$(SCX_INCLUDE)/bpf-compat -I$(SCX_INCLUDE)/lib \
	$(LIBBPF_CFLAGS)
USER_CFLAGS = -std=gnu11 -Wall -Wextra -Werror -Iinclude \
	-I$(BUILD_DIR) -isystem $(SCX_INCLUDE) $(LIBBPF_CFLAGS)
USER_LIBS = $(LIBBPF_LIBS) -lelf -lz -lzstd -lpthread

.PHONY: all workloads clean check-deps

all: $(BUILD_DIR)/group_scheduler

workloads: $(BUILD_DIR)/cpu_worker $(BUILD_DIR)/cpu_supervisor

check-deps:
	@command -v "$(firstword $(CC))" >/dev/null 2>&1 || { \
		echo "Missing C compiler '$(firstword $(CC))'; set CC to a working compiler." >&2; exit 1; }
	@command -v "$(firstword $(CLANG))" >/dev/null 2>&1 || { \
		echo "Missing Clang executable '$(firstword $(CLANG))'; set CLANG to Clang with BPF support." >&2; exit 1; }
	@$(CLANG) -print-targets 2>/dev/null | grep -Eiq '(^|[[:space:]])bpf([[:space:]]|$$)' || { \
		echo "Clang '$(CLANG)' does not report a BPF target; use a Clang build with the BPF backend." >&2; exit 1; }
	@command -v "$(firstword $(BPFTOOL))" >/dev/null 2>&1 || { \
		echo "Missing bpftool executable '$(firstword $(BPFTOOL))'; set BPFTOOL to a working bpftool." >&2; exit 1; }
	@$(BPFTOOL) version >/dev/null 2>&1 || { \
		echo "bpftool '$(BPFTOOL)' cannot run; provide a working bpftool executable." >&2; exit 1; }
	@command -v "$(firstword $(PKG_CONFIG))" >/dev/null 2>&1 || { \
		echo "Missing pkg-config executable '$(firstword $(PKG_CONFIG))'; set PKG_CONFIG to a working pkg-config." >&2; exit 1; }
	@$(PKG_CONFIG) --atleast-version=1.5 libbpf || { \
		echo "libbpf 1.5 or newer is required (found $$($(PKG_CONFIG) --modversion libbpf 2>/dev/null || echo unavailable))." >&2; \
		exit 1; \
	}
	@$(PKG_CONFIG) --cflags --libs libbpf >/dev/null || { \
		echo "pkg-config could not provide libbpf compiler/linker flags; repair the libbpf .pc metadata or PKG_CONFIG_PATH." >&2; exit 1; }
	@printf '%s\n' '#include <bpf/libbpf.h>' | $(CC) $$($(PKG_CONFIG) --cflags libbpf) -x c -fsyntax-only - || { \
		echo "Cannot compile <bpf/libbpf.h>; provide matching libbpf development headers and pkg-config cflags." >&2; exit 1; }
	@printf '%s\n' 'int main(void) { return 0; }' | $(CC) $$($(PKG_CONFIG) --cflags libbpf) -x c -o /dev/null - $$($(PKG_CONFIG) --libs libbpf) -lelf -lz -lzstd -lpthread || { \
		echo "Cannot resolve userspace link dependencies; provide libbpf plus -lelf -lz -lzstd -lpthread development libraries." >&2; exit 1; }
	@test -r "$(SCX_BPF_HEADER)" || { \
		echo "Missing sched_ext headers: $(SCX_BPF_HEADER)" >&2; \
		echo "Set SCX_ROOT to a sched-ext source tree with scheds/include." >&2; \
		exit 1; \
	}
	@test -r "$(SCX_USER_HEADER)" || { \
		echo "Missing sched_ext userspace header: $(SCX_USER_HEADER)" >&2; \
		echo "Set SCX_ROOT to the compatible reference sched_ext checkout." >&2; \
		exit 1; \
	}
	@test -d "$(SCX_INCLUDE)/bpf-compat" || { \
		echo "Missing sched_ext compatibility headers: $(SCX_INCLUDE)/bpf-compat; select the expected reference checkout with SCX_ROOT." >&2; exit 1; }
	@test -d "$(SCX_INCLUDE)/lib" || { \
		echo "Missing sched_ext support headers: $(SCX_INCLUDE)/lib; select the expected reference checkout with SCX_ROOT." >&2; exit 1; }
	@test -r "$(VMLINUX_BTF)" || { \
		echo "Missing kernel BTF: $(VMLINUX_BTF)" >&2; \
		echo "Use the readable vmlinux BTF from the target build/runtime kernel; do not substitute another kernel's BTF." >&2; \
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
