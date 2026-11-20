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

/// The workspace path `path` resolves to.
fn named(local: &LocalFs, path: &str) -> String {
    local.locate(path).unwrap().workspace_path()
}

#[test]
fn relative_and_absolute_paths_inside_resolve_to_workspace_paths() {
    let (_base, local, _) = setup();
    assert_eq!(named(&local, "src/./a.txt"), "src/a.txt");
    let absolute = local.root().join("src/a.txt");
    assert_eq!(named(&local, absolute.to_str().unwrap()), "src/a.txt");
    assert_eq!(named(&local, "."), ".");
    assert_eq!(named(&local, ""), ".");
    assert_eq!(named(&local, "src/new/deeper.txt"), "src/new/deeper.txt");
}

#[test]
fn dot_dot_and_absolute_paths_outside_are_refused() {
    let (_base, local, outside) = setup();
    assert!(escapes(local.locate("../outside/secret.txt")));
    assert!(escapes(local.locate("src/../../outside/secret.txt")));
    assert!(escapes(
        local.locate(outside.join("secret.txt").to_str().unwrap())
    ));
    assert!(escapes(local.locate("/etc/passwd")));
    // `..` through a directory that does not exist cannot be resolved.
    assert!(escapes(local.locate("missing/../../outside/x")));
    // `..` that stays inside is fine.
    assert_eq!(named(&local, "src/../src/a.txt"), "src/a.txt");
}

#[test]
fn symlinks_are_resolved_before_the_check() {
    let (_base, local, outside) = setup();
    let root = local.root().to_path_buf();
    symlink(outside.join("secret.txt"), root.join("leak.txt")).unwrap();
    symlink(&outside, root.join("out")).unwrap();
    symlink(root.join("src/a.txt"), root.join("alias.txt")).unwrap();
    symlink(outside.join("not-there.txt"), root.join("dangling.txt")).unwrap();
    assert!(escapes(local.locate("leak.txt")));
    assert!(escapes(local.locate("out/secret.txt")));
    assert!(escapes(local.locate("out/new.txt")));
    assert!(escapes(local.locate("dangling.txt")));
    // A link that stays inside resolves to its target's name.
    assert_eq!(named(&local, "alias.txt"), "src/a.txt");
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
    assert_eq!(named(&local, through_link.to_str().unwrap()), "src/a.txt");
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
    // A new file gets the mode any newly created file gets under the
    // umask: neither the private temp-file mode nor an unmasked 0666.
    let reference = local.root().join("reference");
    fs::File::create(&reference).unwrap();
    let expected = fs::metadata(&reference).unwrap().permissions().mode() & 0o777;
    assert_eq!(fresh.permissions().mode() & 0o777, expected);
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
    let found = local.glob(&glob, &cancel).await.unwrap();
    let paths: Vec<_> = found.hits.iter().map(|hit| hit.path.as_str()).collect();
    assert_eq!(paths, ["src/a.txt"]);
}

#[test]
fn a_file_changed_while_reading_has_the_python_text() {
    let error = FsError::ChangedWhileReading {
        path: "a.txt".to_owned(),
    };
    assert_eq!(
        error.to_string(),
        "File changed while reading; Read this file again."
    );
}

#[tokio::test]
async fn an_absolute_symlink_target_must_lead_back_into_the_root() {
    let (_base, local, outside) = setup();
    let root = local.root().to_path_buf();
    symlink(root.join("src/a.txt"), root.join("inside.txt")).unwrap();
    symlink(root.join("src"), root.join("dir-link")).unwrap();
    symlink(outside.join("secret.txt"), root.join("outside.txt")).unwrap();
    // Spelled with `..`, the target still has to end up under the root.
    symlink(
        root.join("../outside/secret.txt"),
        root.join("climbing.txt"),
    )
    .unwrap();
    assert_eq!(
        local.read("inside.txt", 1024).await.unwrap().bytes,
        b"inside\n"
    );
    assert_eq!(
        local.read("dir-link/a.txt", 1024).await.unwrap().path,
        "src/a.txt"
    );
    assert!(escapes(local.read("outside.txt", 1024).await));
    assert!(escapes(local.read("climbing.txt", 1024).await));
    assert!(escapes(
        local.write("climbing.txt", b"x".to_vec(), None).await
    ));
    assert_eq!(
        fs::read_to_string(outside.join("secret.txt")).unwrap(),
        "secret\n"
    );
}

#[tokio::test]
async fn dot_dot_after_a_symlinked_directory_climbs_from_its_target() {
    let (_base, local, _) = setup();
    let root = local.root().to_path_buf();
    fs::create_dir_all(root.join("deep/er")).unwrap();
    fs::write(root.join("deep/top.txt"), "top\n").unwrap();
    fs::write(root.join("top.txt"), "top\n").unwrap();
    symlink("deep/er", root.join("link")).unwrap();
    // `link/..` is the parent of the link's target, as in a shell.
    assert_eq!(named(&local, "link/../top.txt"), "deep/top.txt");
    assert_eq!(named(&local, "link/../../top.txt"), "top.txt");
    assert!(escapes(local.locate("link/../../../top.txt")));
    assert!(matches!(
        local.read("link/..", 10).await,
        Err(FsError::NotAFile { .. })
    ));
}

#[tokio::test]
async fn a_symlink_loop_is_an_error_not_a_hang() {
    let (_base, local, _) = setup();
    let root = local.root().to_path_buf();
    symlink("two", root.join("one")).unwrap();
    symlink("one", root.join("two")).unwrap();
    assert!(matches!(
        local.read("one", 10).await,
        Err(FsError::Io { .. })
    ));
    assert!(matches!(
        local.write("one/x", b"x".to_vec(), None).await,
        Err(FsError::Io { .. })
    ));
}

#[tokio::test]
async fn a_file_is_never_created_through_a_dangling_symlink() {
    let (_base, local, outside) = setup();
    let root = local.root().to_path_buf();
    symlink(outside.join("created.txt"), root.join("out.txt")).unwrap();
    symlink("created-inside.txt", root.join("in.txt")).unwrap();
    symlink(outside.join("newdir"), root.join("dir")).unwrap();
    for path in ["out.txt", "in.txt", "dir/x.txt"] {
        assert!(escapes(local.write(path, b"x".to_vec(), None).await));
    }
    assert!(!outside.join("created.txt").exists());
    assert!(!outside.join("newdir").exists());
    assert!(!root.join("created-inside.txt").exists());
}

#[tokio::test]
async fn a_missing_directory_may_not_climb_or_sit_below_a_file() {
    let (_base, local, outside) = setup();
    assert!(escapes(
        local
            .write("new/../../outside/x.txt", b"x".to_vec(), None)
            .await
    ));
    assert!(!outside.join("x.txt").exists());
    assert!(!local.root().join("new").exists());
    assert!(matches!(
        local.write("src/a.txt/x/y.txt", b"x".to_vec(), None).await,
        Err(FsError::NotADirectory { .. })
    ));
}

#[tokio::test]
async fn a_root_that_is_a_symlink_stays_on_the_directory_it_opened() {
    let (base, _, outside) = setup();
    let link = base.path().join("link-to-root");
    symlink(base.path().join("root"), &link).unwrap();
    let local = LocalFs::new(&link).unwrap();
    // The link is repointed after the root was opened.
    fs::remove_file(&link).unwrap();
    symlink(&outside, &link).unwrap();
    assert_eq!(
        local.read("src/a.txt", 1024).await.unwrap().bytes,
        b"inside\n"
    );
    assert!(matches!(
        local.read("secret.txt", 1024).await,
        Err(FsError::NotFound { .. })
    ));
    // Spelled through the link, an absolute path now names the outside.
    let through = link.join("secret.txt");
    assert!(escapes(local.read(through.to_str().unwrap(), 1024).await));
}

#[tokio::test]
async fn a_write_through_a_hard_link_replaces_the_name_not_the_shared_file() {
    // Hard links are not confined: a name inside the root for a file outside
    // reads as that file. A write only replaces the name, so the file
    // outside keeps its content.
    let (_base, local, outside) = setup();
    fs::hard_link(outside.join("secret.txt"), local.root().join("hard.txt")).unwrap();
    local
        .write("hard.txt", b"replaced".to_vec(), None)
        .await
        .unwrap();
    assert_eq!(
        fs::read_to_string(outside.join("secret.txt")).unwrap(),
        "secret\n"
    );
    assert_eq!(
        local.read("hard.txt", 1024).await.unwrap().bytes,
        b"replaced"
    );
}

#[tokio::test]
async fn a_deep_path_is_walked_without_a_path_length_limit_and_a_too_deep_one_is_refused() {
    let (_base, local, _) = setup();
    // 100 levels climbed back to the root, then the file there.
    let down = "d/".repeat(100);
    let up = "../".repeat(100);
    local
        .write(&format!("{down}f.txt"), b"deep".to_vec(), None)
        .await
        .unwrap();
    let there = format!("{down}f.txt");
    assert_eq!(local.read(&there, 1024).await.unwrap().bytes, b"deep");
    assert_eq!(
        local
            .stat(&format!("{down}{up}src/a.txt"))
            .await
            .unwrap()
            .path,
        "src/a.txt"
    );
    // Longer than PATH_MAX, but only 4 levels: the walk goes name by name.
    let dots = "./".repeat(5_000);
    let long = format!("{dots}src/a.txt");
    assert_eq!(local.read(&long, 1024).await.unwrap().bytes, b"inside\n");
    // Too many open levels would hold too many descriptors.
    let deeper = format!("{}x.txt", "d/".repeat(300));
    assert!(matches!(
        local.write(&deeper, b"x".to_vec(), None).await,
        Err(FsError::Io { .. })
    ));
    assert!(matches!(
        local.read(&deeper, 10).await,
        Err(FsError::Io { .. })
    ));
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn names_that_are_not_utf8_are_walked_and_shown_lossily() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    let (_base, local, _) = setup();
    let odd = OsStr::from_bytes(b"d\xff");
    fs::create_dir_all(local.root().join(odd)).unwrap();
    fs::write(local.root().join(odd).join("a.txt"), "odd\n").unwrap();
    symlink(odd, local.root().join("link")).unwrap();
    let file = local.read("link/a.txt", 1024).await.unwrap();
    assert_eq!(file.path, "\u{fffd}/a.txt");
    assert_eq!(file.bytes, b"odd\n");
    // A search finds it by name, too.
    let cancel = CancellationToken::new();
    let glob = GlobQuery {
        pattern: "d*/*.txt".to_owned(),
        path: None,
    };
    let hits = local.glob(&glob, &cancel).await.unwrap().hits;
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].path, "\u{fffd}/a.txt");
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn a_fifo_is_not_a_file_and_does_not_block() {
    let (_base, local, _) = setup();
    rustix::fs::mknodat(
        rustix::fs::CWD,
        local.root().join("pipe"),
        rustix::fs::FileType::Fifo,
        rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        0,
    )
    .unwrap();
    assert!(matches!(
        local.read("pipe", 10).await,
        Err(FsError::NotAFile { .. })
    ));
    assert!(matches!(
        local.stat("pipe").await,
        Err(FsError::NotAFile { .. })
    ));
    assert!(matches!(
        local.write("pipe", b"x".to_vec(), None).await,
        Err(FsError::NotAFile { .. })
    ));
}

#[tokio::test]
async fn a_write_that_is_refused_leaves_no_temporary_file() {
    let (_base, local, _) = setup();
    fs::create_dir_all(local.root().join("target/inner")).unwrap();
    let before = fs::read_dir(local.root()).unwrap().count();
    assert!(matches!(
        local.write("target", b"x".to_vec(), None).await,
        Err(FsError::NotAFile { .. })
    ));
    assert_eq!(fs::read_dir(local.root()).unwrap().count(), before);
}
