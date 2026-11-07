//! `DiskBackend`: real files under a root, confined like the workspace.

use std::fs;
use std::os::unix::fs::symlink;

use linch_tools::vfs::conformance::backend_suite;
use linch_tools::vfs::{DiskBackend, FileBackend, VfsError};

#[tokio::test]
async fn disk_backend_passes_the_suite() {
    let dir = tempfile::tempdir().unwrap();
    let backend = DiskBackend::new(dir.path().join("offload")).unwrap();
    backend_suite(&backend).await;
}

#[tokio::test]
async fn files_are_real_files_under_a_created_root() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("a/b/offload");
    let backend = DiskBackend::new(&root).unwrap();
    assert_eq!(backend.root(), fs::canonicalize(&root).unwrap());
    backend.write("/results/x.txt", "payload").await.unwrap();
    assert_eq!(
        fs::read_to_string(root.join("results/x.txt")).unwrap(),
        "payload"
    );
    fs::write(root.join(".hidden"), "dot").unwrap();
    assert_eq!(
        backend.ls("").await.unwrap(),
        ["/.hidden", "/results/x.txt"],
        "hidden files are listed"
    );
}

#[tokio::test]
async fn paths_cannot_leave_the_root_through_a_symlink() {
    let dir = tempfile::tempdir().unwrap();
    let outside = dir.path().join("outside");
    fs::create_dir_all(&outside).unwrap();
    fs::write(outside.join("secret.txt"), "secret").unwrap();
    let root = dir.path().join("root");
    let backend = DiskBackend::new(&root).unwrap();
    symlink(&outside, root.join("link")).unwrap();

    let read = backend.read("/link/secret.txt").await;
    assert!(
        matches!(read, Err(VfsError::InvalidPath { .. })),
        "{read:?}"
    );
    let write = backend.write("/link/new.txt", "x").await;
    assert!(
        matches!(write, Err(VfsError::InvalidPath { .. })),
        "{write:?}"
    );
    let edit = backend.edit("/link/secret.txt", "secret", "x", false).await;
    assert!(
        matches!(edit, Err(VfsError::InvalidPath { .. })),
        "{edit:?}"
    );
    let delete = backend.delete("/link/secret.txt").await;
    assert!(
        matches!(delete, Err(VfsError::InvalidPath { .. })),
        "{delete:?}"
    );
    assert!(
        backend.ls("").await.unwrap().is_empty(),
        "links are not walked"
    );
    assert_eq!(
        fs::read_to_string(outside.join("secret.txt")).unwrap(),
        "secret"
    );
    assert!(!outside.join("new.txt").exists());
}

#[tokio::test]
async fn a_directory_is_not_a_file() {
    let dir = tempfile::tempdir().unwrap();
    let backend = DiskBackend::new(dir.path()).unwrap();
    backend.write("/d/inner.txt", "x").await.unwrap();
    let read = backend.read("/d").await;
    assert!(matches!(read, Err(VfsError::NotFound { .. })), "{read:?}");
    assert!(!backend.exists("/d").await.unwrap());
    backend.delete("/d").await.unwrap();
    assert!(
        backend.exists("/d/inner.txt").await.unwrap(),
        "delete skips dirs"
    );
}

#[tokio::test]
async fn invalid_utf8_on_disk_is_a_backend_error() {
    let dir = tempfile::tempdir().unwrap();
    let backend = DiskBackend::new(dir.path()).unwrap();
    fs::write(dir.path().join("bin"), [0xff, 0xfe]).unwrap();
    let read = backend.read("/bin").await;
    assert!(matches!(read, Err(VfsError::Backend(_))), "{read:?}");
}
