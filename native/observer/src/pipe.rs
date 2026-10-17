//! One profile pipe, bounded frames, peer checks, and connection-owned scope staging.

use crate::{
    identity::{self, Result},
    protocol::{self, Request, Role, ScopeRoot},
    return_window::Actions,
    sampler::Sampler,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    os::windows::io::AsRawHandle,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::windows::named_pipe::{NamedPipeServer, ServerOptions},
    sync::{Notify, Semaphore},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    System::{Pipes::*, Threading::*},
};

struct Staging {
    connection: String,
    transaction: String,
    base: u64,
    hash: String,
    started: Instant,
    index: u32,
    bytes: usize,
    roots: Vec<ScopeRoot>,
}
pub struct Runtime {
    pub sampler: Sampler,
    actions: Mutex<Actions>,
    stage: Mutex<Option<Staging>>,
    stop: AtomicBool,
    wake: Notify,
    grant_cursor: AtomicUsize,
}
impl Runtime {
    pub fn new(profile: String) -> Self {
        Self {
            sampler: Sampler::new(profile, identity::random_id()),
            actions: Mutex::new(Actions::new()),
            stage: Mutex::new(None),
            stop: AtomicBool::new(false),
            wake: Notify::new(),
            grant_cursor: AtomicUsize::new(0),
        }
    }
    fn status(&self) -> Value {
        let scope = self.sampler.scope.lock().unwrap();
        let config = self.sampler.config.lock().unwrap();
        json!({"status":"available","protocol":protocol::VERSION,"profile_key":self.sampler.profile,"generation":self.sampler.generation,"build":protocol::BUILD,"scope_revision":scope.revision,"scope_hash":scope.hash,"config_revision":config.revision,"config_hash":config.hash()})
    }
    fn disconnect(&self, id: &str) {
        self.actions.lock().unwrap().disconnect(id);
        let mut stage = self.stage.lock().unwrap();
        if stage.as_ref().is_some_and(|s| s.connection == id) {
            *stage = None;
        }
    }
    fn request(&self, id: &str, role: &Role, request: Request) -> Result<Value> {
        if matches!(
            &request,
            Request::SyncRegistrations { .. }
                | Request::ListInstallationCandidates {}
                | Request::ListWindowChoices { .. }
                | Request::ReturnWindow { .. }
        ) && *role != Role::Python
        {
            return Err("role_rejected");
        }
        {
            let mut stage = self.stage.lock().unwrap();
            if stage
                .as_ref()
                .is_some_and(|s| s.started.elapsed() > Duration::from_secs(5))
            {
                *stage = None;
            }
        }
        match request {
            Request::Status {} => Ok(self.status()),
            Request::SyncRegistrations {
                base_revision,
                base_hash,
                config_revision,
                config_hash,
                observations,
            } => {
                observations.validate()?;
                if observations.revision != config_revision || observations.hash() != config_hash {
                    return Err("invalid_config");
                }
                let mut current = self.sampler.config.lock().unwrap();
                if current.revision != config_revision || current.hash() != config_hash {
                    if current.revision != base_revision || current.hash() != base_hash {
                        self.actions.lock().unwrap().disconnect(id);
                        return Err("config_conflict");
                    }
                    *current = observations;
                    self.actions.lock().unwrap().revoke_all();
                }
                Ok(
                    json!({"status":"available","config_revision":config_revision,"config_hash":config_hash}),
                )
            }
            Request::ListInstallationCandidates {} => {
                let current = self.sampler.config.lock().unwrap();
                let view = self.sampler.catalog.lock().unwrap();
                if view.config_hash != current.hash() {
                    return Err("config_pending");
                }
                let mut rows = Vec::new();
                let mut omitted = 0;
                for row in &view.candidates {
                    rows.push(row.clone());
                    if rows.len() > 64
                        || serde_json::to_vec(&rows).map_or(true, |v| v.len() > 60 * 1024)
                    {
                        rows.pop();
                        omitted += 1;
                    }
                }
                Ok(
                    json!({"status":if omitted>0{"limited"}else{"available"},"reason":if omitted>0{Some("snapshot_budget_exceeded")}else{None},"config_revision":current.revision,"config_hash":current.hash(),"candidates":rows,"coverage":{"status":if omitted>0{"limited"}else{"complete"},"omitted":omitted}}),
                )
            }
            Request::GetSnapshot {} => {
                let revision = self.sampler.scope.lock().unwrap().revision;
                let current = self.sampler.config.lock().unwrap();
                let config_hash = current.hash();
                let sample = self.sampler.sample.lock().unwrap();
                let mut value = sample.value.clone();
                if value.get("generation").is_none()
                    || value["scope_revision"].as_u64() != Some(revision)
                    || value["config_revision"].as_u64() != Some(current.revision)
                    || value["config_hash"] != config_hash
                {
                    self.actions.lock().unwrap().disconnect(id);
                    return Ok(
                        json!({"status":"unknown","reason":if value["scope_revision"].as_u64()!=Some(revision){"scope_pending"}else{"config_pending"},"protocol":protocol::VERSION,"profile_key":self.sampler.profile,"generation":self.sampler.generation,"seq":value["seq"].as_u64().unwrap_or(0),"scope_revision":revision,"config_revision":current.revision,"config_hash":config_hash,"registrations":[],"environment_id":"windows:local","sampled_at_ms":0,"metrics_interval_ms":0,"coverage":{"status":"unknown"},"installations":[],"instances":[],"resources":[]}),
                    );
                }
                let seq = value["seq"].as_u64().unwrap_or(0);
                let mut actions = self.actions.lock().unwrap();
                if crate::sampler::now_ms()
                    .saturating_sub(value["sampled_at_ms"].as_u64().unwrap_or(0))
                    > 12000
                {
                    actions.disconnect(id);
                    value["status"] = json!("stale");
                    value["reason"] = json!("sample_timeout");
                    if let Some(rows) = value["instances"].as_array_mut() {
                        for row in rows {
                            row["status"] = json!("stale");
                            row["return_action"] = json!({"status":"stale","reason":"sample_timeout","action_id":null});
                        }
                    }
                    return Ok(value);
                }
                if let Some(instances) = value["instances"].as_array_mut() {
                    let count = instances.len();
                    let start = self.grant_cursor.fetch_add(64, Ordering::Relaxed);
                    for offset in 0..count {
                        let row = &mut instances[(start + offset) % count];
                        if let Some(target) = row["instance_id"]
                            .as_str()
                            .and_then(|key| sample.targets.get(key))
                        {
                            row["return_action"] = actions.observed(
                                id,
                                &self.sampler.generation,
                                seq,
                                &target.process,
                                &target.kind,
                                target.window,
                            );
                        }
                    }
                }
                Ok(value)
            }
            Request::SyncScopeBegin {
                transaction_id,
                base_revision,
                scope_hash,
            } => {
                if !opaque(&transaction_id)
                    || scope_hash.len() != 64
                    || !scope_hash.bytes().all(|b| b.is_ascii_hexdigit())
                {
                    return Err("invalid_scope");
                }
                let mut stage = self.stage.lock().unwrap();
                if stage.is_some() {
                    return Err("scope_busy");
                }
                let scope = self.sampler.scope.lock().unwrap();
                if scope_hash == scope.hash {
                    return Ok(
                        json!({"status":"available","unchanged":true,"scope_revision":scope.revision,"scope_hash":scope.hash}),
                    );
                }
                if base_revision != scope.revision {
                    self.actions.lock().unwrap().disconnect(id);
                    return Err("scope_conflict");
                }
                *stage = Some(Staging {
                    connection: id.into(),
                    transaction: transaction_id,
                    base: base_revision,
                    hash: scope_hash,
                    started: Instant::now(),
                    index: 0,
                    bytes: 0,
                    roots: Vec::new(),
                });
                Ok(json!({"status":"available","unchanged":false,"scope_revision":scope.revision}))
            }
            Request::SyncScopeChunk {
                transaction_id,
                index,
                roots,
            } => {
                let mut stage = self.stage.lock().unwrap();
                let s = stage.as_mut().ok_or("scope_expired")?;
                if s.connection != id || s.transaction != transaction_id || s.index != index {
                    return Err("scope_conflict");
                }
                let bytes = serde_json::to_vec(&roots)
                    .map_err(|_| "invalid_scope")?
                    .len();
                let mut directory_ids: std::collections::HashSet<_> =
                    s.roots.iter().map(|r| r.directory_id.as_str()).collect();
                if s.bytes + bytes > 8 * 1024 * 1024
                    || s.roots.len() + roots.len() > 8000
                    || roots.iter().any(|r| {
                        !opaque(&r.directory_id)
                            || r.path.is_empty()
                            || r.path.chars().any(char::is_control)
                            || !std::path::Path::new(&r.path).is_absolute()
                            || !directory_ids.insert(r.directory_id.as_str())
                            || r.path.chars().count() > 2048
                    })
                {
                    *stage = None;
                    return Err("scope_budget_exceeded");
                }
                s.bytes += bytes;
                s.roots.extend(roots);
                s.index += 1;
                Ok(json!({"status":"available","index":index}))
            }
            Request::SyncScopeCommit { transaction_id } => {
                let mut stage = self.stage.lock().unwrap();
                let s = stage.as_ref().ok_or("scope_expired")?;
                if s.connection != id || s.transaction != transaction_id {
                    return Err("scope_conflict");
                }
                let mut scope = self.sampler.scope.lock().unwrap();
                if scope.revision != s.base {
                    *stage = None;
                    return Err("scope_conflict");
                }
                let s = stage.take().unwrap();
                if scope_digest(&s.roots) != s.hash {
                    return Err("scope_hash_mismatch");
                }
                scope.roots = s.roots;
                scope.hash = s.hash;
                scope.revision += 1;
                self.actions.lock().unwrap().revoke_all();
                Ok(
                    json!({"status":"available","scope_revision":scope.revision,"scope_hash":scope.hash}),
                )
            }
            Request::ListWindowChoices {
                registration_id,
                config_revision,
                config_hash,
            } => {
                let current = self.sampler.config.lock().unwrap();
                if current.revision != config_revision || current.hash() != config_hash {
                    return Err("config_pending");
                }
                let sample = self.sampler.sample.lock().unwrap();
                if sample.value["config_hash"] != config_hash
                    || crate::sampler::now_ms()
                        .saturating_sub(sample.value["sampled_at_ms"].as_u64().unwrap_or(0))
                        > 12000
                {
                    return Err("config_pending");
                }
                let seq = sample.value["seq"].as_u64().unwrap_or(0);
                let view = self.sampler.catalog.lock().unwrap();
                if view.config_hash != config_hash {
                    return Err("config_pending");
                }
                let registration = view
                    .registrations
                    .iter()
                    .find(|r| r.id == registration_id)
                    .ok_or("unsupported_installation")?;
                let mut value = self.actions.lock().unwrap().choices(
                    id,
                    &self.sampler.generation,
                    seq,
                    registration,
                );
                if value.get("choices").is_some() {
                    value["config_revision"] = json!(config_revision);
                    value["config_hash"] = json!(config_hash);
                }
                Ok(value)
            }
            Request::DismissWindowChoices { selection_id } => {
                if !opaque(&selection_id) {
                    return Err("invalid_action");
                }
                self.actions.lock().unwrap().dismiss(id, &selection_id);
                Ok(json!({"status":"available"}))
            }
            Request::ReturnWindow {
                action_id,
                generation,
                seq,
                config_revision,
                config_hash,
            } => {
                if !opaque(&action_id) || generation != self.sampler.generation {
                    return Err("action_expired");
                }
                let current = self.sampler.config.lock().unwrap();
                if current.revision != config_revision || current.hash() != config_hash {
                    self.actions.lock().unwrap().disconnect(id);
                    return Err("config_pending");
                }
                Ok(self
                    .actions
                    .lock()
                    .unwrap()
                    .execute(id, &generation, seq, &action_id))
            }
            Request::Stop {} => {
                if *role != Role::Cli {
                    return Err("role_rejected");
                }
                self.stop.store(true, Ordering::Relaxed);
                self.sampler.stop.store(true, Ordering::Relaxed);
                self.wake.notify_one();
                Ok(json!({"status":"available"}))
            }
            Request::Hello { .. } => Err("handshake_repeated"),
        }
    }
    fn dispatch(&self, id: &str, role: &Role, request: Request) -> Value {
        match self.request(id, role, request) {
            Ok(value) => value,
            Err(reason) => {
                let mut stage = self.stage.lock().unwrap();
                if stage.as_ref().is_some_and(|s| s.connection == id) {
                    *stage = None;
                }
                protocol::error(reason)
            }
        }
    }
}
fn opaque(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn scope_digest(roots: &[ScopeRoot]) -> String {
    struct HashWriter(Sha256);
    impl std::io::Write for HashWriter {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            self.0.update(data);
            Ok(data.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut writer = HashWriter(Sha256::new());
    // Struct field order is directory_id,path: Python sort_keys canonical order.
    serde_json::to_writer(&mut writer, roots).expect("scope_serialization_failed");
    identity::hex(&writer.0.finalize())
}
fn create(name: &str, first: bool) -> Result<NamedPipeServer> {
    let current = identity::credentials(unsafe { GetCurrentProcessId() })?;
    let mut sid = std::ptr::null_mut();
    if unsafe { ConvertSidToStringSidW(current.sid.as_ptr() as *mut _, &mut sid) } == 0 {
        return Err("pipe_security_failed");
    }
    let mut length = 0;
    while unsafe { *sid.add(length) } != 0 {
        length += 1;
    }
    let sid_text = unsafe { String::from_utf16_lossy(std::slice::from_raw_parts(sid, length)) };
    unsafe {
        LocalFree(sid.cast());
    }
    let sddl = identity::wide(format!("D:P(A;;GA;;;{sid_text})"));
    let mut descriptor = std::ptr::null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err("pipe_security_failed");
    }
    let mut attrs = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor,
        bInheritHandle: 0,
    };
    let result = unsafe {
        ServerOptions::new()
            .first_pipe_instance(first)
            .reject_remote_clients(true)
            .max_instances(5)
            .in_buffer_size(65536)
            .out_buffer_size(65536)
            .create_with_security_attributes_raw(
                name,
                (&mut attrs as *mut SECURITY_ATTRIBUTES).cast(),
            )
    };
    unsafe {
        LocalFree(descriptor);
    }
    result.map_err(|_| "pipe_unavailable")
}
async fn read(stream: &mut NamedPipeServer, hello: bool) -> Result<Request> {
    let first = tokio::time::timeout(
        Duration::from_secs(if hello { 1 } else { 15 }),
        stream.read_u8(),
    )
    .await
    .map_err(|_| "pipe_timeout")?
    .map_err(|_| "pipe_closed")?;
    let bytes = tokio::time::timeout(Duration::from_secs(1), async {
        let mut header = [first, 0, 0, 0];
        stream
            .read_exact(&mut header[1..])
            .await
            .map_err(|_| "pipe_closed")?;
        let length = u32::from_le_bytes(header) as usize;
        if length == 0 || length > protocol::REQUEST_MAX {
            return Err("frame_size");
        }
        let mut bytes = vec![0; length];
        stream
            .read_exact(&mut bytes)
            .await
            .map_err(|_| "pipe_closed")?;
        Ok(bytes)
    })
    .await
    .map_err(|_| "pipe_timeout")??;
    protocol::decode(&bytes)
}
async fn write(stream: &mut NamedPipeServer, value: &Value) -> Result<()> {
    let bytes = serde_json::to_vec(value).map_err(|_| "invalid_response")?;
    if bytes.len() > protocol::RESPONSE_MAX {
        return Err("frame_size");
    }
    tokio::time::timeout(Duration::from_secs(1), async {
        stream.write_u32_le(bytes.len() as u32).await?;
        stream.write_all(&bytes).await
    })
    .await
    .map_err(|_| "pipe_timeout")?
    .map_err(|_| "pipe_closed")
}
async fn connected(mut stream: NamedPipeServer, runtime: Arc<Runtime>) {
    let id = identity::random_id();
    let run = async {
        let mut pid = 0;
        if unsafe { GetNamedPipeClientProcessId(stream.as_raw_handle(), &mut pid) } == 0 {
            return Err("identity_mismatch");
        }
        let peer = identity::ProcessIdentity::open(pid)?;
        if identity::credentials(pid)? != identity::credentials(unsafe { GetCurrentProcessId() })? {
            return Err("identity_mismatch");
        }
        let Request::Hello {
            protocol,
            profile_key,
            role,
            build,
        } = read(&mut stream, true).await?
        else {
            return Err("hello_required");
        };
        if protocol != protocol::VERSION || build != protocol::BUILD {
            return Err("protocol_mismatch");
        }
        if profile_key != runtime.sampler.profile {
            return Err("profile_mismatch");
        }
        if role == Role::Cli
            && peer.file
                != identity::file_identity(
                    &std::env::current_exe().map_err(|_| "native_missing")?,
                    false,
                )?
        {
            return Err("identity_mismatch");
        }
        write(&mut stream, &runtime.status()).await?;
        loop {
            let request = read(&mut stream, false).await?;
            peer.verify()?;
            let value = runtime.dispatch(&id, &role, request);
            write(&mut stream, &value).await?;
            if runtime.stop.load(Ordering::Relaxed) {
                break;
            }
        }
        Ok::<(), &'static str>(())
    }
    .await;
    if let Err(reason) = run {
        let _ = write(&mut stream, &protocol::error(reason)).await;
    }
    runtime.disconnect(&id);
}
pub async fn serve(profile: String) -> Result<()> {
    let name = identity::pipe_name(&profile);
    let mut listener = create(&name, true)?;
    let runtime = Arc::new(Runtime::new(profile));
    runtime.sampler.start();
    let permits = Arc::new(Semaphore::new(4));
    loop {
        if runtime.stop.load(Ordering::Relaxed) {
            break;
        }
        let permit = tokio::select! {p=permits.clone().acquire_owned()=>p.map_err(|_|"pipe_closed")?,_=runtime.wake.notified()=>break};
        tokio::select! {r=listener.connect()=>r.map_err(|_|"pipe_closed")?,_=runtime.wake.notified()=>break};
        let next = create(&name, false)?;
        let connection = std::mem::replace(&mut listener, next);
        let runtime = runtime.clone();
        tokio::spawn(async move {
            connected(connection, runtime).await;
            drop(permit);
        });
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn registration_cas_idempotence_and_pending_sample_use_real_pair() {
        let runtime = Runtime::new("synthetic_profile".into());
        let empty = crate::registrations::Observations::default();
        let mut changed = empty.clone();
        changed.revision = 1;
        let request = |base_revision, base_hash: String| Request::SyncRegistrations {
            base_revision,
            base_hash,
            config_revision: 1,
            config_hash: changed.hash(),
            observations: changed.clone(),
        };
        let ack = runtime.dispatch("a", &Role::Python, request(0, empty.hash()));
        assert_eq!(ack["config_hash"], changed.hash());
        assert_eq!(
            runtime.dispatch("b", &Role::Python, request(0, empty.hash())),
            ack
        );
        let mut later = changed.clone();
        later.revision = 2;
        let conflict = runtime.dispatch(
            "b",
            &Role::Python,
            Request::SyncRegistrations {
                base_revision: 0,
                base_hash: empty.hash(),
                config_revision: 2,
                config_hash: later.hash(),
                observations: later,
            },
        );
        assert_eq!(conflict["reason"], "config_conflict");
        assert_eq!(runtime.status()["config_revision"], 1);
        let value = runtime.dispatch("a", &Role::Python, Request::GetSnapshot {});
        assert_eq!(value["config_hash"], changed.hash());
        assert_eq!(value["protocol"], 2);
        assert_eq!(value["instances"], json!([]));
        assert!(value["registrations"].as_array().unwrap().is_empty());
        assert_eq!(
            runtime.dispatch("a", &Role::Cli, request(1, changed.hash()))["reason"],
            "role_rejected"
        );
    }
    #[test]
    fn invalid_registration_hash_never_commits_or_authorizes_old_actions() {
        let runtime = Runtime::new("synthetic_profile".into());
        let empty = crate::registrations::Observations::default();
        assert_eq!(
            runtime.dispatch(
                "a",
                &Role::Python,
                Request::SyncRegistrations {
                    base_revision: 0,
                    base_hash: empty.hash(),
                    config_revision: 1,
                    config_hash: "wrong".into(),
                    observations: empty.clone()
                }
            )["reason"],
            "invalid_config"
        );
        assert_eq!(runtime.status()["config_revision"], 0);
        assert_eq!(
            runtime.dispatch(
                "a",
                &Role::Python,
                Request::ListWindowChoices {
                    registration_id: "unknown".into(),
                    config_revision: 1,
                    config_hash: empty.hash()
                }
            )["reason"],
            "config_pending"
        );
        assert_eq!(
            runtime.dispatch(
                "a",
                &Role::Python,
                Request::ReturnWindow {
                    action_id: "unknown".into(),
                    generation: runtime.sampler.generation.clone(),
                    seq: 1,
                    config_revision: 1,
                    config_hash: empty.hash()
                }
            )["reason"],
            "config_pending"
        );
    }
    fn begin(base: u64) -> Request {
        Request::SyncScopeBegin {
            transaction_id: "transaction".into(),
            base_revision: base,
            scope_hash: scope_digest(&[ScopeRoot {
                directory_id: "directory".into(),
                path: "C:/example/fixture".into(),
            }]),
        }
    }
    #[test]
    fn connection_owned_scope_is_atomic() {
        let runtime = Runtime::new("synthetic_profile".into());
        assert_eq!(
            runtime.dispatch("a", &Role::Python, begin(0))["status"],
            "available"
        );
        assert_eq!(
            runtime.dispatch("b", &Role::Python, begin(0))["reason"],
            "scope_busy"
        );
        let chunk = Request::SyncScopeChunk {
            transaction_id: "transaction".into(),
            index: 0,
            roots: vec![ScopeRoot {
                directory_id: "directory".into(),
                path: "C:/example/fixture".into(),
            }],
        };
        assert_eq!(
            runtime.dispatch("a", &Role::Python, chunk)["status"],
            "available"
        );
        assert_eq!(runtime.sampler.scope.lock().unwrap().revision, 0);
        let commit = Request::SyncScopeCommit {
            transaction_id: "transaction".into(),
        };
        assert_eq!(
            runtime.dispatch(
                "b",
                &Role::Python,
                Request::SyncScopeCommit {
                    transaction_id: "transaction".into()
                }
            )["reason"],
            "scope_conflict"
        );
        assert_eq!(
            runtime.dispatch("a", &Role::Python, commit)["scope_revision"],
            1
        );
        let pending = runtime.dispatch("a", &Role::Python, Request::GetSnapshot {});
        assert_eq!(pending["reason"], "scope_pending");
        assert_eq!(pending["scope_revision"], 1);
        assert_eq!(pending["instances"], json!([]));
    }
    #[test]
    fn malformed_or_disconnected_stage_is_released() {
        let runtime = Runtime::new("synthetic_profile".into());
        runtime.dispatch("a", &Role::Python, begin(0));
        let wrong = Request::SyncScopeChunk {
            transaction_id: "transaction".into(),
            index: 8,
            roots: vec![],
        };
        assert_eq!(
            runtime.dispatch("a", &Role::Python, wrong)["reason"],
            "scope_conflict"
        );
        assert!(runtime.stage.lock().unwrap().is_none());
        runtime.dispatch("a", &Role::Python, begin(0));
        runtime.disconnect("a");
        assert!(runtime.stage.lock().unwrap().is_none());
    }
    #[test]
    fn stale_and_unauthorized_control_fail_closed() {
        let runtime = Runtime::new("synthetic_profile".into());
        assert_eq!(
            runtime.dispatch("a", &Role::Python, Request::Stop {})["reason"],
            "role_rejected"
        );
        assert_eq!(
            runtime.dispatch(
                "a",
                &Role::Python,
                Request::ReturnWindow {
                    action_id: "arbitrary".into(),
                    generation: "other".into(),
                    seq: 1,
                    config_revision: 0,
                    config_hash: crate::registrations::Observations::default().hash()
                }
            )["reason"],
            "action_expired"
        );
        assert_eq!(
            runtime.dispatch("a", &Role::Python, begin(1))["reason"],
            "scope_conflict"
        );
    }
}
