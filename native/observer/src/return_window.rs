//! One-shot navigation to a verified existing window; never Agent input/control.

use crate::{
    discovery::VerifiedRegistration,
    identity::{self, ProcessIdentity, Result},
    sampler::now_ms,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    io::Read,
    os::windows::process::CommandExt,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use windows_sys::Win32::{Foundation::*, UI::WindowsAndMessaging::*};

pub fn visible_windows() -> Vec<(usize, u32)> {
    unsafe extern "system" fn visit(hwnd: HWND, param: LPARAM) -> i32 {
        let rows = unsafe { &mut *(param as *mut Vec<(usize, u32)>) };
        if rows.len() >= 4096 {
            return 0;
        }
        if unsafe { IsWindowVisible(hwnd) } != 0 && unsafe { GetAncestor(hwnd, GA_ROOT) } == hwnd {
            let mut pid = 0;
            unsafe {
                GetWindowThreadProcessId(hwnd, &mut pid);
            }
            rows.push((hwnd as usize, pid));
        }
        1
    }
    let mut rows = Vec::new();
    unsafe {
        EnumWindows(Some(visit), (&mut rows as *mut Vec<(usize, u32)>) as isize);
    }
    rows
}
fn verify_window(process: &ProcessIdentity, hwnd: usize) -> Result<()> {
    process.verify()?;
    if !visible_windows()
        .iter()
        .any(|(w, p)| *w == hwnd && *p == process.pid)
    {
        return Err("binding_changed");
    }
    Ok(())
}
#[derive(Clone)]
enum Target {
    Instance(ProcessIdentity, String, Option<usize>),
    Window(ProcessIdentity, usize),
}
struct Grant {
    id: String,
    connection: String,
    generation: String,
    seq: u64,
    expires: Instant,
    expires_ms: u64,
    target: Target,
    selection: Option<String>,
    proof: Vec<(std::path::PathBuf, identity::FileIdentity)>,
}
pub struct Actions {
    grants: Vec<Grant>,
    receipts: HashMap<String, (String, Instant, Value)>,
    picker: HashMap<String, (String, Instant)>,
    last_list: HashMap<String, Instant>,
    picker_registration: HashMap<String, String>,
}
impl Actions {
    pub fn new() -> Self {
        Self {
            grants: Vec::new(),
            receipts: HashMap::new(),
            picker: HashMap::new(),
            last_list: HashMap::new(),
            picker_registration: HashMap::new(),
        }
    }
    fn prune(&mut self) {
        self.grants.retain(|g| g.expires > Instant::now());
        self.receipts
            .retain(|_, (_, t, _)| t.elapsed() < Duration::from_secs(30));
        self.picker
            .retain(|_, (_, t)| t.elapsed() < Duration::from_secs(10));
        self.grants.retain(|g| {
            g.selection.as_ref().is_none_or(|s| {
                self.picker
                    .get(&g.connection)
                    .is_some_and(|(id, _)| id == s)
            })
        });
    }
    pub fn disconnect(&mut self, connection: &str) {
        self.grants.retain(|g| g.connection != connection);
        self.picker.remove(connection);
        self.last_list.remove(connection);
        self.picker_registration.remove(connection);
    }
    pub fn revoke_all(&mut self) {
        self.grants.clear();
        self.picker.clear();
        self.picker_registration.clear();
    }
    fn grant(
        &mut self,
        connection: &str,
        generation: &str,
        seq: u64,
        target: Target,
        selection: Option<String>,
    ) -> Option<Value> {
        self.prune();
        let key = match &target {
            Target::Instance(p, _, w) => (p.key(), *w),
            Target::Window(p, w) => (p.key(), Some(*w)),
        };
        if let Some(grant) = self.grants.iter().find(|g| {
            g.connection == connection
                && match &g.target {
                    Target::Instance(p, _, w) => (p.key(), *w) == key,
                    Target::Window(p, w) => (p.key(), Some(*w)) == key,
                }
        }) {
            return Some(public_grant(grant));
        }
        if self.grants.len() >= 64 {
            return None;
        }
        let grant = Grant {
            id: identity::random_id(),
            connection: connection.into(),
            generation: generation.into(),
            seq,
            expires: selection
                .as_ref()
                .and_then(|_| self.picker.get(connection))
                .map_or(Instant::now(), |(_, t)| *t)
                + Duration::from_secs(10),
            expires_ms: now_ms()
                + selection
                    .as_ref()
                    .and_then(|_| self.picker.get(connection))
                    .map_or(10000, |(_, t)| {
                        10000u64.saturating_sub(t.elapsed().as_millis() as u64)
                    }),
            target,
            selection,
            proof: Vec::new(),
        };
        let public = public_grant(&grant);
        self.grants.push(grant);
        Some(public)
    }
    pub fn observed(
        &mut self,
        connection: &str,
        generation: &str,
        seq: u64,
        process: &ProcessIdentity,
        kind: &str,
        window: Option<usize>,
    ) -> Value {
        // Automatic bindings have no accepted positive evidence in this slice.
        const AUTOMATIC_RETURN_VERIFIED: bool = false;
        if !AUTOMATIC_RETURN_VERIFIED {
            return json!({"status":"limited","reason":"binding_unknown","action_id":null});
        }
        self.grant(
            connection,
            generation,
            seq,
            Target::Instance(process.clone(), kind.into(), window),
            None,
        )
        .unwrap_or_else(
            || json!({"status":"limited","reason":"grant_capacity","action_id":Value::Null}),
        )
    }
    pub fn choices(
        &mut self,
        connection: &str,
        generation: &str,
        seq: u64,
        registration: &VerifiedRegistration,
    ) -> Value {
        self.prune();
        if self
            .last_list
            .get(connection)
            .is_some_and(|t| t.elapsed() < Duration::from_secs(1))
        {
            return crate::protocol::error("action_busy");
        }
        self.last_list.insert(connection.into(), Instant::now());
        if self.picker_registration.get(connection) != Some(&registration.id) {
            self.grants.retain(|g| g.connection != connection);
            self.picker.remove(connection);
            self.picker_registration
                .insert(connection.into(), registration.id.clone());
        }
        let selection = self
            .picker
            .entry(connection.into())
            .or_insert_with(|| (identity::random_id(), Instant::now()))
            .0
            .clone();
        let installations = &registration.images;
        if installations.is_empty() || !registration.valid() {
            return json!({"status":"limited","reason":"unsupported_installation","generation":generation,"selection_id":selection,"expires_at_ms":now_ms()+10000,"coverage":{"status":"limited","omitted":0},"choices":[]});
        }
        let mut rows = Vec::new();
        let mut omitted = 0;
        let me = identity::credentials(unsafe {
            windows_sys::Win32::System::Threading::GetCurrentProcessId()
        })
        .ok();
        for (hwnd, pid) in visible_windows() {
            let Ok(process) = ProcessIdentity::open(pid) else {
                continue;
            };
            if identity::credentials(pid).ok() != me
                || me.is_none()
                || !installations
                    .iter()
                    .any(|p| identity::file_identity(p, false).is_ok_and(|id| id == process.file))
            {
                continue;
            }
            if rows.len() == 32 {
                omitted += 1;
                continue;
            }
            if let Some(mut grant) = self.grant(
                connection,
                generation,
                seq,
                Target::Window(process, hwnd),
                Some(selection.clone()),
            ) {
                if let Some(id) = grant["action_id"].as_str()
                    && let Some(issued) = self.grants.iter_mut().find(|g| g.id == id)
                {
                    issued.proof = registration.proof.clone();
                }
                // Only explicit choice enumeration reaches the title getter.
                let mut text = [0u16; 257];
                let count =
                    unsafe { GetWindowTextW(hwnd as HWND, text.as_mut_ptr(), 257) }.max(0) as usize;
                grant["title"] = json!(bounded_title(&text[..count]));
                text.fill(0);
                rows.push(grant);
            } else {
                omitted += 1;
            }
        }
        json!({"status":if omitted>0{"limited"}else{"available"},"reason":if omitted>0{Some("grant_capacity")}else{None},"generation":generation,"selection_id":selection,"expires_at_ms":rows.iter().filter_map(|r|r["expires_at_ms"].as_u64()).min().unwrap_or(now_ms()+10000),"coverage":{"status":if omitted>0{"limited"}else{"complete"},"omitted":omitted},"choices":rows})
    }
    pub fn dismiss(&mut self, connection: &str, selection: &str) {
        self.grants
            .retain(|g| g.connection != connection || g.selection.as_deref() != Some(selection));
        if self
            .picker
            .get(connection)
            .is_some_and(|(s, _)| s == selection)
        {
            self.picker.remove(connection);
        }
    }
    pub fn execute(&mut self, connection: &str, generation: &str, seq: u64, id: &str) -> Value {
        self.prune();
        if let Some((owner, _, result)) = self.receipts.get(id) {
            return if owner == connection {
                result.clone()
            } else {
                crate::protocol::error("action_expired")
            };
        }
        if self.receipts.len() >= 64 {
            return crate::protocol::error("action_busy");
        }
        let Some(index) = self.grants.iter().position(|g| {
            g.id == id && g.connection == connection && g.generation == generation && g.seq == seq
        }) else {
            return crate::protocol::error("action_expired");
        };
        let grant = self.grants.remove(index);
        let kind = if matches!(grant.target, Target::Window(..)) {
            "manual_navigation"
        } else {
            "observed_instance"
        };
        let result = (|| -> Result<()> {
            for (path, file) in &grant.proof {
                if identity::file_identity(path, false)? != *file {
                    return Err("binding_changed");
                }
            }
            let (process, hwnd) = match &grant.target {
                Target::Window(p, w) => (p.clone(), *w),
                Target::Instance(p, mode, window) => {
                    p.verify()?;
                    if mode == "desktop" {
                        if visible_windows()
                            .iter()
                            .filter(|(_, pid)| *pid == p.pid)
                            .count()
                            != 1
                        {
                            return Err("binding_changed");
                        }
                        (p.clone(), window.ok_or("binding_unknown")?)
                    } else {
                        console_binding(p)?
                    }
                }
            };
            verify_window(&process, hwnd)?;
            unsafe {
                SetForegroundWindow(hwnd as HWND);
            }
            if unsafe { GetForegroundWindow() } != hwnd as HWND {
                return Err("foreground_denied");
            }
            Ok(())
        })();
        if let Some(selection) = grant.selection {
            self.dismiss(connection, &selection);
        }
        let value = match result {
            Ok(()) => json!({"status":"available","reason":Value::Null,"kind":kind}),
            Err(reason) => json!({"status":"limited","reason":reason,"kind":kind}),
        };
        self.receipts.insert(
            id.into(),
            (connection.into(), Instant::now(), value.clone()),
        );
        value
    }
}
fn bounded_title(text: &[u16]) -> String {
    let mut count = text.len().min(256);
    if count > 0 && (0xd800..=0xdbff).contains(&text[count - 1]) {
        count -= 1;
    }
    String::from_utf16_lossy(&text[..count])
}
fn public_grant(grant: &Grant) -> Value {
    let (manual, available) = match &grant.target {
        Target::Window(..) => (true, true),
        Target::Instance(_, kind, window) => (false, kind == "desktop" && window.is_some()),
    };
    json!({"action_id":grant.id,"issued_seq":grant.seq,"expires_at_ms":grant.expires_ms,"kind":if manual{"manual_navigation"}else{"observed_instance"},
        "status":if available{"available"}else{"limited"},"reason":if available{None}else{Some("verify_on_request")},"scope":if manual||available{"application_window"}else{"console_window"}})
}
fn console_binding(process: &ProcessIdentity) -> Result<(ProcessIdentity, usize)> {
    let executable = std::env::current_exe().map_err(|_| "native_missing")?;
    let mut child = Command::new(executable)
        .args([
            "console-proof",
            &process.pid.to_string(),
            &process.created.to_string(),
        ])
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "binding_unknown")?;
    let deadline = Instant::now() + Duration::from_millis(500);
    loop {
        if let Some(status) = child.try_wait().map_err(|_| "binding_unknown")? {
            if !status.success() {
                return Err("binding_unknown");
            }
            break;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("binding_timeout");
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let mut bytes = Vec::new();
    child
        .stdout
        .take()
        .ok_or("binding_unknown")?
        .take(4097)
        .read_to_end(&mut bytes)
        .map_err(|_| "binding_unknown")?;
    if bytes.len() > 4096 {
        return Err("binding_unknown");
    }
    let proof: Value = serde_json::from_slice(&bytes).map_err(|_| "binding_unknown")?;
    process.verify()?;
    let owner =
        ProcessIdentity::open(proof["owner_pid"].as_u64().ok_or("binding_unknown")? as u32)?;
    if Some(owner.created) != proof["owner_created"].as_u64() {
        return Err("binding_changed");
    }
    Ok((
        owner,
        proof["hwnd"].as_u64().ok_or("binding_unknown")? as usize,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unverified_automatic_returns_issue_no_grant() {
        let mut actions = Actions::new();
        for kind in ["desktop", "cli", "custom_exe"] {
            let result = actions.observed("a", "generation", 1, &own(), kind, None);
            assert_eq!(
                result,
                json!({"status":"limited","reason":"binding_unknown","action_id":null})
            );
        }
        assert!(actions.grants.is_empty());
    }
    fn own() -> ProcessIdentity {
        ProcessIdentity::open(std::process::id()).unwrap()
    }
    #[test]
    fn grants_reuse_issued_sequence_and_hard_capacity() {
        let mut actions = Actions::new();
        let process = own();
        let first = actions
            .grant(
                "a",
                "generation",
                1,
                Target::Window(process.clone(), 1),
                None,
            )
            .unwrap();
        let again = actions
            .grant(
                "a",
                "generation",
                99,
                Target::Window(process.clone(), 1),
                None,
            )
            .unwrap();
        assert_eq!(first, again);
        assert_eq!(again["issued_seq"], 1);
        for hwnd in 2..=64 {
            assert!(
                actions
                    .grant(
                        "a",
                        "generation",
                        2,
                        Target::Window(process.clone(), hwnd),
                        None
                    )
                    .is_some()
            );
        }
        assert!(
            actions
                .grant("b", "generation", 3, Target::Window(process, 65), None)
                .is_none()
        );
        actions.disconnect("a");
        assert!(actions.grants.is_empty());
    }
    #[test]
    fn stale_identity_never_reaches_foreground_and_is_not_replayed() {
        let mut actions = Actions::new();
        let mut changed = own();
        changed.created ^= 1;
        let grant = actions
            .grant("a", "generation", 1, Target::Window(changed, 1), None)
            .unwrap();
        let id = grant["action_id"].as_str().unwrap();
        let first = actions.execute("a", "generation", 1, id);
        assert_eq!(first["reason"], "instance_changed");
        assert_eq!(actions.execute("a", "generation", 1, id), first);
        assert_eq!(
            actions.execute("b", "generation", 1, id)["reason"],
            "action_expired"
        );
    }
    #[test]
    fn expiry_dismiss_and_receipt_pressure_do_not_focus() {
        let mut actions = Actions::new();
        let process = own();
        let grant = actions
            .grant(
                "a",
                "generation",
                1,
                Target::Window(process.clone(), 1),
                Some("selection".into()),
            )
            .unwrap();
        actions.dismiss("b", "selection");
        assert_eq!(actions.grants.len(), 1);
        actions.dismiss("a", "selection");
        assert!(actions.grants.is_empty());
        let grant2 = actions
            .grant("a", "generation", 2, Target::Window(process, 2), None)
            .unwrap();
        actions.grants[0].expires = Instant::now() - Duration::from_secs(1);
        assert_eq!(
            actions.execute("a", "generation", 2, grant2["action_id"].as_str().unwrap())["reason"],
            "action_expired"
        );
        for index in 0..64 {
            actions.receipts.insert(
                index.to_string(),
                ("a".into(), Instant::now(), json!({"status":"limited"})),
            );
        }
        assert_eq!(
            actions.execute("a", "generation", 1, grant["action_id"].as_str().unwrap())["reason"],
            "action_busy"
        );
    }
    #[test]
    fn title_budget_handles_surrogates_without_interpreting_html() {
        let mut title = vec![b'x' as u16; 255];
        title.extend([0xd83d, 0xde00]);
        assert_eq!(bounded_title(&title).encode_utf16().count(), 255);
        assert_eq!(
            bounded_title(&[b'<' as u16, b'b' as u16, b'>' as u16]),
            "<b>"
        );
        assert_eq!(bounded_title(&[0xdc00]), "\u{fffd}");
    }
    #[test]
    fn picker_deadline_is_shared_and_never_extended() {
        let mut actions = Actions::new();
        let began = Instant::now() - Duration::from_secs(9);
        actions
            .picker
            .insert("a".into(), ("selection".into(), began));
        let grant = actions
            .grant(
                "a",
                "generation",
                1,
                Target::Window(own(), 1),
                Some("selection".into()),
            )
            .unwrap();
        assert!(grant["expires_at_ms"].as_u64().unwrap() <= now_ms() + 1000);
        assert_eq!(actions.grants[0].expires, began + Duration::from_secs(10));
        actions.picker.get_mut("a").unwrap().1 = Instant::now() - Duration::from_secs(11);
        actions.prune();
        assert!(actions.grants.is_empty());
    }
}
