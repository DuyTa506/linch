//! `CompositeBackend` routing and `ScopedBackend` confinement.

use std::sync::Arc;

use async_trait::async_trait;
use linch_tools::vfs::conformance::backend_suite;
use linch_tools::vfs::{CompositeBackend, FileBackend, MemoryBackend, ScopedBackend, VfsError};

fn memory() -> Arc<MemoryBackend> {
    Arc::new(MemoryBackend::new())
}

#[tokio::test]
async fn composite_backend_passes_the_suite() {
    let composite = CompositeBackend::new(memory())
        .with_route("/conformance/ls", memory())
        .unwrap();
    backend_suite(&composite).await;
}

#[tokio::test]
async fn the_longest_matching_route_wins() {
    let (default, memories, deep) = (memory(), memory(), memory());
    let composite = CompositeBackend::new(default.clone())
        .with_route("memories/", memories.clone())
        .unwrap()
        .with_route("/memories/deep", deep.clone())
        .unwrap();
    for path in [
        "/scratch/a",
        "/memories",
        "/memories/a",
        "/memories/deeper",
        "/memories/deep/b",
    ] {
        composite.write(path, path).await.unwrap();
    }
    assert_eq!(default.ls("").await.unwrap(), ["/scratch/a"]);
    assert_eq!(
        memories.ls("").await.unwrap(),
        ["/memories", "/memories/a", "/memories/deeper"]
    );
    assert_eq!(deep.ls("").await.unwrap(), ["/memories/deep/b"]);
    assert_eq!(
        composite.ls("/").await.unwrap(),
        [
            "/memories",
            "/memories/a",
            "/memories/deep/b",
            "/memories/deeper",
            "/scratch/a"
        ],
        "ls merges every backend, sorted"
    );
    assert_eq!(
        composite.read("/memories/deep/b").await.unwrap(),
        "/memories/deep/b"
    );
}

#[tokio::test]
async fn a_route_prefix_must_be_a_valid_path() {
    let error = CompositeBackend::new(memory())
        .with_route("/a/../b", memory())
        .err()
        .expect("a `..` route is refused");
    assert!(matches!(error, VfsError::InvalidPath { .. }), "{error}");
}

/// A backend whose every call fails.
struct Broken;

#[async_trait]
impl FileBackend for Broken {
    async fn read(&self, _: &str) -> Result<String, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn write(&self, _: &str, _: &str) -> Result<(), VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn edit(&self, _: &str, _: &str, _: &str, _: bool) -> Result<usize, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn ls(&self, _: &str) -> Result<Vec<String>, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn exists(&self, _: &str) -> Result<bool, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn delete(&self, _: &str) -> Result<(), VfsError> {
        Err(VfsError::Backend("down".into()))
    }
}

#[tokio::test]
async fn a_failing_route_fails_the_listing() {
    let composite = CompositeBackend::new(memory())
        .with_route("/memories", Arc::new(Broken))
        .unwrap();
    composite.write("/scratch/a", "x").await.unwrap();
    let ls = composite.ls("").await;
    assert!(matches!(ls, Err(VfsError::Backend(_))), "{ls:?}");
}

#[tokio::test]
async fn scoped_backend_passes_the_suite() {
    let scoped = ScopedBackend::new(memory(), "/tenants/a").unwrap();
    backend_suite(&scoped).await;
}

#[tokio::test]
async fn a_scope_sees_only_its_own_subtree() {
    let inner = memory();
    let a = ScopedBackend::new(inner.clone(), "tenants/a/").unwrap();
    let b = ScopedBackend::new(inner.clone(), "/tenants/ab").unwrap();
    inner.write("/tenants/other.txt", "o").await.unwrap();
    a.write("/notes.txt", "from a").await.unwrap();
    b.write("notes.txt", "from b").await.unwrap();

    assert_eq!(
        inner.ls("").await.unwrap(),
        [
            "/tenants/a/notes.txt",
            "/tenants/ab/notes.txt",
            "/tenants/other.txt"
        ]
    );
    assert_eq!(a.ls("").await.unwrap(), ["/notes.txt"]);
    assert_eq!(a.read("/notes.txt").await.unwrap(), "from a");
    assert_eq!(b.read("/notes.txt").await.unwrap(), "from b");

    let missing = a.read("/gone.txt").await;
    match missing {
        Err(VfsError::NotFound { path }) => {
            assert_eq!(path, "/gone.txt", "errors name scope paths, not the root");
        }
        other => panic!("expected NotFound, got {other:?}"),
    }
    let escape = a.read("/../ab/notes.txt").await;
    assert!(
        matches!(escape, Err(VfsError::InvalidPath { .. })),
        "{escape:?}"
    );
}
