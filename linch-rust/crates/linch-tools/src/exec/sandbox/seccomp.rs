//! The sandbox's seccomp filter: no new sockets but the network ones the
//! policy allows, and no `io_uring` (whose requests the filter would not
//! see).
//!
//! Landlock (ABI 6, which the sandbox needs) restricts TCP but not UDP or
//! raw sockets, and connecting to a pathname Unix socket only from ABI 9;
//! a socket the server's user can reach (a D-Bus session bus, a Docker
//! socket) would let a command start something unconfined. So
//! `socket(AF_UNIX, ...)` always fails, and with the network off every
//! `socket` does. `socketpair` still works.
//!
//! The filter is applied to the calling thread only; the sandbox applies
//! it on the thread that forks the command, which inherits it.

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
const BPF_RET_K: u16 = 0x06;

/// Offsets in `struct seccomp_data`: the syscall number, the architecture
/// and the low 32 bits of the first argument (little-endian targets).
const NR: u32 = 0;
const ARCH: u32 = 4;
const ARG0: u32 = 16;

const RET_ALLOW: u32 = 0x7fff_0000;
const RET_KILL_PROCESS: u32 = 0x8000_0000;
const RET_EACCES: u32 = 0x0005_0000 | libc::EACCES as u32;

/// x32 syscalls on x86_64 carry this bit; the filter refuses them all.
const X32_BIT: u32 = 0x4000_0000;

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

/// The filter for a sandbox with or without `network`.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
pub(super) fn program(network: bool) -> BpfProgram {
    let socket = libc::SYS_socket as u32;
    let io_uring = libc::SYS_io_uring_setup as u32;
    let x32 = cfg!(target_arch = "x86_64");
    let mut program = vec![
        stmt(BPF_LD_W_ABS, ARCH),
        jump(BPF_JEQ_K, NATIVE, 1, 0),
        stmt(BPF_RET_K, RET_KILL_PROCESS),
        stmt(BPF_LD_W_ABS, NR),
    ];
    // A jump skips that many instructions; `allow` and `deny` are the
    // last two.
    if x32 {
        // To `deny` past: io_uring, socket, [arg load, unix,] allow.
        let to_deny = if network { 5 } else { 3 };
        program.push(jump(BPF_JGE_K, X32_BIT, to_deny, 0));
    }
    if network {
        program.extend([
            jump(BPF_JEQ_K, io_uring, 4, 0),
            jump(BPF_JEQ_K, socket, 0, 2),
            stmt(BPF_LD_W_ABS, ARG0),
            jump(BPF_JEQ_K, libc::AF_UNIX as u32, 1, 0),
        ]);
    } else {
        program.extend([
            jump(BPF_JEQ_K, io_uring, 2, 0),
            jump(BPF_JEQ_K, socket, 1, 0),
            stmt(BPF_RET_K, RET_ALLOW),
        ]);
        program.push(stmt(BPF_RET_K, RET_EACCES));
        return program;
    }
    program.push(stmt(BPF_RET_K, RET_ALLOW));
    program.push(stmt(BPF_RET_K, RET_EACCES));
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

    /// Every jump lands inside the program, and the last two instructions
    /// are allow then deny.
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
            assert_eq!(program[len - 2], stmt(BPF_RET_K, RET_ALLOW));
            assert_eq!(program[len - 1], stmt(BPF_RET_K, RET_EACCES));
        }
    }

    /// Run `program` on one syscall, as the kernel would.
    fn eval(program: &[sock_filter], arch: u32, nr: u32, arg0: u32) -> u32 {
        let (mut at, mut acc) = (0, 0);
        loop {
            let op = &program[at];
            match op.code {
                BPF_LD_W_ABS => {
                    acc = match op.k {
                        NR => nr,
                        ARCH => arch,
                        ARG0 => arg0,
                        other => panic!("load at {other}"),
                    }
                }
                BPF_JEQ_K | BPF_JGE_K => {
                    let taken = match op.code {
                        BPF_JEQ_K => acc == op.k,
                        _ => acc >= op.k,
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
        let socket = libc::SYS_socket as u32;
        let io_uring = libc::SYS_io_uring_setup as u32;
        let read = libc::SYS_read as u32;
        let (unix, inet, inet6) = (
            libc::AF_UNIX as u32,
            libc::AF_INET as u32,
            libc::AF_INET6 as u32,
        );
        let on = program(true);
        let off = program(false);
        for program in [&on, &off] {
            assert_eq!(eval(program, NATIVE, read, 0), RET_ALLOW);
            assert_eq!(eval(program, NATIVE, socket, unix), RET_EACCES);
            assert_eq!(eval(program, NATIVE, io_uring, 0), RET_EACCES);
            assert_eq!(eval(program, 0x4000_0003, read, 0), RET_KILL_PROCESS);
            if cfg!(target_arch = "x86_64") {
                assert_eq!(eval(program, NATIVE, X32_BIT | socket, inet), RET_EACCES);
            }
        }
        assert_eq!(eval(&on, NATIVE, socket, inet), RET_ALLOW);
        assert_eq!(eval(&on, NATIVE, socket, inet6), RET_ALLOW);
        assert_eq!(eval(&off, NATIVE, socket, inet), RET_EACCES);
        assert_eq!(eval(&off, NATIVE, socket, inet6), RET_EACCES);
    }
}
