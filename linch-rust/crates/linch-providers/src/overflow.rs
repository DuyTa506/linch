//! How each provider says a prompt does not fit the model's context
//! window, so its refusal maps to [`ProviderError::ContextLength`].
//!
//! Every adapter declares its own [`Overflow`] from its vendor's error
//! shape; none inherits another's. Vendors document the status and error
//! body, rarely the wording, so a declaration prefers an error `code` or
//! `type` that means only this, and falls back to the message phrases the
//! vendor's server or API is known to send (see each declaration).

use linch_core::ProviderError;

/// One provider's context-length refusal.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Overflow {
    /// HTTP statuses the refusal comes with. An error with another status
    /// is never a refusal; one with no status (inside a stream) is judged
    /// by its labels and message alone.
    pub statuses: &'static [u16],
    /// Error `code` or `type` values that alone mean it.
    pub labels: &'static [&'static str],
    /// Lowercase phrases of the error message that mean it.
    pub phrases: &'static [&'static str],
}

impl Overflow {
    /// Whether an error with `status`, `labels` (its `code` and `type`, as
    /// given) and `message` is this provider refusing a prompt as too long.
    pub(crate) fn matches(&self, status: Option<u16>, labels: &[&str], message: &str) -> bool {
        if status.is_some_and(|status| !self.statuses.contains(&status)) {
            return false;
        }
        if labels.iter().any(|label| self.labels.contains(label)) {
            return true;
        }
        let message = message.to_lowercase();
        self.phrases.iter().any(|phrase| message.contains(phrase))
    }

    /// `error` as a [`ProviderError::ContextLength`] when it is a status
    /// error that [`Self::matches`] with `labels`; otherwise unchanged.
    pub(crate) fn classify(&self, error: ProviderError, labels: &[&str]) -> ProviderError {
        match error {
            ProviderError::Status {
                status, message, ..
            } if self.matches(Some(status), labels, &message) => ProviderError::ContextLength {
                status: Some(status),
                message,
            },
            other => other,
        }
    }
}

/// Anthropic Messages: a 400 `invalid_request_error`, either
/// `prompt is too long: N tokens > M maximum` or `input length and
/// `max_tokens` exceed context limit: ...`. The error type is shared with
/// every other bad request, so the message decides. A 413
/// `request_too_large` is the request's size in bytes, which compaction
/// does not address, so it stays a status error.
pub(crate) static ANTHROPIC: Overflow = Overflow {
    statuses: &[400],
    labels: &[],
    phrases: &["prompt is too long", "exceed context limit"],
};

/// OpenAI Chat Completions: a 400 with `code: context_length_exceeded`
/// (`This model's maximum context length is N tokens. However, ...`).
pub(crate) static OPENAI_CHAT: Overflow = Overflow {
    statuses: &[400],
    labels: &["context_length_exceeded"],
    phrases: &["maximum context length"],
};

/// OpenAI Responses: a 400 with `code: context_length_exceeded` (`Your
/// input exceeds the context window of this model. ...`) under the default
/// `truncation: "disabled"`, and the same code in a streamed
/// `response.failed` error.
pub(crate) static RESPONSES: Overflow = Overflow {
    statuses: &[400],
    labels: &["context_length_exceeded"],
    phrases: &["exceeds the context window", "maximum context length"],
};

/// DeepSeek: a 400 with OpenAI's wording (`This model's maximum context
/// length is N tokens. However, you requested ...`). Its documented error
/// codes say nothing of context, and the `code` it sends varies, so only
/// the message decides.
pub(crate) static DEEPSEEK: Overflow = Overflow {
    statuses: &[400],
    labels: &[],
    phrases: &["maximum context length"],
};

/// vLLM: a 400 `BadRequestError` (nested `{"error": {...}}` now, flat in
/// older releases), `This model's maximum context length is N tokens.
/// However, ...` from `vllm/renderers/params.py` and the older serving
/// engine.
pub(crate) static VLLM: Overflow = Overflow {
    statuses: &[400],
    labels: &[],
    phrases: &["maximum context length"],
};

/// SGLang: a 400 `BadRequest`, `Input length (N tokens) exceeds the maximum
/// allowed length (M tokens). This model's maximum context length is ...`
/// for the prompt alone, or `Requested token count exceeds the model's
/// maximum context length of N tokens. ...` with the completion.
pub(crate) static SGLANG: Overflow = Overflow {
    statuses: &[400],
    labels: &[],
    phrases: &[
        "maximum context length",
        "exceeds the maximum allowed length",
    ],
};

/// llama.cpp's server: a 400 of type `exceed_context_size_error`
/// (`request (N tokens) exceeds the available context size (M tokens),
/// try increasing it`, or `input (N tokens) is larger than the max context
/// size`). Running out of context while generating is a normal stop, not
/// this.
pub(crate) static LLAMACPP: Overflow = Overflow {
    statuses: &[400],
    labels: &["exceed_context_size_error"],
    phrases: &[
        "exceeds the available context size",
        "larger than the max context size",
    ],
};

/// Gemini: a 400 `INVALID_ARGUMENT`, `The input token count (N) exceeds
/// the maximum number of tokens allowed (M).` The status is shared with
/// every other bad argument, so the message decides.
pub(crate) static GEMINI: Overflow = Overflow {
    statuses: &[400],
    labels: &[],
    phrases: &["exceeds the maximum number of tokens allowed"],
};

#[cfg(test)]
mod tests {
    use super::*;

    fn status(status: u16, message: &str) -> ProviderError {
        ProviderError::Status {
            status,
            retry_after: None,
            message: message.into(),
        }
    }

    fn refused(overflow: &Overflow, status_code: u16, labels: &[&str], message: &str) -> bool {
        matches!(
            overflow.classify(status(status_code, message), labels),
            ProviderError::ContextLength {
                status: Some(code),
                ..
            } if code == status_code
        )
    }

    #[test]
    fn each_provider_matches_its_own_refusal_only() {
        let anthropic = "invalid_request_error: prompt is too long: 201838 tokens > 200000 maximum";
        assert!(refused(&ANTHROPIC, 400, &[], anthropic));
        let anthropic_max = "invalid_request_error: input length and `max_tokens` exceed \
                             context limit: 187986 + 21333 > 200000";
        assert!(refused(&ANTHROPIC, 400, &[], anthropic_max));
        assert!(!refused(
            &ANTHROPIC,
            413,
            &[],
            "request_too_large: too many bytes"
        ));
        assert!(!refused(
            &ANTHROPIC,
            400,
            &[],
            "invalid_request_error: bad tool"
        ));

        let openai = "This model's maximum context length is 4097 tokens. However, your \
                      messages resulted in 9106 tokens.";
        assert!(refused(
            &OPENAI_CHAT,
            400,
            &["context_length_exceeded"],
            "x"
        ));
        assert!(refused(&OPENAI_CHAT, 400, &[], openai));
        assert!(!refused(
            &OPENAI_CHAT,
            429,
            &["context_length_exceeded"],
            openai
        ));

        let responses = "Your input exceeds the context window of this model.";
        assert!(refused(&RESPONSES, 400, &[], responses));
        assert!(RESPONSES.matches(None, &["context_length_exceeded"], ""));

        let deepseek = "This model's maximum context length is 131072 tokens. However, you \
                        requested 131329 tokens";
        assert!(refused(
            &DEEPSEEK,
            400,
            &["invalid_request_error"],
            deepseek
        ));
        assert!(!refused(
            &DEEPSEEK,
            400,
            &["context_length_exceeded"],
            "bad"
        ));

        assert!(refused(&VLLM, 400, &[], openai));
        let sglang = "Input length (9000 tokens) exceeds the maximum allowed length (8192 tokens).";
        assert!(refused(&SGLANG, 400, &[], sglang));
        let llama = "request (9000 tokens) exceeds the available context size (8192 tokens), \
                     try increasing it";
        assert!(refused(&LLAMACPP, 400, &["exceed_context_size_error"], "x"));
        assert!(refused(&LLAMACPP, 400, &[], llama));

        let gemini = "INVALID_ARGUMENT: The input token count (35868) exceeds the maximum \
                      number of tokens allowed (32768).";
        assert!(refused(&GEMINI, 400, &[], gemini));
        assert!(!refused(
            &GEMINI,
            400,
            &[],
            "INVALID_ARGUMENT: context cache not found"
        ));
    }

    /// SGLang 0.5.23 (`tokenizer_manager.py`, live on Qwen3.6-35B-A3B): a prompt
    /// longer than `--context-length` answers 400 `BadRequestError` with this
    /// exact text, which carries neither of the profile's phrases.
    #[test]
    fn sglang_prompt_longer_than_the_context_length_is_a_context_overflow() {
        let live = "The input (40029 tokens) is longer than the model's context length \
                    (32768 tokens).";
        assert!(refused(&SGLANG, 400, &[], live));
    }

    #[test]
    fn anything_else_passes_through() {
        let busy = ProviderError::Transport {
            message: "prompt is too long".into(),
        };
        assert!(matches!(
            ANTHROPIC.classify(busy, &[]),
            ProviderError::Transport { .. }
        ));
    }
}
