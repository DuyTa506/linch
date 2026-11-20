//! The sandbox's seccomp filter: no new sockets but the IP ones the policy
//! allows, no `io_uring` (whose requests the filter would not see), no
//! namespaces, no privileged calls, and no call that changes another
//! process's limits or scheduling.
//!
//! Landlock (ABI 6, which the sandbox needs) restricts TCP but not UDP or
//! raw sockets, and connecting to a pathname Unix socket only from ABI 9;
//! a socket the server's user can reach (a D-Bus session bus, a Docker
//! socket) would let a command start something unconfined. So with the
//! network on only `AF_INET` and `AF_INET6` sockets open, and with it off
//! no `socket` does. `socketpair` still works.
//!
//! Commands run as the server's user, and Landlock does not cover the
//! calls that act on another process by pid without ptrace rights:
//! `prlimit64`, the `sched_set*` calls, `setpriority` and `ioprio_set`
//! work only on the caller (pid or `who` 0).
//!
//! The filter is applied to the calling thread only; the sandbox applies
//! it on the thread that forks the command, which inherits it.
//!
//! Each rule is one self-contained block that starts by matching the
//! syscall number and ends in a return, so no jump crosses a block.

use seccompiler::{BpfProgram, sock_filter};

/// The audit architecture of this build: a syscall made through another
/// ABI (i386 `int 0x80` on x86_64) kills the process instead of slipping
/// past the filter.
#[cfg(target_arch = "x86_64")]
const NATIVE: u32 = 62 | 0x8000_0000 | 0x4000_0000;
#[cfg(target_arch = "aarch64")]
const NATIVE: u32 = 183 | 0x8000_0000 | 0x4000_0000;

/// Whether the filter can be built here.
pub(super) const SUPPORTED: bool = cfg!(any(target_arch = "x86_64", target_arch = "aarch64"));

/// `BPF_LD | BPF_W | BPF_ABS`.
const BPF_LD_W_ABS: u16 = 0x20;
const BPF_JEQ_K: u16 = 0x05 | 0x10;
const BPF_JGE_K: u16 = 0x05 | 0x30;
const BPF_JSET_K: u16 = 0x05 | 0x40;
const BPF_RET_K: u16 = 0x06;

/// Offsets in `struct seccomp_data`: the syscall number, the architecture
/// and the low 32 bits of the arguments (little-endian targets). Every
/// argument the filter reads is an `int` (or, for `clone`, has its
/// namespace flags in the low half), so the high half never matters.
const NR: u32 = 0;
const ARCH: u32 = 4;
const ARG0: u32 = 16;
const ARG1: u32 = 24;

const RET_ALLOW: u32 = 0x7fff_0000;
const RET_KILL_PROCESS: u32 = 0x8000_0000;
const RET_ERRNO: u32 = 0x0005_0000;
const RET_EACCES: u32 = RET_ERRNO | libc::EACCES as u32;
const RET_EPERM: u32 = RET_ERRNO | libc::EPERM as u32;
const RET_ENOSYS: u32 = RET_ERRNO | libc::ENOSYS as u32;

/// x32 syscalls on x86_64 carry this bit; the filter refuses them all.
const X32_BIT: u32 = 0x4000_0000;

/// `clone` flags that make a namespace. `CLONE_NEWTIME` is left out: in
/// `clone` its bit is part of the exit signal (only `unshare` and `clone3`
/// take it, and both are refused).
const CLONE_NAMESPACES: u32 = (libc::CLONE_NEWNS
    | libc::CLONE_NEWCGROUP
    | libc::CLONE_NEWUTS
    | libc::CLONE_NEWIPC
    | libc::CLONE_NEWUSER
    | libc::CLONE_NEWPID
    | libc::CLONE_NEWNET) as u32;

/// `setpriority`'s `PRIO_PROCESS` and `ioprio_set`'s `IOPRIO_WHO_PROCESS`.
const PRIO_PROCESS: u32 = 0;
const IOPRIO_WHO_PROCESS: u32 = 1;

/// Calls refused with `EPERM` whatever their arguments: privileged calls
/// (kernel modules, kexec, reboot, swap, mounts, `chroot`), as defence in
/// depth behind the root check at open; the user's keyrings, BPF, perf and
/// userfaultfd, which reach past the sandbox; System V IPC and POSIX
/// message queues, whose objects are checked by user id; and namespaces.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const REFUSED: [libc::c_long; 42] = [
    libc::SYS_init_module,
    libc::SYS_finit_module,
    libc::SYS_delete_module,
    libc::SYS_kexec_load,
    libc::SYS_kexec_file_load,
    libc::SYS_reboot,
    libc::SYS_swapon,
    libc::SYS_swapoff,
    libc::SYS_mount,
    libc::SYS_umount2,
    libc::SYS_pivot_root,
    libc::SYS_chroot,
    libc::SYS_open_tree,
    libc::SYS_move_mount,
    libc::SYS_fsopen,
    libc::SYS_fsconfig,
    libc::SYS_fsmount,
    libc::SYS_fspick,
    libc::SYS_mount_setattr,
    libc::SYS_keyctl,
    libc::SYS_add_key,
    libc::SYS_request_key,
    libc::SYS_bpf,
    libc::SYS_perf_event_open,
    libc::SYS_userfaultfd,
    libc::SYS_unshare,
    libc::SYS_setns,
    libc::SYS_shmget,
    libc::SYS_shmat,
    libc::SYS_shmctl,
    libc::SYS_msgget,
    libc::SYS_msgsnd,
    libc::SYS_msgrcv,
    libc::SYS_msgctl,
    libc::SYS_semget,
    libc::SYS_semop,
    libc::SYS_semtimedop,
    libc::SYS_semctl,
    libc::SYS_mq_open,
    libc::SYS_mq_unlink,
    libc::SYS_mq_timedsend,
    libc::SYS_mq_timedreceive,
];

/// Calls whose first argument is a pid: allowed for 0 (the caller) only.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const SELF_ONLY: [libc::c_long; 5] = [
    libc::SYS_prlimit64,
    libc::SYS_sched_setscheduler,
    libc::SYS_sched_setparam,
    libc::SYS_sched_setaffinity,
    libc::SYS_sched_setattr,
];

fn stmt(code: u16, k: u32) -> sock_filter {
    sock_filter {
        code,
        jt: 0,
        jf: 0,
        k,
    }
}

fn jump(code: u16, k: u32, jt: u8, jf: u8) -> sock_filter {
    sock_filter { code, jt, jf, k }
}

/// A syscall number as the filter compares it.
fn nr(sys: libc::c_long) -> u32 {
    u32::try_from(sys).expect("syscall numbers are small")
}

/// The block for syscall `sys`: skipped unless the number matches, else
/// `body`, which must end in a return. Within a block, no jump leaves it.
fn block(sys: libc::c_long, body: Vec<sock_filter>) -> Vec<sock_filter> {
    let skip = u8::try_from(body.len()).expect("a block is short");
    let mut block = vec![jump(BPF_JEQ_K, nr(sys), 0, skip)];
    block.extend(body);
    block
}

/// `sys` fails with `ret`.
fn refuse(sys: libc::c_long, ret: u32) -> Vec<sock_filter> {
    block(sys, vec![stmt(BPF_RET_K, ret)])
}

/// `sys` is allowed when every `(argument, value)` pair holds; otherwise
/// it fails with `ret`.
fn allow_when(sys: libc::c_long, checks: &[(u32, u32)], ret: u32) -> Vec<sock_filter> {
    let mut body = Vec::new();
    let last = checks.len();
    for (i, &(arg, value)) in checks.iter().enumerate() {
        // On a mismatch, skip the checks left and the allow, to `ret`.
        let to_refuse = u8::try_from(2 * (last - i) - 1).expect("few checks");
        body.push(stmt(BPF_LD_W_ABS, arg));
        body.push(jump(BPF_JEQ_K, value, 0, to_refuse));
    }
    body.push(stmt(BPF_RET_K, RET_ALLOW));
    body.push(stmt(BPF_RET_K, ret));
    block(sys, body)
}

/// `sys` is allowed when its first argument is one of `values`; otherwise
/// it fails with `ret`.
fn allow_one_of(sys: libc::c_long, values: &[u32], ret: u32) -> Vec<sock_filter> {
    let mut body = vec![stmt(BPF_LD_W_ABS, ARG0)];
    let count = values.len();
    for (i, &value) in values.iter().enumerate() {
        // On a match, skip the values left and the refusal, to allow.
        let to_allow = u8::try_from(count - i).expect("few values");
        body.push(jump(BPF_JEQ_K, value, to_allow, 0));
    }
    body.push(stmt(BPF_RET_K, ret));
    body.push(stmt(BPF_RET_K, RET_ALLOW));
    block(sys, body)
}

/// The filter for a sandbox with or without `network`.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
pub(super) fn program(network: bool) -> BpfProgram {
    let mut program = vec![
        stmt(BPF_LD_W_ABS, ARCH),
        jump(BPF_JEQ_K, NATIVE, 1, 0),
        stmt(BPF_RET_K, RET_KILL_PROCESS),
        stmt(BPF_LD_W_ABS, NR),
    ];
    if cfg!(target_arch = "x86_64") {
        program.extend([jump(BPF_JGE_K, X32_BIT, 0, 1), stmt(BPF_RET_K, RET_EACCES)]);
    }
    program.extend(refuse(libc::SYS_io_uring_setup, RET_EACCES));
    program.extend(match network {
        true => allow_one_of(
            libc::SYS_socket,
            &[libc::AF_INET as u32, libc::AF_INET6 as u32],
            RET_EACCES,
        ),
        false => refuse(libc::SYS_socket, RET_EACCES),
    });
    for sys in REFUSED {
        program.extend(refuse(sys, RET_EPERM));
    }
    // `clone3` passes its flags in memory the filter cannot read.
    program.extend(refuse(libc::SYS_clone3, RET_ENOSYS));
    program.extend(block(
        libc::SYS_clone,
        vec![
            stmt(BPF_LD_W_ABS, ARG0),
            jump(BPF_JSET_K, CLONE_NAMESPACES, 0, 1),
            stmt(BPF_RET_K, RET_EPERM),
            stmt(BPF_RET_K, RET_ALLOW),
        ],
    ));
    for sys in SELF_ONLY {
        program.extend(allow_when(sys, &[(ARG0, 0)], RET_EPERM));
    }
    program.extend(allow_when(
        libc::SYS_setpriority,
        &[(ARG0, PRIO_PROCESS), (ARG1, 0)],
        RET_EPERM,
    ));
    program.extend(allow_when(
        libc::SYS_ioprio_set,
        &[(ARG0, IOPRIO_WHO_PROCESS), (ARG1, 0)],
        RET_EPERM,
    ));
    program.push(stmt(BPF_RET_K, RET_ALLOW));
    program
}

/// Apply the filter to the calling thread (setting `no_new_privs`).
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
pub(super) fn apply(network: bool) -> Result<(), String> {
    seccompiler::apply_filter(&program(network)).map_err(|error| error.to_string())
}

#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
pub(super) fn apply(_network: bool) -> Result<(), String> {
    Err("seccomp filter not built for this architecture".into())
}

#[cfg(all(test, any(target_arch = "x86_64", target_arch = "aarch64")))]
mod tests {
    use super::*;

    const EPERM: u32 = RET_EPERM;

    /// Every jump lands inside the program, and it ends allowing what no
    /// rule named.
    #[test]
    fn jumps_stay_in_the_program() {
        for network in [false, true] {
            let program = program(network);
            let len = program.len();
            for (at, op) in program.iter().enumerate() {
                if op.code & 0x07 == 0x05 {
                    assert!(at + 1 + usize::from(op.jt) < len, "{network} {at}");
                    assert!(at + 1 + usize::from(op.jf) < len, "{network} {at}");
                }
            }
            assert_eq!(program[len - 1], stmt(BPF_RET_K, RET_ALLOW));
        }
    }

    /// Run `program` on one syscall, as the kernel would: `args` are the
    /// low 32 bits of the first three arguments.
    #[allow(clippy::panic)]
    fn eval(program: &[sock_filter], arch: u32, nr: u32, args: [u32; 3]) -> u32 {
        let (mut at, mut acc) = (0, 0);
        loop {
            let op = &program[at];
            match op.code {
                BPF_LD_W_ABS => {
                    acc = match op.k {
                        NR => nr,
                        ARCH => arch,
                        ARG0 => args[0],
                        ARG1 => args[1],
                        32 => args[2],
                        other => panic!("load at {other}"),
                    }
                }
                BPF_JEQ_K | BPF_JGE_K | BPF_JSET_K => {
                    let taken = match op.code {
                        BPF_JEQ_K => acc == op.k,
                        BPF_JGE_K => acc >= op.k,
                        _ => acc & op.k != 0,
                    };
                    at += usize::from(if taken { op.jt } else { op.jf });
                }
                BPF_RET_K => return op.k,
                other => panic!("op {other}"),
            }
            at += 1;
        }
    }

    #[test]
    fn the_filter_decides_as_documented() {
        let socket = nr(libc::SYS_socket);
        let io_uring = nr(libc::SYS_io_uring_setup);
        let read = nr(libc::SYS_read);
        let (unix, inet, inet6) = (
            libc::AF_UNIX as u32,
            libc::AF_INET as u32,
            libc::AF_INET6 as u32,
        );
        let on = program(true);
        let off = program(false);
        for program in [&on, &off] {
            assert_eq!(eval(program, NATIVE, read, [0; 3]), RET_ALLOW);
            assert_eq!(eval(program, NATIVE, socket, [unix, 0, 0]), RET_EACCES);
            assert_eq!(eval(program, NATIVE, io_uring, [0; 3]), RET_EACCES);
            assert_eq!(eval(program, 0x4000_0003, read, [0; 3]), RET_KILL_PROCESS);
            if cfg!(target_arch = "x86_64") {
                let x32 = X32_BIT | socket;
                assert_eq!(eval(program, NATIVE, x32, [inet, 0, 0]), RET_EACCES);
            }
        }
        assert_eq!(eval(&on, NATIVE, socket, [inet, 0, 0]), RET_ALLOW);
        assert_eq!(eval(&on, NATIVE, socket, [inet6, 0, 0]), RET_ALLOW);
        assert_eq!(eval(&off, NATIVE, socket, [inet, 0, 0]), RET_EACCES);
        assert_eq!(eval(&off, NATIVE, socket, [inet6, 0, 0]), RET_EACCES);
    }

    /// Calls only a privileged process could make are refused outright,
    /// as defence in depth behind the root check at open.
    #[test]
    fn privileged_calls_are_refused() {
        let calls = [
            libc::SYS_init_module,
            libc::SYS_finit_module,
            libc::SYS_delete_module,
            libc::SYS_kexec_load,
            libc::SYS_kexec_file_load,
            libc::SYS_reboot,
            libc::SYS_swapon,
            libc::SYS_swapoff,
            libc::SYS_mount,
            libc::SYS_umount2,
            libc::SYS_pivot_root,
            libc::SYS_chroot,
            libc::SYS_open_tree,
            libc::SYS_move_mount,
            libc::SYS_fsopen,
            libc::SYS_fsconfig,
            libc::SYS_fsmount,
            libc::SYS_fspick,
            libc::SYS_mount_setattr,
        ];
        for network in [false, true] {
            let program = program(network);
            for call in calls {
                assert_eq!(eval(&program, NATIVE, nr(call), [0; 3]), EPERM, "{call}");
            }
        }
    }

    /// System V IPC and POSIX message queues are checked by user id, the
    /// server's, so another program of that user is reachable through them.
    #[test]
    fn ipc_calls_are_refused() {
        let calls = [
            libc::SYS_shmget,
            libc::SYS_shmat,
            libc::SYS_shmctl,
            libc::SYS_msgget,
            libc::SYS_msgsnd,
            libc::SYS_msgrcv,
            libc::SYS_msgctl,
            libc::SYS_semget,
            libc::SYS_semop,
            libc::SYS_semtimedop,
            libc::SYS_semctl,
            libc::SYS_mq_open,
            libc::SYS_mq_unlink,
            libc::SYS_mq_timedsend,
            libc::SYS_mq_timedreceive,
        ];
        for network in [false, true] {
            let program = program(network);
            for call in calls {
                assert_eq!(eval(&program, NATIVE, nr(call), [0; 3]), EPERM, "{call}");
            }
        }
    }

    /// Kernel facilities that reach past the sandbox (the user's keyrings,
    /// BPF, perf, userfaultfd) and new namespaces are refused.
    #[test]
    fn namespaces_and_kernel_facilities_are_refused() {
        let calls = [
            libc::SYS_keyctl,
            libc::SYS_add_key,
            libc::SYS_request_key,
            libc::SYS_bpf,
            libc::SYS_perf_event_open,
            libc::SYS_userfaultfd,
            libc::SYS_unshare,
            libc::SYS_setns,
        ];
        let clone = nr(libc::SYS_clone);
        let clone3 = nr(libc::SYS_clone3);
        let fork = libc::SIGCHLD as u32;
        let thread =
            (libc::CLONE_VM | libc::CLONE_FS | libc::CLONE_FILES | libc::CLONE_THREAD) as u32;
        for network in [false, true] {
            let program = program(network);
            for call in calls {
                assert_eq!(eval(&program, NATIVE, nr(call), [0; 3]), EPERM, "{call}");
            }
            // `clone3` hides its flags from the filter; glibc falls back to
            // `clone` on ENOSYS.
            assert_eq!(eval(&program, NATIVE, clone3, [0; 3]), RET_ENOSYS);
            assert_eq!(eval(&program, NATIVE, clone, [fork, 0, 0]), RET_ALLOW);
            assert_eq!(eval(&program, NATIVE, clone, [thread, 0, 0]), RET_ALLOW);
            for flag in [
                libc::CLONE_NEWNS,
                libc::CLONE_NEWCGROUP,
                libc::CLONE_NEWUTS,
                libc::CLONE_NEWIPC,
                libc::CLONE_NEWUSER,
                libc::CLONE_NEWPID,
                libc::CLONE_NEWNET,
            ] {
                let flags = fork | flag as u32;
                let decided = eval(&program, NATIVE, clone, [flags, 0, 0]);
                assert_eq!(decided, EPERM, "{flag:#x}");
            }
        }
    }

    /// A command changes its own limits, priority and affinity (pid or
    /// `who` 0), never another process's: the server runs as the same user.
    #[test]
    fn resource_calls_reach_only_the_caller() {
        let pid_first = [
            libc::SYS_prlimit64,
            libc::SYS_sched_setscheduler,
            libc::SYS_sched_setparam,
            libc::SYS_sched_setaffinity,
            libc::SYS_sched_setattr,
        ];
        let setpriority = nr(libc::SYS_setpriority);
        let ioprio = nr(libc::SYS_ioprio_set);
        let (process, pgrp, user) = (0, 1, 2);
        let (io_process, io_pgrp, io_user) = (1, 2, 3);
        for network in [false, true] {
            let program = program(network);
            for call in pid_first {
                assert_eq!(
                    eval(&program, NATIVE, nr(call), [0; 3]),
                    RET_ALLOW,
                    "{call}"
                );
                assert_eq!(
                    eval(&program, NATIVE, nr(call), [42, 0, 0]),
                    EPERM,
                    "{call}"
                );
            }
            let decide = |args| eval(&program, NATIVE, setpriority, args);
            assert_eq!(decide([process, 0, 5]), RET_ALLOW);
            assert_eq!(decide([process, 42, 5]), EPERM);
            assert_eq!(decide([pgrp, 0, 5]), EPERM);
            assert_eq!(decide([user, 0, 5]), EPERM);
            let decide = |args| eval(&program, NATIVE, ioprio, args);
            assert_eq!(decide([io_process, 0, 7]), RET_ALLOW);
            assert_eq!(decide([io_process, 42, 7]), EPERM);
            assert_eq!(decide([io_pgrp, 0, 7]), EPERM);
            assert_eq!(decide([io_user, 0, 7]), EPERM);
        }
    }

    /// With the network on, only IP sockets open: no Unix, vsock, packet,
    /// crypto or Bluetooth socket.
    #[test]
    fn the_network_opens_ip_sockets_only() {
        let socket = nr(libc::SYS_socket);
        let on = program(true);
        for family in [
            libc::AF_UNIX,
            libc::AF_VSOCK,
            libc::AF_PACKET,
            libc::AF_ALG,
            libc::AF_BLUETOOTH,
        ] {
            let decided = eval(&on, NATIVE, socket, [family as u32, 0, 0]);
            assert_eq!(decided, RET_EACCES, "{family}");
        }
    }
}
