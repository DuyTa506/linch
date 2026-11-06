//! JSON-RPC 2.0 message objects. Reading is strict: an unknown member, a
//! wrong `jsonrpc` version or a mistyped member is an error.

use linch::{JsonObject, JsonValue};
use serde::{Deserialize, Serialize};

use crate::error::{ErrorObject, invalid_params, invalid_request};

/// The `jsonrpc` member. Only `"2.0"` exists.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum Version {
    /// JSON-RPC 2.0.
    #[default]
    #[serde(rename = "2.0")]
    V2,
}

/// A request id: a string, a number, or null (JSON-RPC allows all three).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Id {
    /// A numeric id, echoed exactly.
    Number(serde_json::Number),
    /// A string id.
    String(String),
    /// A null id; used when a request's id could not be read.
    Null,
}

/// A call or notification from a client.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Request {
    /// Always 2.0.
    pub jsonrpc: Version,
    /// Absent for a notification, which gets no response.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<Id>,
    /// Method name.
    pub method: String,
    /// Named parameters; an absent `params` reads as an empty object.
    pub params: JsonObject,
}

/// One response, carrying a result or an error.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Response {
    /// Always 2.0.
    pub jsonrpc: Version,
    /// The id of the request this answers.
    pub id: Id,
    /// The result on success. Every result of this protocol is an object.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<JsonValue>,
    /// The error on failure.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorObject>,
}

/// A server-to-client notification (`run.event`, `run.end`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Notification {
    /// Always 2.0.
    pub jsonrpc: Version,
    /// Notification name.
    pub method: String,
    /// Its parameters, an object.
    pub params: JsonValue,
}

/// What a client reads from the server: a response or a notification.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(untagged)]
pub enum Message {
    /// The answer to one call.
    Response(Response),
    /// A server notification.
    Notification(Notification),
}

impl Request {
    /// Read one request object.
    ///
    /// Args:
    ///     value: One decoded JSON line.
    ///
    /// Returns:
    ///     The request, with an absent `params` as an empty object.
    ///
    /// Raises:
    ///     Response: The error response to send: invalid request or invalid
    ///         params, with the request's id (null when it could not be
    ///         read). A batch array is an invalid request: batches are not
    ///         supported.
    pub fn from_value(value: JsonValue) -> Result<Self, Box<Response>> {
        let reject = |id: &Id, error| Box::new(Response::err(id.clone(), error));
        let mut object = match value {
            JsonValue::Object(object) => object,
            JsonValue::Array(_) => {
                return Err(reject(
                    &Id::Null,
                    invalid_request("batches are not supported"),
                ));
            }
            _ => return Err(reject(&Id::Null, invalid_request("a request is an object"))),
        };
        let id = match object.remove("id") {
            None => None,
            Some(value) => Some(serde_json::from_value::<Id>(value).map_err(|_| {
                reject(
                    &Id::Null,
                    invalid_request("id must be a string, number or null"),
                )
            })?),
        };
        let reply_id = id.clone().unwrap_or(Id::Null);
        let invalid = |message: &str| reject(&reply_id, invalid_request(message));
        match object.remove("jsonrpc") {
            Some(JsonValue::String(version)) if version == "2.0" => {}
            _ => return Err(invalid("jsonrpc must be \"2.0\"")),
        }
        let method = match object.remove("method") {
            Some(JsonValue::String(method)) => method,
            _ => return Err(invalid("method must be a string")),
        };
        let params = match object.remove("params") {
            None => JsonObject::new(),
            Some(JsonValue::Object(params)) => params,
            Some(_) => {
                let error = invalid_params("params must be an object");
                return Err(reject(&reply_id, error));
            }
        };
        if let Some(member) = object.keys().next() {
            return Err(invalid(&format!("unexpected member {member:?}")));
        }
        Ok(Self {
            jsonrpc: Version::V2,
            id,
            method,
            params,
        })
    }
}

impl Response {
    /// A success response.
    pub fn ok(id: Id, result: JsonValue) -> Self {
        Self {
            jsonrpc: Version::V2,
            id,
            result: Some(result),
            error: None,
        }
    }

    /// An error response.
    pub fn err(id: Id, error: ErrorObject) -> Self {
        Self {
            jsonrpc: Version::V2,
            id,
            result: None,
            error: Some(error),
        }
    }

    /// The error if there is one, else the result (null when absent).
    pub fn into_result(self) -> Result<JsonValue, ErrorObject> {
        match self.error {
            Some(error) => Err(error),
            None => Ok(self.result.unwrap_or(JsonValue::Null)),
        }
    }
}

impl Notification {
    /// A notification named `method` with `params`.
    pub fn new(method: impl Into<String>, params: JsonValue) -> Self {
        Self {
            jsonrpc: Version::V2,
            method: method.into(),
            params,
        }
    }
}
