//! Typed identifiers, so a run id cannot be passed where a session id belongs.

use serde::{Deserialize, Serialize};

macro_rules! id_type {
    ($(#[$meta:meta])* $name:ident) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(String);

        impl $name {
            /// Wrap an existing identifier.
            pub fn new(value: impl Into<String>) -> Self {
                Self(value.into())
            }

            /// The identifier text.
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(&self.0)
            }
        }

        impl From<String> for $name {
            fn from(value: String) -> Self {
                Self(value)
            }
        }

        impl From<&str> for $name {
            fn from(value: &str) -> Self {
                Self(value.to_owned())
            }
        }
    };
}

id_type!(
    /// Identifies one conversation and its append-only log.
    SessionId
);
id_type!(
    /// Identifies one run of the agent loop. A child run records its parent.
    RunId
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_serialize_as_plain_strings() {
        let id = RunId::new("run-1");
        assert_eq!(serde_json::to_string(&id).unwrap(), "\"run-1\"");
        let back: RunId = serde_json::from_str("\"run-1\"").unwrap();
        assert_eq!(back, id);
        assert_eq!(back.to_string(), "run-1");
    }
}
