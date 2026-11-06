use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::PathBuf;

use tempfile::TempDir;
use tokio_util::sync::CancellationToken;

use super::*;

/// A temp dir holding `root/` (the workspace) and `outside/` with a secret.
fn setup() -> (TempDir, LocalFs, PathBuf) {
    let base = tempfile::tempdir().unwrap();
    let root = base.path().join("root");
    let outside = base.path().join("outside");
    fs::create_dir_all(root.join("src")).unwrap();
    fs::create_dir_all(&outside).unwrap();
    fs::write(root.join("src/a.txt"), "inside\n").unwrap();
    fs::write(outside.join("secret.txt"), "secret\n").unwrap();
    let local = LocalFs::new(&root).unwrap();
    (base, local, outside)
}

fn escapes(result: Result<impl std::fmt::Debug, FsError>) -> bool {
    matches!(result, Err(FsError::Escape { .. }))
}

#[test]
fn relative_and_absolute_paths_inside_resolve_to_workspace_paths() {
    let (_base, local, _) = setup();
    let real = local.resolve("src/./a.txt").unwrap();
    assert_eq!(local.workspace_path(&real), "src/a.txt");
    let absolute = local.root().join("src/a.txt");
    let real = local.resolve(absolute.to_str().unwrap()).unwrap();
    assert_eq!(local.workspace_path(&real), "src/a.txt");
    assert_eq!(local.workspace_path(&local.resolve(".").unwrap()), ".");
    let new = local.resolve("src/new/deeper.txt").unwrap();
    assert_eq!(local.workspace_path(&new), "src/new/deeper.txt");
}

#[test]
fn dot_dot_and_absolute_paths_outside_are_refused() {
    let (_base, local, outside) = setup();
    assert!(escapes(local.resolve("../outside/secret.txt")));
    assert!(escapes(local.resolve("src/../../outside/secret.txt")));
    assert!(escapes(
        local.resolve(outside.join("secret.txt").to_str().unwrap())
    ));
    assert!(escapes(local.resolve("/etc/passwd")));
    // `..` through a directory that does not exist cannot be resolved.
    assert!(escapes(local.resolve("missing/../../outside/x")));
    // `..` that stays inside is fine.
    let real = local.resolve("src/../src/a.txt").unwrap();
    assert_eq!(local.workspace_path(&real), "src/a.txt");
}

#[test]
fn symlinks_are_resolved_before_the_check() {
    let (_base, local, outside) = setup();
    let root = local.root().to_path_buf();
    symlink(outside.join("secret.txt"), root.join("leak.txt")).unwrap();
    symlink(&outside, root.join("out")).unwrap();
    symlink(root.join("src/a.txt"), root.join("alias.txt")).unwrap();
    symlink(outside.join("not-there.txt"), root.join("dangling.txt")).unwrap();
    assert!(escapes(local.resolve("leak.txt")));
    assert!(escapes(local.resolve("out/secret.txt")));
    assert!(escapes(local.resolve("out/new.txt")));
    assert!(escapes(local.resolve("dangling.txt")));
    // A link that stays inside resolves to its target's name.
    let real = local.resolve("alias.txt").unwrap();
    assert_eq!(local.workspace_path(&real), "src/a.txt");
}

#[test]
fn a_root_reached_through_a_symlink_is_canonicalized() {
    let (base, _, _) = setup();
    let link = base.path().join("link-to-root");
    symlink(base.path().join("root"), &link).unwrap();
    let local = LocalFs::new(&link).unwrap();
    assert_eq!(
        local.root(),
        fs::canonicalize(base.path().join("root")).unwrap()
    );
    let through_link = link.join("src/a.txt");
    let real = local.resolve(through_link.to_str().unwrap()).unwrap();
    assert_eq!(local.workspace_path(&real), "src/a.txt");
}

#[test]
fn a_root_must_be_an_existing_directory() {
    let (base, local, _) = setup();
    assert!(matches!(
        LocalFs::new(base.path().join("nope")),
        Err(FsError::NotFound { .. })
    ));
    assert!(matches!(
        LocalFs::new(local.root().join("src/a.txt")),
        Err(FsError::NotADirectory { .. })
    ));
}

#[tokio::test]
async fn file_operations_refuse_escapes() {
    let (_base, local, outside) = setup();
    symlink(&outside, local.root().join("out")).unwrap();
    assert!(escapes(local.stat("out/secret.txt").await));
    assert!(escapes(local.read("out/secret.txt", 1024).await));
    assert!(escapes(local.write("out/x.txt", b"x".to_vec(), None).await));
    assert!(escapes(
        local.write("../outside/x.txt", b"x".to_vec(), None).await
    ));
    assert!(!outside.join("x.txt").exists());
    assert_eq!(
        fs::read_to_string(outside.join("secret.txt")).unwrap(),
        "secret\n"
    );
}

#[tokio::test]
async fn stat_read_and_write_agree_on_stamps() {
    let (_base, local, _) = setup();
    let info = local.stat("src/a.txt").await.unwrap();
    let content = local.read("src/a.txt", 1024).await.unwrap();
    assert_eq!(info.stamp, Some(content.stamp));
    assert_eq!(content.bytes, b"inside\n");
    assert_eq!(local.stat("src/missing.txt").await.unwrap().stamp, None);
    let stamp = local
        .write("new/dir/b.txt", b"hello".to_vec(), None)
        .await
        .unwrap();
    assert_eq!(
        local.stat("new/dir/b.txt").await.unwrap().stamp,
        Some(stamp)
    );
    assert_eq!(stamp.len, 5);
}

#[tokio::test]
async fn read_refuses_large_missing_and_non_files() {
    let (_base, local, _) = setup();
    assert!(matches!(
        local.read("src/a.txt", 3).await,
        Err(FsError::TooLarge { limit: 3, .. })
    ));
    assert!(matches!(
        local.read("src/nope.txt", 10).await,
        Err(FsError::NotFound { .. })
    ));
    assert!(matches!(
        local.read("src", 10).await,
        Err(FsError::NotAFile { .. })
    ));
    assert!(matches!(
        local.stat("src").await,
        Err(FsError::NotAFile { .. })
    ));
    assert!(matches!(
        local.write("src", b"x".to_vec(), None).await,
        Err(FsError::NotAFile { .. })
    ));
    assert!(matches!(
        local.read("src/a.txt/x", 10).await,
        Err(FsError::NotADirectory { .. })
    ));
}

#[tokio::test]
async fn a_guarded_write_fails_when_the_file_changed() {
    let (_base, local, _) = setup();
    let before = local.stat("src/a.txt").await.unwrap().stamp.unwrap();
    fs::write(local.root().join("src/a.txt"), "changed by someone else\n").unwrap();
    let result = local
        .write("src/a.txt", b"mine".to_vec(), Some(before))
        .await;
    assert!(matches!(result, Err(FsError::Changed { .. })));
    let now = fs::read_to_string(local.root().join("src/a.txt")).unwrap();
    assert_eq!(now, "changed by someone else\n");
    // A guard on a file that does not exist fails as well.
    let result = local
        .write("src/gone.txt", b"x".to_vec(), Some(before))
        .await;
    assert!(matches!(result, Err(FsError::Changed { .. })));
}

#[tokio::test]
async fn writes_keep_an_existing_mode_and_leave_no_temp_files() {
    let (_base, local, _) = setup();
    let path = local.root().join("src/a.txt");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o751)).unwrap();
    local
        .write("src/a.txt", b"new".to_vec(), None)
        .await
        .unwrap();
    let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o751);
    local
        .write("src/fresh.txt", b"x".to_vec(), None)
        .await
        .unwrap();
    let fresh = fs::metadata(local.root().join("src/fresh.txt")).unwrap();
    // A new file follows the umask instead of a private temp-file mode.
    assert_ne!(fresh.permissions().mode() & 0o777, 0o600);
    let names: Vec<_> = fs::read_dir(local.root().join("src"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    assert!(
        names.iter().all(|name| !name.ends_with(".tmp")),
        "{names:?}"
    );
}

#[tokio::test]
async fn searches_stop_when_cancelled() {
    let (_base, local, _) = setup();
    let cancel = CancellationToken::new();
    cancel.cancel();
    let query = GrepQuery {
        pattern: "inside".to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 10,
    };
    assert!(matches!(
        local.grep(&query, &cancel).await,
        Err(FsError::Cancelled)
    ));
    let glob = GlobQuery {
        pattern: "*.txt".to_owned(),
        path: None,
    };
    assert!(matches!(
        local.glob(&glob, &cancel).await,
        Err(FsError::Cancelled)
    ));
}

#[tokio::test]
async fn searches_never_follow_symlinks_out_of_the_root() {
    let (_base, local, outside) = setup();
    symlink(&outside, local.root().join("out")).unwrap();
    symlink(outside.join("secret.txt"), local.root().join("leak.txt")).unwrap();
    let cancel = CancellationToken::new();
    let query = GrepQuery {
        pattern: "secret".to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 10,
    };
    assert!(local.grep(&query, &cancel).await.unwrap().files.is_empty());
    let mut into_link = query.clone();
    into_link.path = Some("out".to_owned());
    assert!(escapes(local.grep(&into_link, &cancel).await));
    let glob = GlobQuery {
        pattern: "**/*.txt".to_owned(),
        path: None,
    };
    let hits = local.glob(&glob, &cancel).await.unwrap();
    let paths: Vec<_> = hits.iter().map(|hit| hit.path.as_str()).collect();
    assert_eq!(paths, ["src/a.txt"]);
}
