# Copyright (c) Meta Platforms, Inc. and affiliates.
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile

from util import nproc, out_of_date

_9PFS_MSIZE = 1024 * 1024

# Script run as init in the virtual machine.
_INIT_TEMPLATE = r"""#!/bin/sh

# Having /proc from the host visible in the guest can confuse some commands. In
# particular, if BusyBox is configured with FEATURE_SH_STANDALONE, then busybox
# sh executes BusyBox applets using /proc/self/exe. So, before doing anything
# else, mount /proc (using the fully qualified executable path so that BusyBox
# doesn't use /proc/self/exe).
/bin/mount -t proc -o nosuid,nodev,noexec proc /proc

set -eu

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
{kdump_needs_nosmp}

trap 'poweroff -f' EXIT

umask 022

HOSTNAME=vmtest
VPORT_NAME=com.osandov.vmtest.0
RELEASE=$(uname -r)

# Set up overlayfs on the temporary directory containing this script.
mnt=$(dirname "$0")
mount -t tmpfs tmpfs "$mnt"
mkdir "$mnt/upper" "$mnt/work" "$mnt/merged"

mkdir "$mnt/upper/dev" "$mnt/upper/etc" "$mnt/upper/mnt"
mkdir -m 555 "$mnt/upper/proc" "$mnt/upper/sys"
mkdir -m 1777 "$mnt/upper/tmp"

mount -t overlay -o lowerdir=/,upperdir="$mnt/upper",workdir="$mnt/work" overlay "$mnt/merged"

# Mount core filesystems.
mount -t devtmpfs -o nosuid,noexec dev "$mnt/merged/dev"
mkdir "$mnt/merged/dev/shm"
mount -t tmpfs -o nosuid,nodev tmpfs "$mnt/merged/dev/shm"
mount -t proc -o nosuid,nodev,noexec proc "$mnt/merged/proc"
mount -t sysfs -o nosuid,nodev,noexec sys "$mnt/merged/sys"
# cgroup2 was added in Linux v4.5.
mount -t cgroup2 -o nosuid,nodev,noexec cgroup2 "$mnt/merged/sys/fs/cgroup" || true
# Ideally we'd just be able to create an opaque directory for /tmp on the upper
# layer. However, before Linux kernel commit 51f7e52dc943 ("ovl: share inode
# for hard link") (in v4.8), overlayfs doesn't handle hard links correctly,
# which breaks some tests.
mount -t tmpfs -o nosuid,nodev tmpfs "$mnt/merged/tmp"

# Pivot into the new root.
pivot_root "$mnt/merged" "$mnt/merged/mnt"
cd /
umount -l /mnt

# Load kernel modules.
mkdir -p "/lib/modules/$RELEASE"
mount -t 9p -o trans=virtio,cache=loose,ro,msize={_9PFS_MSIZE} modules "/lib/modules/$RELEASE"
for module in configs rng_core virtio_rng; do
	modprobe "$module"
done

# Create static device nodes.
grep -v '^#' "/lib/modules/$RELEASE/modules.devname" |
while read -r module name node; do
	name="/dev/$name"
	dev=${{node#?}}
	major=${{dev%%:*}}
	minor=${{dev##*:}}
	type=${{node%"${{dev}}"}}
	mkdir -p "$(dirname "$name")"
	mknod "$name" "$type" "$major" "$minor"
done
ln -s /proc/self/fd /dev/fd
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr

# Configure networking.
cat << EOF > /etc/hosts
127.0.0.1 localhost
::1 localhost
127.0.1.1 $HOSTNAME.localdomain $HOSTNAME
EOF
: > /etc/resolv.conf
hostname "$HOSTNAME"
ip link set lo up

# Find virtio port.
vport=
for vport_dir in /sys/class/virtio-ports/*; do
	if [ -r "$vport_dir/name" -a "$(cat "$vport_dir/name")" = "$VPORT_NAME" ]; then
		vport="${{vport_dir#/sys/class/virtio-ports/}}"
		break
	fi
done
if [ -z "$vport" ]; then
	echo "could not find virtio-port \"$VPORT_NAME\""
	exit 1
fi

cd {cwd}
set +e
sh -c {command}
rc=$?
set -e

echo "Exited with status $rc"
echo "$rc" > "/dev/$vport"
"""


def _compile(
    *args: str,
    CPPFLAGS: str = "",
    CFLAGS: str = "",
    LDFLAGS: str = "",
    LIBADD: str = "",
) -> None:
    # This mimics automake: the order of the arguments allows for the default
    # flags to be overridden by environment variables, and we use the same
    # default CFLAGS.
    cmd = [
        os.getenv("CC", "cc"),
        *shlex.split(CPPFLAGS),
        *shlex.split(os.getenv("CPPFLAGS", "")),
        *shlex.split(CFLAGS),
        *shlex.split(os.getenv("CFLAGS", "-g -O2")),
        *shlex.split(LDFLAGS),
        *shlex.split(os.getenv("LDFLAGS", "")),
        *args,
        *shlex.split(LIBADD),
        *shlex.split(os.getenv("LIBS", "")),
    ]
    print(" ".join([shlex.quote(arg) for arg in cmd]))
    subprocess.check_call(cmd)


def _build_onoatimehack(dir: Path) -> Path:
    dir.mkdir(parents=True, exist_ok=True)

    onoatimehack_so = dir / "onoatimehack.so"
    onoatimehack_c = (Path(__file__).parent / "onoatimehack.c").relative_to(Path.cwd())
    if out_of_date(onoatimehack_so, onoatimehack_c):
        _compile(
            "-o",
            str(onoatimehack_so),
            str(onoatimehack_c),
            CPPFLAGS="-D_GNU_SOURCE",
            CFLAGS="-fPIC",
            LDFLAGS="-shared",
            LIBADD="-ldl",
        )
    return onoatimehack_so


class LostVMError(Exception):
    pass


def run_in_vm(command: str, kernel_dir: Path, build_dir: Path) -> int:
    match = re.search(
        r"QEMU emulator version ([0-9]+(?:\.[0-9]+)*)",
        subprocess.check_output(
            ["qemu-system-x86_64", "-version"], universal_newlines=True
        ),
    )
    if not match:
        raise Exception("could not determine QEMU version")
    qemu_version = tuple(int(x) for x in match.group(1).split("."))

    # multidevs was added in QEMU 4.2.0.
    multidevs = ",multidevs=remap" if qemu_version >= (4, 2) else ""
    # QEMU's 9pfs O_NOATIME handling was fixed in 5.1.0. The fix was backported
    # to 5.0.1.
    env = os.environ.copy()
    if qemu_version < (5, 0, 1):
        onoatimehack_so = _build_onoatimehack(build_dir)
        env["LD_PRELOAD"] = f"{str(onoatimehack_so)}:{env.get('LD_PRELOAD', '')}"

    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        kvm_args = ["-cpu", "host", "-enable-kvm"]
    else:
        print(
            "warning: /dev/kvm cannot be accessed; falling back to emulation",
            file=sys.stderr,
        )
        kvm_args = []

    with tempfile.TemporaryDirectory(prefix="drgn-vmtest-") as temp_dir, socket.socket(
        socket.AF_UNIX
    ) as server_sock:
        temp_path = Path(temp_dir)
        socket_path = temp_path / "socket"
        server_sock.bind(str(socket_path))
        server_sock.listen()

        init = (temp_path / "init").resolve()
        with open(init, "w") as init_file:
            init_file.write(
                _INIT_TEMPLATE.format(
                    _9PFS_MSIZE=_9PFS_MSIZE,
                    cwd=shlex.quote(os.getcwd()),
                    command=shlex.quote(command),
                    kdump_needs_nosmp="" if kvm_args else "export KDUMP_NEEDS_NOSMP=1",
                )
            )
        os.chmod(init, 0o755)
        with subprocess.Popen(
            [
                # fmt: off
                "qemu-system-x86_64", *kvm_args,

                "-smp", str(nproc()), "-m", "2G",

                "-nodefaults", "-display", "none", "-serial", "mon:stdio",

                # This along with -append panic=-1 ensures that we exit on a
                # panic instead of hanging.
                "-no-reboot",

                "-virtfs",
                f"local,id=root,path=/,mount_tag=/dev/root,security_model=none,readonly=on{multidevs}",

                "-virtfs",
                f"local,path={kernel_dir},mount_tag=modules,security_model=none,readonly=on",

                "-device", "virtio-rng-pci",

                "-device", "virtio-serial",
                "-chardev", f"socket,id=vmtest,path={socket_path}",
                "-device",
                "virtserialport,chardev=vmtest,name=com.osandov.vmtest.0",

                "-kernel", str(kernel_dir / "vmlinuz"),
                "-append",
                f"rootfstype=9p rootflags=trans=virtio,cache=loose,msize={_9PFS_MSIZE} ro console=0,115200 panic=-1 crashkernel=256M init={init}",
                # fmt: on
            ],
            env=env,
        ):
            server_sock.settimeout(5)
            try:
                sock = server_sock.accept()[0]
            except socket.timeout:
                raise LostVMError(
                    f"QEMU did not connect within {server_sock.gettimeout()} seconds"
                )
            try:
                status_buf = bytearray()
                while True:
                    try:
                        buf = sock.recv(4)
                    except ConnectionResetError:
                        buf = b""
                    if not buf:
                        break
                    status_buf.extend(buf)
            finally:
                sock.close()
        if not status_buf:
            raise LostVMError("VM did not return status")
        if status_buf[-1] != ord("\n") or not status_buf[:-1].isdigit():
            raise LostVMError(f"VM returned invalid status: {repr(status_buf)[11:-1]}")
        return int(status_buf)


if __name__ == "__main__":
    import argparse
    import logging

    logging.basicConfig(
        format="%(asctime)s:%(levelname)s:%(name)s:%(message)s", level=logging.INFO
    )

    parser = argparse.ArgumentParser(
        description="run vmtest virtual machine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-d",
        "--directory",
        metavar="DIR",
        type=Path,
        default="build/vmtest",
        help="directory for build artifacts and downloaded kernels",
    )
    parser.add_argument(
        "--lost-status",
        metavar="STATUS",
        type=int,
        default=128,
        help="exit status if VM is lost",
    )
    parser.add_argument(
        "-k",
        "--kernel",
        default=argparse.SUPPRESS,
        help="kernel to use (default: latest available kernel)",
    )
    parser.add_argument(
        "command",
        type=str,
        nargs=argparse.REMAINDER,
        help="command to run in VM (default: sh -i)",
    )
    args = parser.parse_args()

    kernel = getattr(args, "kernel", "*")
    if kernel.startswith(".") or kernel.startswith("/"):
        kernel_dir = Path(kernel)
    else:
        from vmtest.download import download_kernels

        kernel_dir = next(download_kernels(args.directory, "x86_64", (kernel,)))

    try:
        command = " ".join(args.command) if args.command else "sh -i"
        sys.exit(run_in_vm(command, kernel_dir, args.directory))
    except LostVMError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(args.lost_status)
