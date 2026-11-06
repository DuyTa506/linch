//! Reading lines without letting one bad line end the connection.
//!
//! [`LineCodec`] splits input at `\n` and reports a line over the limit as
//! an item, with its first bytes kept, instead of as an error: an error
//! makes `FramedRead` yield `None` once, which would read as end of input.
//! UTF-8 is checked by the caller, so a line that is not UTF-8 is one bad
//! message, not a read failure. [`Head`] reads the `id` and kind of an
//! oversized message from those first bytes.

use bytes::{Buf, BytesMut};
use serde::Deserializer as _;
use serde::de::{IgnoredAny, MapAccess, Visitor};
use tokio_util::codec::Decoder;

use crate::message::Id;

/// Bytes kept from the start of an oversized message.
pub(super) const HEAD_BYTES: usize = 64 * 1024;

/// One frame of input.
pub(super) enum Frame {
    /// A complete line, without its `\n` (and `\r`).
    Line(Vec<u8>),
    /// A line over the limit, already discarded; its first bytes.
    TooLong(Vec<u8>),
}

/// Newline-delimited frames of at most `max` bytes.
pub(super) struct LineCodec {
    max: usize,
    /// Where to resume looking for `\n` in the buffer.
    next_index: usize,
    /// The head of the oversized line being discarded.
    discarding: Option<Vec<u8>>,
}

impl LineCodec {
    pub(super) fn new(max: usize) -> Self {
        Self {
            max,
            next_index: 0,
            discarding: None,
        }
    }
}

impl Decoder for LineCodec {
    type Item = Frame;
    type Error = std::io::Error;

    fn decode(&mut self, buffer: &mut BytesMut) -> Result<Option<Frame>, Self::Error> {
        loop {
            let newline = (buffer[self.next_index..].iter())
                .position(|byte| *byte == b'\n')
                .map(|offset| self.next_index + offset);
            match (&mut self.discarding, newline) {
                (Some(_), Some(end)) => {
                    buffer.advance(end + 1);
                    self.next_index = 0;
                    return Ok(self.discarding.take().map(Frame::TooLong));
                }
                (Some(_), None) => {
                    buffer.clear();
                    self.next_index = 0;
                    return Ok(None);
                }
                (None, Some(end)) if end <= self.max => {
                    let mut line = buffer.split_to(end + 1).to_vec();
                    self.next_index = 0;
                    line.pop();
                    if line.last() == Some(&b'\r') {
                        line.pop();
                    }
                    return Ok(Some(Frame::Line(line)));
                }
                (None, newline) if newline.is_some() || buffer.len() > self.max => {
                    let head = buffer[..buffer.len().min(HEAD_BYTES)].to_vec();
                    self.discarding = Some(head);
                    self.next_index = 0;
                }
                (None, _) => {
                    self.next_index = buffer.len();
                    return Ok(None);
                }
            }
        }
    }

    fn decode_eof(&mut self, buffer: &mut BytesMut) -> Result<Option<Frame>, Self::Error> {
        if let Some(frame) = self.decode(buffer)? {
            return Ok(Some(frame));
        }
        if let Some(head) = self.discarding.take() {
            return Ok(Some(Frame::TooLong(head)));
        }
        self.next_index = 0;
        if buffer.is_empty() {
            return Ok(None);
        }
        // A last line without `\n`.
        Ok(Some(Frame::Line(buffer.split().to_vec())))
    }
}

/// What the first bytes of an oversized message say about it.
#[derive(Debug, Default, PartialEq)]
pub(super) struct Head {
    /// Its `id`, when it comes before the cut.
    pub(super) id: Option<Id>,
    /// Whether a `method` member came before the cut: a request.
    pub(super) method: bool,
    /// Whether a `result` or `error` member came before the cut: an answer.
    pub(super) answer: bool,
}

impl Head {
    /// Read the top-level members of `bytes` up to where they are cut.
    pub(super) fn read(bytes: &[u8]) -> Self {
        let mut head = Self::default();
        let mut reader = serde_json::Deserializer::from_slice(bytes);
        // The cut always ends in an error; what was read before it counts.
        let _ = reader.deserialize_map(Members(&mut head));
        head
    }
}

/// Records each member of interest as it is read.
struct Members<'a>(&'a mut Head);

impl<'de> Visitor<'de> for Members<'_> {
    type Value = ();

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON-RPC message object")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "id" => self.0.id = Some(map.next_value()?),
                "method" => {
                    self.0.method = true;
                    map.next_value::<IgnoredAny>()?;
                }
                "result" | "error" => {
                    self.0.answer = true;
                    map.next_value::<IgnoredAny>()?;
                }
                _ => {
                    map.next_value::<IgnoredAny>()?;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frames(input: &[u8], max: usize) -> Vec<Frame> {
        let mut codec = LineCodec::new(max);
        let mut buffer = BytesMut::new();
        let mut frames = Vec::new();
        // Feed one byte at a time, so every split point is exercised.
        for byte in input {
            buffer.extend_from_slice(&[*byte]);
            while let Some(frame) = codec.decode(&mut buffer).unwrap() {
                frames.push(frame);
            }
        }
        while let Some(frame) = codec.decode_eof(&mut buffer).unwrap() {
            frames.push(frame);
        }
        frames
    }

    fn describe(frames: &[Frame]) -> Vec<String> {
        (frames.iter())
            .map(|frame| match frame {
                Frame::Line(line) => format!("line {}", String::from_utf8_lossy(line)),
                Frame::TooLong(head) => format!("long {}", String::from_utf8_lossy(head)),
            })
            .collect()
    }

    #[test]
    fn lines_split_at_newlines_and_a_long_one_is_reported_once() {
        let input = b"ab\r\ncd\n0123456789\nef\n\xff\ngh";
        let got = describe(&frames(input, 4));
        assert_eq!(
            got,
            [
                "line ab",
                "line cd",
                "long 01234",
                "line ef",
                "line \u{fffd}",
                "line gh"
            ]
        );
        let got = describe(&frames(b"ok\n0123456789", 4));
        assert_eq!(got, ["line ok", "long 01234"]);
        assert_eq!(describe(&frames(b"1234\n", 4)), ["line 1234"]);
    }

    #[test]
    fn the_head_names_the_id_and_kind_before_the_cut() {
        let answer = Head::read(br#"{"jsonrpc":"2.0","id":"srv-3","result":{"value":"aaaa"#);
        assert_eq!(answer.id, Some(Id::String("srv-3".into())));
        assert!(answer.answer && !answer.method);
        let request = Head::read(br#"{"id": 9, "method": "agent.open", "params": {"x": "y"#);
        assert_eq!(request.id, Some(Id::Number(9.into())));
        assert!(request.method && !request.answer);
        let late = Head::read(br#"{"result": {"value": "aaaaaaaa"#);
        assert_eq!(late.id, None);
        assert!(late.answer);
        assert_eq!(Head::read(b"\xff\xfe"), Head::default());
    }
}
