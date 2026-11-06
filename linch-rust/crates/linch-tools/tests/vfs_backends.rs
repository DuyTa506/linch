//! The virtual file backends: path rules, the conformance suite on every
//! in-process backend, and per-session lookup.

use std::sync::Arc;

use linch_core::SessionId;
use linch_tools::vfs::conformance::backend_suite;
use linch_tools::vfs::{
    FileBackend, MemoryBackend, MemorySessions, SessionFiles, SharedFiles, VfsError, normalize_path,
};

#[test]
fn paths_normalize_to_one_rooted_form() {
    assert_eq!(normalize_path("").unwrap(), "/");
    assert_eq!(normalize_path("/").unwrap(), "/");
    assert_eq!(normalize_path("a//b/").unwrap(), "/a/b");
    assert_eq!(normalize_path("/a/./b").unwrap(), "/a/b");
    assert_eq!(normalize_path("offload/x.txt").unwrap(), "/offload/x.txt");
}

#[test]
fn traversal_and_nul_are_refused() {
    for bad in ["/a/../b", "..", "a/..", "/a\0b"] {
        let error = normalize_path(bad).expect_err(bad);
        assert!(
            matches!(error, VfsError::InvalidPath { .. }),
            "{bad}: {error}"
        );
    }
}

#[tokio::test]
async fn memory_backend_passes_the_suite() {
    backend_suite(&MemoryBackend::new()).await;
}

#[tokio::test]
async fn each_session_gets_its_own_memory_backend() {
    let sessions = MemorySessions::new();
    let (a, b) = (SessionId::from("a"), SessionId::from("b"));
    sessions.backend(&a).write("/note", "from a").await.unwrap();
    assert_eq!(sessions.backend(&a).read("/note").await.unwrap(), "from a");
    assert!(!sessions.backend(&b).exists("/note").await.unwrap());

    sessions.forget(&a);
    assert!(
        !sessions.backend(&a).exists("/note").await.unwrap(),
        "a forgotten session starts empty"
    );
}

#[tokio::test]
async fn shared_files_serve_every_session_one_backend() {
    let backend: Arc<dyn FileBackend> = Arc::new(MemoryBackend::new());
    let shared = SharedFiles::new(backend.clone());
    let a = shared.backend(&SessionId::from("a"));
    a.write("/memories/x", "kept").await.unwrap();
    let b = shared.backend(&SessionId::from("b"));
    assert_eq!(b.read("/memories/x").await.unwrap(), "kept");
}
