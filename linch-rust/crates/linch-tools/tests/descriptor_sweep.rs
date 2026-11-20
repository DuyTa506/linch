//! With only a few descriptors left, a search answers whole or with an error,
//! never with a part. One test in this binary: the descriptor limit is the
//! whole process's.

mod common;

use std::fs::{self, File};

use common::Workspace;
use linch_tools::fs::{ExecutionFs, GlobQuery, GrepMode, GrepQuery};
use linch_tools::vfs::{DiskBackend, FileBackend};
use rustix::process::{Resource, Rlimit, getrlimit, setrlimit};
use tokio_util::sync::CancellationToken;

#[tokio::test]
async fn with_few_descriptors_a_search_is_whole_or_an_error() {
    let ws = Workspace::new();
    ws.put(".gitignore", "ignored.txt\n");
    ws.put("ignored.txt", "needle\n");
    ws.put("top.txt", "needle\n");
    for dir in ["a", "a/b", "a/b/c", "a/b/c/d", "a/b/c/d/e", "m", "m/n", "z"] {
        ws.put(&format!("{dir}/x.txt"), "needle\n");
        ws.put(&format!("{dir}/y.txt"), "needle\n");
    }
    ws.put("m/.gitignore", "y.txt\n");
    let disk = DiskBackend::new(ws.root()).unwrap();
    let cancel = CancellationToken::new();
    let grep = GrepQuery {
        pattern: "needle".to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 1000,
    };
    let glob = GlobQuery {
        pattern: "**/*.txt".to_owned(),
        path: None,
    };
    // The whole truth, which also warms the blocking pool.
    let whole = ws.fs.grep(&grep, &cancel).await.unwrap();
    let want_grep: Vec<_> = whole.files.iter().map(|file| file.path.clone()).collect();
    let whole = ws.fs.glob(&glob, &cancel).await.unwrap();
    let want_glob: Vec<_> = whole.hits.iter().map(|hit| hit.path.clone()).collect();
    let want_ls = disk.ls("/").await.unwrap();
    let before = getrlimit(Resource::Nofile);
    let open = fs::read_dir("/proc/self/fd").unwrap().count();
    let limit = Rlimit {
        current: Some(u64::try_from(open + 40).unwrap()),
        maximum: before.maximum,
    };
    setrlimit(Resource::Nofile, limit).unwrap();
    let mut wrong = Vec::new();
    for left in 0..=12usize {
        let mut held: Vec<File> = std::iter::from_fn(|| File::open("/dev/null").ok()).collect();
        held.truncate(held.len().saturating_sub(left));
        let grepped = ws.fs.grep(&grep, &cancel).await;
        let globbed = ws.fs.glob(&glob, &cancel).await;
        let listed = disk.ls("/").await;
        drop(held);
        if let Ok(hits) = grepped {
            let names: Vec<_> = hits.files.iter().map(|file| file.path.clone()).collect();
            if names != want_grep || hits.left_out != 0 {
                wrong.push(format!(
                    "grep with {left} left: {names:?}, {} left out",
                    hits.left_out
                ));
            }
        }
        if let Ok(found) = globbed {
            let names: Vec<_> = found.hits.iter().map(|hit| hit.path.clone()).collect();
            if names != want_glob || found.left_out != 0 {
                wrong.push(format!(
                    "glob with {left} left: {names:?}, {} left out",
                    found.left_out
                ));
            }
        }
        if let Ok(names) = listed
            && names != want_ls
        {
            wrong.push(format!(
                "ls with {left} left: {} of {} names",
                names.len(),
                want_ls.len()
            ));
        }
    }
    setrlimit(Resource::Nofile, before).unwrap();
    assert!(
        wrong.is_empty(),
        "a part of the answer given as if it were whole:\n{}",
        wrong.join("\n")
    );
}
