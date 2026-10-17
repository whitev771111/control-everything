//! A bounded in-memory copy of Python-owned observation intent.

use crate::identity::Result;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_BYTES: usize = 48 * 1024;
pub const MAX_REVISION: u64 = 9_007_199_254_740_991;
pub fn local_locator(path: &str) -> bool {
    let b = path.as_bytes();
    b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b"/\\".contains(&b[2])
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Registration {
    pub id: String,
    pub category: String,
    #[serde(deserialize_with = "provider_id")]
    pub provider_id: Option<String>,
    pub environment_id: String,
    pub label: String,
    pub locator_kind: String,
    pub path: String,
    pub enabled: bool,
    pub binding_state: String,
}
fn provider_id<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<String>, D::Error> {
    Option::<String>::deserialize(d)
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Observations {
    pub revision: u64,
    pub registrations: Vec<Registration>,
}

impl Registration {
    pub fn active(&self) -> bool {
        self.enabled && self.binding_state == "confirmed"
    }
}

impl Observations {
    pub fn hash(&self) -> String {
        // Value uses sorted map keys; strings use the same UTF-8 JSON as Python.
        let canonical = serde_json::to_value(self).expect("serializable registration");
        crate::identity::hex(&Sha256::digest(serde_json::to_vec(&canonical).unwrap()))
    }

    pub fn validate(&self) -> Result<()> {
        if self.revision > MAX_REVISION
            || self.registrations.len() > 32
            || (self.revision == 0 && !self.registrations.is_empty())
            || serde_json::to_vec(self).map_or(true, |v| v.len() > MAX_BYTES)
        {
            return Err("invalid_config");
        }
        let mut previous = "";
        for row in &self.registrations {
            let valid_kind = match (
                row.category.as_str(),
                row.provider_id.as_deref(),
                row.locator_kind.as_str(),
            ) {
                ("custom_exe", None, "executable") => {
                    row.path.to_ascii_lowercase().ends_with(".exe")
                }
                ("builtin", Some("codex" | "claude" | "pi"), "npm_command")
                | ("builtin", Some("hermes"), "hermes_launcher")
                | ("builtin", Some("codex"), "codex_desktop") => true,
                _ => false,
            };
            if row.id.len() != 32
                || !row
                    .id
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                || row.id.as_str() <= previous
                || row.environment_id != "windows:local"
                || !valid_kind
                || row.label.is_empty()
                || row.label.chars().count() > 80
                || row.label.chars().any(char::is_control)
                || row.path.is_empty()
                || row.path.chars().count() > 2048
                || row.path.chars().any(char::is_control)
                || !local_locator(&row.path)
                || !["confirmed", "rebind_required"].contains(&row.binding_state.as_str())
            {
                return Err("invalid_config");
            }
            previous = &row.id;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_vectors_and_strict_fields() {
        let schema: serde_json::Value =
            serde_json::from_str(include_str!("../protocol.json")).unwrap();
        for vector in schema["registration_vectors"].as_array().unwrap() {
            let value: Observations =
                serde_json::from_value(vector["observations"].clone()).unwrap();
            value.validate().unwrap();
            assert_eq!(value.hash(), vector["sha256"].as_str().unwrap());
        }
        let mut row = schema["registration_vectors"][1]["observations"].clone();
        row["registrations"][0]["shell"] = serde_json::json!("never");
        assert!(serde_json::from_value::<Observations>(row).is_err());
    }
    #[test]
    fn rejects_order_duplicates_combinations_and_budgets() {
        let schema: serde_json::Value =
            serde_json::from_str(include_str!("../protocol.json")).unwrap();
        let mut value: Observations =
            serde_json::from_value(schema["registration_vectors"][1]["observations"].clone())
                .unwrap();
        value.registrations.push(value.registrations[0].clone());
        assert!(value.validate().is_err());
        value.registrations.pop();
        value.registrations[0].provider_id = Some("codex".into());
        assert!(value.validate().is_err());
        value.registrations[0].provider_id = None;
        value.revision = MAX_REVISION + 1;
        assert!(value.validate().is_err());
        value.revision = 1;
        value.registrations[0].path = "relative.exe".into();
        assert!(value.validate().is_err());
    }
    #[test]
    fn local_locator_rejects_unc_device_and_relative_without_io() {
        for path in [
            r"\\example.invalid\share\tool.exe",
            r"\\?\C:\example\tool.exe",
            r"\root.exe",
            r"C:tool.exe",
            "relative.exe",
        ] {
            assert!(!local_locator(path));
        }
        assert!(local_locator("C:/example/tool.exe"));
        assert!(local_locator(r"D:\example\tool.exe"));
    }
}
