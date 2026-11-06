//! `PgFileBackend`: the vfs conformance suite, namespaces, byte order and
//! edits that do not lose each other.

use linch_postgres::PgFileBackend;
use linch_tools::vfs::FileBackend;
use linch_tools::vfs::conformance::backend_suite;

use crate::common::TestDb;

#[tokio::test]
async fn file_backend_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("files_conformance").await else {
        return;
    };
    backend_suite(&PgFileBackend::new(db.pool.clone(), "ns")).await;
    db.finish().await;
}

#[tokio::test]
async fn namespaces_hold_separate_files() {
    let Some(db) = TestDb::migrated("files_namespaces").await else {
        return;
    };
    let a = PgFileBackend::new(db.pool.clone(), "a");
    let b = PgFileBackend::new(db.pool.clone(), "b");
    a.write("/notes.txt", "from a").await.unwrap();
    assert!(!b.exists("/notes.txt").await.unwrap());
    assert!(b.ls("").await.unwrap().is_empty());
    b.write("/notes.txt", "from b").await.unwrap();
    assert_eq!(a.read("/notes.txt").await.unwrap(), "from a");
    b.delete("/notes.txt").await.unwrap();
    assert_eq!(a.ls("/").await.unwrap(), ["/notes.txt"]);
    db.finish().await;
}

#[tokio::test]
async fn listings_sort_by_bytes_like_every_backend() {
    let Some(db) = TestDb::migrated("files_order").await else {
        return;
    };
    let files = PgFileBackend::new(db.pool.clone(), "ns");
    let paths = ["/a", "/B", "/_x", "/Z", "/\u{e9}"];
    for path in paths {
        files.write(path, "x").await.unwrap();
    }
    let mut sorted = paths.map(str::to_owned).to_vec();
    sorted.sort();
    assert_eq!(files.ls("").await.unwrap(), sorted);
    db.finish().await;
}

#[tokio::test]
async fn concurrent_edits_of_one_file_all_land() {
    let Some(db) = TestDb::migrated("files_edits").await else {
        return;
    };
    let files = PgFileBackend::new(db.pool.clone(), "ns");
    let letters = "abcdefghij";
    let spaced: Vec<String> = letters.chars().map(String::from).collect();
    files.write("/f", &spaced.join(" ")).await.unwrap();
    let mut edits = tokio::task::JoinSet::new();
    for letter in letters.chars() {
        let files = files.clone();
        edits.spawn(async move {
            let (old, new) = (letter.to_string(), letter.to_ascii_uppercase().to_string());
            files.edit("/f", &old, &new, false).await
        });
    }
    for edited in edits.join_all().await {
        assert_eq!(edited.unwrap(), 1);
    }
    let upper: Vec<String> = letters.to_uppercase().chars().map(String::from).collect();
    assert_eq!(files.read("/f").await.unwrap(), upper.join(" "));
    db.finish().await;
}
