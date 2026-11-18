use super::*;

#[test]
fn a_plain_endpoint_passes() {
    for url in [
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/",
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "http://127.0.0.1:8201/v1",
        "http://[::1]:8000/v1",
        "https://gateway.example.com/openai/v1",
        "HTTPS://Gateway.Example.com/v1",
    ] {
        assert_eq!(check_base_url(url), Ok(()), "{url}");
    }
}

/// Each of these would send the key to a path other than the adapter's
/// own, or to another host than the URL reads as.
#[test]
fn a_url_that_steers_the_request_is_refused() {
    let hostile = [
        // A fragment or a query swallows the appended path.
        (
            "https://api.openai.com/v1/files#",
            BaseUrlError::QueryOrFragment,
        ),
        (
            "https://api.openai.com/v1/files?",
            BaseUrlError::QueryOrFragment,
        ),
        (
            "https://api.openai.com/v1?x=",
            BaseUrlError::QueryOrFragment,
        ),
        (
            "https://api.openai.com/v1#frag",
            BaseUrlError::QueryOrFragment,
        ),
        // Dot segments, as written, as escapes and as backslashes.
        (
            "https://api.openai.com/v1/../files",
            BaseUrlError::PathSegments,
        ),
        ("https://api.openai.com/v1/./x", BaseUrlError::PathSegments),
        ("https://api.openai.com/v1/..", BaseUrlError::PathSegments),
        (
            "https://api.openai.com/v1/%2e%2e/files",
            BaseUrlError::Escaped,
        ),
        (
            "https://api.openai.com/v1/%2E%2E/files",
            BaseUrlError::Escaped,
        ),
        ("https://api.openai.com/v1%2ffiles", BaseUrlError::Escaped),
        (
            "https://api.openai.com/v1/%252e%252e",
            BaseUrlError::Escaped,
        ),
        (
            "https://api.openai.com/v1\\..\\files",
            BaseUrlError::Escaped,
        ),
        // An empty segment.
        ("https://api.openai.com//v1", BaseUrlError::PathSegments),
        (
            "https://api.openai.com/v1//files",
            BaseUrlError::PathSegments,
        ),
        // Userinfo moves the host.
        (
            "https://api.openai.com@evil.example/v1",
            BaseUrlError::Userinfo,
        ),
        ("https://user:pw@api.openai.com/v1", BaseUrlError::Userinfo),
        ("https://@api.openai.com/v1", BaseUrlError::Userinfo),
        // Whitespace and control characters.
        ("https://api.openai.com/v1 /x", BaseUrlError::Escaped),
        ("https://api.openai.com/v1\n/x", BaseUrlError::Escaped),
        // Not http(s), or not absolute.
        ("ftp://api.openai.com/v1", BaseUrlError::NotHttp),
        ("file:///etc/passwd", BaseUrlError::NotHttp),
        ("api.openai.com/v1", BaseUrlError::NotHttp),
        ("https:api.openai.com/v1", BaseUrlError::NotHttp),
        ("", BaseUrlError::NotHttp),
    ];
    for (url, why) in hostile {
        assert_eq!(check_base_url(url), Err(why), "{url:?}");
    }
}

#[test]
fn the_refusal_never_echoes_the_url() {
    let error = check_base_url("https://user:s3cret@api.openai.com/v1").unwrap_err();
    let text = format!("{error} {error:?}");
    assert!(!text.contains("s3cret") && !text.contains("api.openai.com"));
}

/// Every adapter checks its base at build, so a steering URL never gets a
/// key to send, whatever built the options.
#[test]
fn every_adapter_refuses_a_steering_base_url_when_it_is_built() {
    use crate::{
        Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
        OpenAiChat, OpenAiChatOptions, OpenAiResponses, ResponsesOptions, Server, ServerOptions,
    };
    let key = || ApiKey::new("sk-base-url-test").unwrap();
    let hostile = [
        "https://api.openai.com/v1/files#",
        "https://api.openai.com/v1/files?",
        "https://api.openai.com/v1/%2e%2e/files",
        "https://api.openai.com/v1/../files",
        "https://api.openai.com//v1",
        "https://user@api.openai.com/v1",
    ];
    for url in hostile {
        let refused = |result: Result<(), linch_core::ProviderError>| {
            let error = result.unwrap_err();
            assert!(matches!(
                error,
                linch_core::ProviderError::InvalidRequest { .. }
            ));
            let text = error.to_string();
            assert!(!text.contains("api.openai.com"), "{text}");
        };
        let mut options = OpenAiChatOptions::new(key());
        options.base_url = url.into();
        refused(OpenAiChat::new(options).map(drop));
        let mut options = ResponsesOptions::new(key());
        options.base_url = url.into();
        refused(OpenAiResponses::new(options).map(drop));
        let mut options = DeepSeekOptions::new(key());
        options.base_url = url.into();
        refused(DeepSeek::new(options).map(drop));
        let mut options = AnthropicOptions::new(key());
        options.base_url = url.into();
        refused(Anthropic::new(options).map(drop));
        let mut options = GeminiOptions::new(key());
        options.base_url = url.into();
        refused(Gemini::new(options).map(drop));
        for server in [Server::Vllm, Server::SgLang, Server::LlamaCpp] {
            refused(OpenAiChat::server(ServerOptions::new(server, url)).map(drop));
        }
    }
}
