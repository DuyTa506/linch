//! Clipping: what a run does when a prompt is refused as longer than the
//! model's context window and a plain summary cannot help.
//!
//! Text is cut to a token budget the way a person skims it: the head and the
//! tail stay, a marker (`[... N bytes omitted ...]`) says what went, and no
//! message is dropped, so every `tool_use_id` stays paired. The cut is one
//! cap across all the texts in scope, the largest that brings the estimate
//! under the budget, so a view of many medium results converges as well as
//! one of a single giant.
//!
//! - A forced summary request reads the folded part clipped (tool results
//!   first, then other text), a smaller budget at each refusal of it.
//! - The last turn clips in the view itself, tool results only, as a logged
//!   projection: the session's log keeps every byte.
//!
//! The estimate undercounts a provider's prompt by up to 2x, which is why the
//! budgets halve at each refusal instead of being computed once.

use std::sync::Arc;

use crate::message::{ContentBlock, Message, ToolResultPart};

use super::estimate_tokens;

/// Refusals as too long a summary survives: its request goes again on half
/// the folded part until the fourth refusal of one that reads it.
const MAX_SUMMARY_TRIES: u32 = 4;

/// How many of a summary's requests the provider may refuse as too long
/// before it is out of requests: a forced summary reads the folded part from
/// its first request, a turn-start one reads the whole view first, and that
/// refusal does not count against the folded part's.
pub(crate) fn summary_requests(forced: bool) -> u32 {
    MAX_SUMMARY_TRIES + u32::from(!forced)
}

/// Forced steps of one turn that a clip of the last turn may follow, each
/// against half the budget the one before it used.
pub(crate) const MAX_CLIP_ROUNDS: u32 = 3;

/// Strategy name of a clip in the `compaction` event.
pub(crate) const CLIP_STRATEGY: &str = "clip";

/// Reason stored with a clip's projection entry.
pub(crate) const CLIP_REASON: &str = "clip";

/// Starts the `rejected` text of a forced summary the provider refused as
/// longer than its window, which a smaller one answers.
pub(crate) const SIZE_REFUSED: &str = "context_length";

/// Most bytes the marker of a cut takes.
const MARKER_BOUND: usize = 52;

/// Least bytes of head and tail a cut text keeps together.
const MIN_KEEP: usize = 256;

/// What a clip made of some messages.
#[derive(Debug)]
pub(crate) struct Clipped {
    /// The messages with their texts cut; those that did not change are the
    /// original `Arc`s.
    pub(crate) messages: Vec<Arc<Message>>,
    /// Bytes the cuts took out, their markers counted.
    pub(crate) bytes: u64,
}

/// Which texts a clip may cut.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Scope {
    /// Tool results.
    Results,
    /// Tool results first, then the text of any message.
    Texts,
}

/// Tokens (estimated) of the folded part a forced summary request may carry
/// after `refusals` refusals of it as too long (0 for the first request): the
/// window less the summary's own output (at most half the window, so a large
/// output limit on a small window still leaves room) at first, then half of
/// what the part weighs, again and again, since the estimate says nothing of
/// how far under the window the request really is. `folded` is the part's
/// estimate.
pub(crate) fn folded_budget(
    window: u64,
    max_output_tokens: u32,
    refusals: u32,
    folded: u64,
) -> u64 {
    let room = window - u64::from(max_output_tokens).min(window / 2);
    match refusals {
        0 => room.max(1),
        refusals => (room.min(folded) >> refusals.min(63)).max(1),
    }
}

/// Tokens (estimated) the last turn may weigh after the `round`-th
/// refusal of a turn's primary call (0 for the first): half the window, then
/// half of that, and so on. `None` once the rounds are spent.
pub(crate) fn last_turn_budget(window: u64, round: u32) -> Option<u64> {
    (round <= MAX_CLIP_ROUNDS).then(|| ((window / 2) >> round).max(1))
}

/// `messages` with text cut until their estimate is at most `budget` tokens,
/// or as far as `scope` allows. `None` when they already fit, or nothing in
/// scope is long enough to cut.
pub(crate) fn clip(messages: &[Arc<Message>], budget: u64, scope: Scope) -> Option<Clipped> {
    let tokens = estimate_tokens(messages);
    if tokens <= budget {
        return None;
    }
    // The estimate is characters / 4, rounded down: this much more than the
    // budget's characters brings it under, whatever the rounding was.
    let mut excess = usize::try_from(tokens - budget)
        .unwrap_or(usize::MAX)
        .saturating_mul(4);
    let mut owned: Vec<Message> = messages.iter().map(|m| Message::clone(m)).collect();
    let mut touched = vec![false; owned.len()];
    let mut cut = 0;
    for results in [true, false] {
        if excess == 0 || (!results && scope == Scope::Results) {
            continue;
        }
        let removed = shrink(&mut owned, &mut touched, results, excess);
        excess = excess.saturating_sub(removed);
        cut += removed;
    }
    if cut == 0 {
        return None;
    }
    let messages = (owned.into_iter().zip(messages).zip(touched))
        .map(|((new, old), touched)| if touched { Arc::new(new) } else { old.clone() })
        .collect();
    Some(Clipped {
        messages,
        bytes: cut as u64,
    })
}

/// `view` with the tool results from index `start` on cut until that part
/// weighs at most `budget` tokens. `None` when it already does, or no result
/// is long enough to cut.
pub(crate) fn clip_from(view: &[Arc<Message>], start: usize, budget: u64) -> Option<Clipped> {
    let kept = view.get(start..)?;
    let mut clipped = clip(kept, budget, Scope::Results)?;
    let mut messages = view[..start].to_vec();
    messages.append(&mut clipped.messages);
    clipped.messages = messages;
    Some(clipped)
}

/// Visit each text of `messages` in scope: the parts of tool results when
/// `results`, else the text blocks.
fn for_each_text(
    messages: &mut [Message],
    results: bool,
    mut visit: impl FnMut(usize, &mut String),
) {
    for (at, message) in messages.iter_mut().enumerate() {
        for block in &mut message.content {
            match block {
                ContentBlock::ToolResult(result) if results => {
                    for part in &mut result.content {
                        if let ToolResultPart::Text(text) = part {
                            visit(at, &mut text.text);
                        }
                    }
                }
                ContentBlock::Text(text) if !results => visit(at, &mut text.text),
                _ => {}
            }
        }
    }
}

/// Cut the texts in scope by one cap, the largest that removes `excess`
/// bytes (or the smallest cap, when none does). Returns the bytes removed.
fn shrink(owned: &mut [Message], touched: &mut [bool], results: bool, excess: usize) -> usize {
    let mut lens = Vec::new();
    for_each_text(owned, results, |_, text| lens.push(text.len()));
    let removed = |cap: usize| -> usize {
        (lens.iter())
            .map(|len| len.saturating_sub(cap + MARKER_BOUND))
            .sum()
    };
    let (mut low, mut high) = (MIN_KEEP, lens.iter().copied().max().unwrap_or(0));
    if high <= low || removed(low) < excess {
        // Nothing to find, or all the cuts together fall short: cut to the
        // floor.
        high = low;
    }
    // The largest cap whose cuts remove `excess`.
    while low < high {
        let mid = low + (high - low).div_ceil(2);
        if removed(mid) >= excess {
            low = mid;
        } else {
            high = mid - 1;
        }
    }
    let mut total = 0;
    for_each_text(owned, results, |at, text| {
        let gone = cut(text, low);
        touched[at] |= gone > 0;
        total += gone;
    });
    total
}

/// Cut `text` to `cap` bytes of head and tail around a marker, unless that
/// would not make it shorter. Returns the bytes it took out, the marker
/// counted.
fn cut(text: &mut String, cap: usize) -> usize {
    let len = text.len();
    if len <= cap + MARKER_BOUND {
        return 0;
    }
    let mut head = cap / 2;
    while !text.is_char_boundary(head) {
        head -= 1;
    }
    let mut tail = len - (cap - cap / 2);
    while !text.is_char_boundary(tail) {
        tail += 1;
    }
    let omitted = tail - head;
    let clipped = format!(
        "{}\n[... {omitted} bytes omitted ...]\n{}",
        &text[..head],
        &text[tail..]
    );
    let gone = len - clipped.len();
    *text = clipped;
    gone
}

#[cfg(test)]
mod tests;
