//! A path outside the root tells nothing about the host.

mod common;

use std::fs;

use common::Workspace;
use linch_tools::ExecutionFs;
use linch_tools::fs::FsError;

/// An absolute path outside the root is refused the same way whether or not
/// something exists there.
#[tokio::test]
async fn an_outside_path_is_refused_the_same_whether_it_exists_or_not() {
    let ws = Workspace::new();
    fs::write(ws.outside().join("id_rsa"), "HOST SECRET").unwrap();
    let outside = ws.outside().display().to_string();
    for path in [
        format!("{outside}/id_rsa/x"),
        format!("{outside}/id_dsa/x"),
        format!("{outside}/id_rsa"),
        format!("{outside}/id_dsa"),
        "/etc/passwd/x".to_owned(),
        "/etc/no-such-file-here/x".to_owned(),
    ] {
        let read = ws.fs.read(&path, 1024).await.unwrap_err();
        assert!(matches!(read, FsError::Escape { .. }), "{path}: {read:?}");
        let write = ws.fs.write(&path, b"x".to_vec(), None).await.unwrap_err();
        assert!(matches!(write, FsError::Escape { .. }), "{path}: {write:?}");
        let stat = ws.fs.stat(&path).await.unwrap_err();
        assert!(matches!(stat, FsError::Escape { .. }), "{path}: {stat:?}");
    }
}
