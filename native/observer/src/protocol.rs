//! Bounded, strict messages shared with the Python observer client.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: u32 = 2;
pub const REQUEST_MAX: usize = 64 * 1024;
pub const RESPONSE_MAX: usize = 512 * 1024;
pub const BUILD: &str = match option_env!("OBS_BUILD_ID") {
    Some(value) => value,
    None => "development",
};

#[derive(Debug, Deserialize)]
#[serde(tag = "method", rename_all = "snake_case", deny_unknown_fields)]
pub enum Request {
    Hello {
        protocol: u32,
        profile_key: String,
        role: Role,
        build: String,
    },
    Status {},
    GetSnapshot {},
    SyncRegistrations {
        base_revision: u64,
        base_hash: String,
        config_revision: u64,
        config_hash: String,
        observations: crate::registrations::Observations,
    },
    ListInstallationCandidates {},
    SyncScopeBegin {
        transaction_id: String,
        base_revision: u64,
        scope_hash: String,
    },
    SyncScopeChunk {
        transaction_id: String,
        index: u32,
        roots: Vec<ScopeRoot>,
    },
    SyncScopeCommit {
        transaction_id: String,
    },
    ListWindowChoices {
        registration_id: String,
        config_revision: u64,
        config_hash: String,
    },
    DismissWindowChoices {
        selection_id: String,
    },
    ReturnWindow {
        action_id: String,
        generation: String,
        seq: u64,
        config_revision: u64,
        config_hash: String,
    },
    Stop {},
}

#[derive(Debug, PartialEq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Python,
    Cli,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScopeRoot {
    pub directory_id: String,
    pub path: String,
}

pub fn decode(data: &[u8]) -> Result<Request, &'static str> {
    if data.is_empty() || data.len() > REQUEST_MAX {
        return Err("frame_size");
    }
    let (mut depth, mut quoted, mut escaped) = (0usize, false, false);
    for byte in data {
        if quoted {
            if escaped {
                escaped = false;
            } else if *byte == b'\\' {
                escaped = true;
            } else if *byte == b'"' {
                quoted = false;
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'{' | b'[' => {
                    depth += 1;
                    if depth > 12 {
                        return Err("json_depth");
                    }
                }
                b'}' | b']' => depth = depth.saturating_sub(1),
                _ => (),
            }
        }
    }
    serde_json::from_slice(data).map_err(|_| "invalid_message")
}

pub fn error(reason: &str) -> Value {
    serde_json::json!({"status":"unavailable","reason":reason})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_fields_and_duplicates() {
        assert!(decode(br#"{"method":"status"}"#).is_ok());
        assert!(decode(br#"{"method":"status","path":"secret"}"#).is_err());
        assert!(decode(br#"{"method":"hello","protocol":1,"protocol":1,"profile_key":"x","role":"python","build":"x"}"#).is_err());
        assert!(decode(&vec![b' '; REQUEST_MAX + 1]).is_err());
    }
}
