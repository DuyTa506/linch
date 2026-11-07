//! The wire form of [`ApprovalResponse`]: tagged by `decision`, strict.

use serde_json::json;

use super::*;

#[test]
fn approval_responses_round_trip_strictly() {
    let deny = ApprovalResponse::Deny {
        reason: "no".into(),
    };
    for (response, wire) in [
        (ApprovalResponse::Allow, json!({"decision": "allow"})),
        (deny, json!({"decision": "deny", "reason": "no"})),
    ] {
        assert_eq!(serde_json::to_value(&response).unwrap(), wire);
        let back: ApprovalResponse = serde_json::from_value(wire).unwrap();
        assert_eq!(back, response);
    }
    for bad in [
        json!({"decision": "maybe"}),
        json!({"decision": "allow", "x": 1}),
        json!({"decision": "deny"}),
        json!({"decision": "deny", "reason": "no", "x": 1}),
        json!("allow"),
    ] {
        let decoded = serde_json::from_value::<ApprovalResponse>(bad.clone());
        assert!(decoded.is_err(), "{bad} must not decode");
    }
}
