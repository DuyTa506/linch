//! Bounded capture of one output stream: the first [`HEAD`] bytes and the
//! last [`TAIL`] bytes, with a count of what was dropped between them.

/// Bytes kept from the start of a stream.
pub(super) const HEAD: usize = 4_000;

/// Bytes kept from the end of a stream.
pub(super) const TAIL: usize = 26_000;

/// One stream's retained bytes.
#[derive(Debug, Default)]
pub(super) struct Capture {
    head: Vec<u8>,
    /// Holds up to twice [`TAIL`] so trimming is amortized.
    tail: Vec<u8>,
    total: u64,
}

impl Capture {
    /// Append a chunk read from the stream.
    pub(super) fn push(&mut self, mut chunk: &[u8]) {
        self.total += chunk.len() as u64;
        if self.head.len() < HEAD {
            let take = (HEAD - self.head.len()).min(chunk.len());
            self.head.extend_from_slice(&chunk[..take]);
            chunk = &chunk[take..];
        }
        self.tail.extend_from_slice(chunk);
        if self.tail.len() > 2 * TAIL {
            self.tail.drain(..self.tail.len() - TAIL);
        }
    }

    /// The retained text, decoded lossily, without one final newline, and
    /// whether bytes were dropped.
    pub(super) fn render(&self) -> (String, bool) {
        let tail = &self.tail[self.tail.len().saturating_sub(TAIL)..];
        let omitted = self.total - (self.head.len() + tail.len()) as u64;
        let mut text = String::from_utf8_lossy(&self.head).into_owned();
        if omitted > 0 {
            text.push_str(&format!("\n… ({omitted} bytes omitted)\n"));
        }
        text.push_str(&String::from_utf8_lossy(tail));
        if text.ends_with('\n') {
            text.pop();
        }
        (text, omitted > 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_streams_are_kept_whole() {
        let mut capture = Capture::default();
        capture.push(b"hello ");
        capture.push(b"world\n");
        assert_eq!(capture.render(), ("hello world".to_owned(), false));
    }

    #[test]
    fn long_streams_keep_head_and_tail_across_chunk_sizes() {
        let data: Vec<u8> = (0..200_000u32).map(|i| b'a' + (i % 26) as u8).collect();
        for chunk in [1, 7, 4_096, 65_536, 200_000] {
            let mut capture = Capture::default();
            data.chunks(chunk).for_each(|part| capture.push(part));
            let (text, truncated) = capture.render();
            assert!(truncated);
            let marker = format!("\n… ({} bytes omitted)\n", 200_000 - HEAD - TAIL);
            let (head, tail) = text.split_once(&marker).unwrap();
            assert_eq!(head.as_bytes(), &data[..HEAD]);
            assert_eq!(tail.as_bytes(), &data[data.len() - TAIL..]);
        }
    }

    #[test]
    fn exactly_head_plus_tail_drops_nothing() {
        let mut capture = Capture::default();
        capture.push(&vec![b'x'; HEAD + TAIL]);
        let (text, truncated) = capture.render();
        assert!(!truncated);
        assert_eq!(text.len(), HEAD + TAIL);
    }
}
