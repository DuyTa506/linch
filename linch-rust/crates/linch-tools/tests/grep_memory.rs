//! The memory one `Grep` may hold. This file holds one test: the peak
//! resident memory is a property of the whole process, so another test
//! running beside it (one that compiles a large pattern, say) would be
//! counted, or would hide the growth.

mod common;

use common::{Workspace, call};
use linch_tools::Grep;
use serde_json::json;

const MIB: u64 = 1024 * 1024;

/// Peak resident memory of this process so far, in bytes.
fn peak_rss() -> u64 {
    let status = std::fs::read_to_string("/proc/self/status").unwrap();
    let line = status
        .lines()
        .find(|line| line.starts_with("VmHWM:"))
        .unwrap();
    let kb: u64 = line.split_whitespace().nth(1).unwrap().parse().unwrap();
    kb * 1024
}

/// A 2 MiB file and a 16-byte pattern, both the model's, must not cost the
/// server hundreds of megabytes: the matcher's DFA cache is bounded.
#[tokio::test]
async fn one_grep_call_cannot_hold_hundreds_of_megabytes() {
    let ws = Workspace::new();
    let mut seed = 0x9e37_79b9_7f4a_7c15u64;
    let mut bits = String::with_capacity(2 * 1024 * 1024 + 32_000);
    while bits.len() < 2 * 1024 * 1024 {
        for _ in 0..100 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            bits.push(if seed & 1 == 0 { '0' } else { '1' });
        }
        bits.push('\n');
    }
    ws.put("bits.txt", &bits);
    drop(bits);
    let tool = Grep::new(ws.fs.clone());
    let before = peak_rss();
    let query = json!({"pattern": "(0|1)*1(0|1){22}", "output_mode": "count"});
    call(&tool, query).await.unwrap();
    let grown = peak_rss().saturating_sub(before);
    eprintln!("peak memory grew by {} MiB", grown / MIB);
    assert!(
        grown < 128 * MIB,
        "one Grep over a 2 MiB file raised the peak memory by {} MiB",
        grown / MIB
    );
}
