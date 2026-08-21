"""Versioned E2B template used by VMA-managed session sandboxes."""

from e2b import Template


TEMPLATE_NAME = "vma-hardened"
TEMPLATE_CANDIDATE = f"{TEMPLATE_NAME}:v20260821-1"
TEMPLATE_CPU_COUNT = 2
TEMPLATE_MEMORY_MB = 2048


_HARDEN = r"""
set -eux

install -d -o user -g user -m 0700 /workspace
install -d -o user -g user -m 0700 /mnt/memory
install -d -o root -g root -m 0755 /mnt /mnt/session /mnt/session/uploads
install -d -o user -g user -m 0700 /mnt/session/outputs

usermod -d /workspace -s /bin/bash user

if getent group sudo >/dev/null; then
  gpasswd -d user sudo || true
fi

if getent group adm >/dev/null; then
  gpasswd -d user adm || true
fi

rm -f /etc/sudoers.d/*

if dpkg-query -W -f='${Status}' sudo 2>/dev/null | grep -q 'ok installed'; then
  DEBIAN_FRONTEND=noninteractive apt-get purge -y sudo
fi

rm -rf /home/user

# /tmp and /var/tmp keep their distro-default writable, sticky permissions;
# no override needed. All durable tenant work belongs in /workspace or
# /mnt/memory.
find / -xdev \
  \( -path /workspace -o -path /tmp -o -path /var/tmp -o -path /mnt/memory -o -path /mnt/session/outputs \) -prune \
  -o -user user -exec chown -h root:root {} +

find / -xdev \
  \( -path /workspace -o -path /tmp -o -path /var/tmp -o -path /mnt/memory -o -path /mnt/session/outputs \) -prune \
  -o -type d -perm /0022 -exec chmod go-w {} +

find / -xdev -type f -perm /6000 -exec chmod a-s {} +
"""


_ATTEST = r"""
set -eu
PATH=/usr/bin:/bin
export PATH

test "$(/usr/bin/id -un)" = user
test "$(/usr/bin/id -u)" -ne 0
test "$(pwd)" = /workspace
test -x /usr/bin/python3
test ! -w /usr/bin
test ! -w /usr/lib
/usr/bin/python3 -I -S -c 'import json, os, pwd, stat, sys'

if test -x /usr/bin/sudo && /usr/bin/sudo -n true >/dev/null 2>&1; then
  exit 41
fi

test -w /workspace
test -w /mnt/memory
test ! -w /mnt/session
test ! -w /mnt/session/uploads
test -w /mnt/session/outputs

test -d /tmp
test -w /tmp
test -k /tmp
"""


template = (
    Template()
    # VMA's root bootstrap and verification scripts invoke python3 directly.
    .from_python_image("3.12")
    .apt_install(
        [
            "bash",
            "ca-certificates",
            "curl",
            "git",
            "jq",
            "python3-minimal",
            "ripgrep",
            "unzip",
            "zip",
        ]
    )
    .run_cmd(_HARDEN, user="root")
    .set_envs({"HOME": "/workspace"})
    # Keep this before the build-time guest attestation: it verifies pwd.
    .set_workdir("/workspace")
    .run_cmd(_ATTEST, user="user")
    # The last selected user is persisted as the sandbox execution default.
    .set_user("user")
)
