//! Conformance suite shared by every [`FileBackend`].
//!
//! Every backend must pass [`backend_suite`]. A failed rule panics with a
//! message naming it, so call the suite from a test. The suite only touches
//! paths under `/conformance/`; run it on a backend that holds nothing
//! there.

use super::{FileBackend, VfsError};

/// Check every [`FileBackend`] rule. Panics on the first rule that fails.
pub async fn backend_suite(backend: &dyn FileBackend) {
    missing_files(backend).await;
    round_trips(backend).await;
    paths_are_normalized(backend).await;
    bad_paths_are_refused(backend).await;
    listing(backend).await;
    edits(backend).await;
    deletes(backend).await;
}

async fn missing_files(backend: &dyn FileBackend) {
    let missing = "/conformance/missing/file.txt";
    let read = backend.read(missing).await;
    assert!(
        matches!(read, Err(VfsError::NotFound { .. })),
        "missing: read of a missing file is NotFound, got {read:?}"
    );
    assert!(
        !backend.exists(missing).await.expect("missing: exists"),
        "missing: exists is false"
    );
}

async fn round_trips(backend: &dyn FileBackend) {
    let samples = [
        ("/conformance/rt/plain.txt", "one\ntwo\n"),
        ("/conformance/rt/crlf.txt", "a\r\nb\r\n"),
        (
            "/conformance/rt/unicode.txt",
            "caf\u{e9} \u{65e5}\u{672c} \u{1f600}",
        ),
        ("/conformance/rt/empty.txt", ""),
    ];
    for (path, content) in samples {
        backend
            .write(path, content)
            .await
            .expect("round trip: write");
        let back = backend.read(path).await.expect("round trip: read");
        assert_eq!(back, content, "round trip: {path} reads back exactly");
        assert!(
            backend.exists(path).await.expect("round trip: exists"),
            "round trip: {path} exists after a write"
        );
    }
    let path = "/conformance/rt/plain.txt";
    backend.write(path, "replaced").await.expect("overwrite");
    let back = backend.read(path).await.expect("overwrite: read");
    assert_eq!(back, "replaced", "round trip: a write replaces the file");
}

async fn paths_are_normalized(backend: &dyn FileBackend) {
    backend
        .write("conformance//norm/./a.txt/", "x")
        .await
        .expect("normalized: write");
    let back = backend.read("/conformance/norm/a.txt").await;
    assert_eq!(
        back.expect("normalized: read"),
        "x",
        "normalized: a messy path names the canonical file"
    );
}

async fn bad_paths_are_refused(backend: &dyn FileBackend) {
    for bad in [
        "/conformance/../escape.txt",
        "/conformance/nul\0.txt",
        "/",
        "",
    ] {
        let write = backend.write(bad, "x").await;
        assert!(
            matches!(write, Err(VfsError::InvalidPath { .. })),
            "bad path: write to {bad:?} is InvalidPath, got {write:?}"
        );
        let read = backend.read(bad).await;
        assert!(
            matches!(read, Err(VfsError::InvalidPath { .. })),
            "bad path: read of {bad:?} is InvalidPath, got {read:?}"
        );
    }
    let ls = backend.ls("/conformance/..").await;
    assert!(
        matches!(ls, Err(VfsError::InvalidPath { .. })),
        "bad path: ls of a `..` prefix is InvalidPath, got {ls:?}"
    );
}

async fn listing(backend: &dyn FileBackend) {
    let files = [
        "/conformance/ls/b.txt",
        "/conformance/ls/a/x.txt",
        "/conformance/ls/a/y/z.txt",
        "/conformance/ls/ab",
        "/conformance/ls/pXq",
        "/conformance/ls/p_q",
        "/conformance/ls/r%s",
        "/conformance/ls/rzzs",
    ];
    for path in files {
        backend.write(path, path).await.expect("ls: write");
    }
    let all = backend.ls("/conformance/ls").await.expect("ls: list");
    let mut sorted = files.map(str::to_owned).to_vec();
    sorted.sort();
    assert_eq!(all, sorted, "ls: every file under the prefix, sorted");

    let a = backend.ls("conformance/ls/a/").await.expect("ls: list a");
    assert_eq!(
        a,
        ["/conformance/ls/a/x.txt", "/conformance/ls/a/y/z.txt"],
        "ls: a prefix matches whole segments, so /a does not list /ab"
    );
    let exact = backend.ls("/conformance/ls/ab").await.expect("ls: exact");
    assert_eq!(
        exact,
        ["/conformance/ls/ab"],
        "ls: a file path lists itself"
    );
    let underscore = backend.ls("/conformance/ls/p_q").await.expect("ls: _");
    assert_eq!(underscore, ["/conformance/ls/p_q"], "ls: `_` is literal");
    let percent = backend.ls("/conformance/ls/r%s").await.expect("ls: %");
    assert_eq!(percent, ["/conformance/ls/r%s"], "ls: `%` is literal");
    let none = backend
        .ls("/conformance/ls/nothing")
        .await
        .expect("ls: none");
    assert!(none.is_empty(), "ls: an unknown prefix lists nothing");

    for everything in ["", "/"] {
        let listed = backend.ls(everything).await.expect("ls: everything");
        assert!(
            files.iter().all(|path| listed.iter().any(|p| p == path)),
            "ls: {everything:?} lists every file"
        );
    }
}

async fn edits(backend: &dyn FileBackend) {
    let path = "/conformance/edit/a.txt";
    let missing = backend
        .edit("/conformance/edit/none", "a", "b", false)
        .await;
    assert!(
        matches!(missing, Err(VfsError::NotFound { .. })),
        "edit: a missing file is NotFound, got {missing:?}"
    );
    backend
        .write(path, "foo bar foo")
        .await
        .expect("edit: write");
    let absent = backend.edit(path, "baz", "x", false).await;
    assert!(
        matches!(absent, Err(VfsError::NoMatch { .. })),
        "edit: absent old text is NoMatch, got {absent:?}"
    );
    let twice = backend.edit(path, "foo", "x", false).await;
    assert!(
        matches!(twice, Err(VfsError::NotUnique { count: 2, .. })),
        "edit: two matches without replace_all is NotUnique, got {twice:?}"
    );
    let content = backend.read(path).await.expect("edit: read");
    assert_eq!(
        content, "foo bar foo",
        "edit: a refused edit changes nothing"
    );

    let one = backend
        .edit(path, "bar", "BAR", false)
        .await
        .expect("edit one");
    assert_eq!(one, 1, "edit: one replacement");
    let all = backend
        .edit(path, "foo", "F", true)
        .await
        .expect("edit all");
    assert_eq!(all, 2, "edit: replace_all counts every replacement");
    let content = backend.read(path).await.expect("edit: read back");
    assert_eq!(content, "F BAR F", "edit: the replacements landed");
}

async fn deletes(backend: &dyn FileBackend) {
    let path = "/conformance/delete/a.txt";
    backend.write(path, "x").await.expect("delete: write");
    backend.delete(path).await.expect("delete");
    assert!(
        !backend.exists(path).await.expect("delete: exists"),
        "delete: the file is gone"
    );
    backend
        .delete(path)
        .await
        .expect("delete: removing a missing file does nothing");
}
