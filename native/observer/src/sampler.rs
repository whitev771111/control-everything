//! One sampler, bounded snapshots; business storage remains in Python.

use crate::{
    discovery::Catalog,
    identity::{self, ProcessIdentity},
    protocol::ScopeRoot,
    registrations::Observations,
};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};
use windows_sys::Win32::System::{Diagnostics::ToolHelp::*, Threading::*};

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
#[derive(Default)]
pub struct Scope {
    pub roots: Vec<ScopeRoot>,
    pub revision: u64,
    pub hash: String,
}
pub struct Sample {
    pub value: Value,
    pub targets: HashMap<String, Observed>,
}
#[derive(Clone)]
pub struct Observed {
    pub process: ProcessIdentity,
    pub provider: String,
    pub kind: String,
    pub directories: Vec<String>,
    pub scope_revision: u64,
    pub window: Option<usize>,
    pub registration_ids: Vec<String>,
}
#[derive(Default)]
pub struct CatalogView {
    pub config_hash: String,
    pub registrations: Vec<crate::discovery::VerifiedRegistration>,
    pub candidates: Vec<Value>,
}
impl Default for Sample {
    fn default() -> Self {
        Self {
            value: json!({"status":"unknown","reason":"sampling_pending"}),
            targets: HashMap::new(),
        }
    }
}
pub struct Sampler {
    pub sample: Arc<Mutex<Sample>>,
    pub scope: Arc<Mutex<Scope>>,
    pub config: Arc<Mutex<Observations>>,
    pub catalog: Arc<Mutex<CatalogView>>,
    pub stop: Arc<AtomicBool>,
    pub generation: String,
    pub profile: String,
}
impl Sampler {
    pub fn new(profile: String, generation: String) -> Self {
        Self {
            sample: Arc::new(Mutex::new(Sample::default())),
            scope: Arc::new(Mutex::new(Scope::default())),
            config: Arc::new(Mutex::new(Observations::default())),
            catalog: Arc::new(Mutex::new(CatalogView::default())),
            stop: Arc::new(AtomicBool::new(false)),
            generation,
            profile,
        }
    }
    pub fn start(&self) {
        let (config, catalog_view) = (self.config.clone(), self.catalog.clone());
        let (sample, scope, stop, generation, profile) = (
            self.sample.clone(),
            self.scope.clone(),
            self.stop.clone(),
            self.generation.clone(),
            self.profile.clone(),
        );
        std::thread::spawn(move || {
            let mut catalog = Catalog::default();
            let mut system = System::new();
            let mut last_discovery = Instant::now() - Duration::from_secs(10);
            let mut targets = HashMap::<String, Observed>::new();
            let mut seq = 0;
            let mut previous = HashSet::new();
            let me = unsafe { GetCurrentProcessId() };
            let user = identity::credentials(me).ok();
            let mut coverage =
                json!({"status":"unknown","scanned":0,"recognized":0,"omitted":0,"denied":0});
            let mut last = Instant::now();
            let mut sampled_config = String::new();
            let mut sampled_scope = u64::MAX;
            while !stop.load(Ordering::Relaxed) {
                let observations = config.lock().unwrap().clone();
                let config_hash = observations.hash();
                catalog.refresh(&observations);
                *catalog_view.lock().unwrap() = CatalogView {
                    config_hash: config_hash.clone(),
                    registrations: catalog.registrations.clone(),
                    candidates: catalog.candidates(),
                };
                let revision = scope.lock().unwrap().revision;
                if last_discovery.elapsed() >= Duration::from_secs(5)
                    || sampled_scope != revision
                    || sampled_config != config_hash
                {
                    let (found, counts) = discover(&catalog, user.as_ref(), &scope);
                    targets = found;
                    coverage = counts;
                    last_discovery = Instant::now();
                    sampled_config = config_hash.clone();
                    sampled_scope = revision;
                }
                targets.retain(|_, target| target.process.verify().is_ok());
                let identities: HashSet<_> = targets.keys().cloned().collect();
                if identities != previous {
                    system = System::new();
                    previous.clear();
                }
                let pids: Vec<_> = targets
                    .values()
                    .map(|target| Pid::from_u32(target.process.pid))
                    .collect();
                system.refresh_processes_specifics(
                    ProcessesToUpdate::Some(&pids),
                    true,
                    ProcessRefreshKind::nothing().with_cpu().with_memory(),
                );
                let interval = last.elapsed().as_millis() as u64;
                last = Instant::now();
                let mut resources = Vec::new();
                let mut instances = Vec::new();
                let mut current = HashSet::new();
                let threads = thread_counts();
                let revision = scope.lock().unwrap().revision;
                for (id, target) in &targets {
                    let process = &target.process;
                    if process.verify().is_err() {
                        continue;
                    }
                    current.insert(id.clone());
                    let metrics = system.process(Pid::from_u32(process.pid));
                    let mut handles = 0;
                    let handles = if unsafe {
                        GetProcessHandleCount(process.handle.raw(), &mut handles)
                    } != 0
                    {
                        Some(handles)
                    } else {
                        None
                    };
                    let thread_count = threads.get(&process.pid).copied();
                    resources.push(json!({"resource_id":id,"scope":"process","cpu_percent_one_core":if previous.contains(id){metrics.map(|m|m.cpu_usage())}else{None},
                        "rss_bytes":metrics.map(|m|m.memory()),"threads":thread_count,"handles":handles,"metrics_interval_ms":interval,"source":"windows_process","sampled_at_ms":now_ms()}));
                    instances.push(json!({"instance_id":id,"provider_id":if target.provider.is_empty(){Value::Null}else{json!(target.provider)},"kind":target.kind,"pid":process.pid,"resource_id":id,
                        "classification":if target.provider.is_empty(){"custom_exe"}else{"provider"},"registration_ids":target.registration_ids,
                        "directory_ids":if target.scope_revision==revision{target.directories.clone()}else{vec![]},"status":"available","reason":Value::Null,"source":"verified_os_process","sampled_at_ms":now_ms(),
                        "session":{"status":"unknown","reason":"no_session_source"},"pending":{"status":"unknown","reason":"no_pending_source"}}));
                }
                previous = current;
                seq += 1;
                let registration_rows: Vec<_> =
                    catalog.registrations.iter().map(|r| r.public()).collect();
                let mut result = json!({"protocol":crate::protocol::VERSION,"profile_key":profile,"generation":generation,"seq":seq,"scope_revision":revision,
                    "config_revision":observations.revision,"config_hash":config_hash,"registrations":registration_rows,
                    "environment_id":"windows:local","sampled_at_ms":now_ms(),"metrics_interval_ms":interval,"status":"available","reason":Value::Null,
                    "coverage":coverage,"installations":catalog.public(),"instances":instances,"resources":resources});
                if serde_json::to_vec(&result).map_or(true, |v| v.len() > 450 * 1024) {
                    result = json!({"status":"limited","reason":"snapshot_budget_exceeded","protocol":crate::protocol::VERSION,"profile_key":profile,"generation":generation,"seq":seq,"scope_revision":revision,"config_revision":observations.revision,"config_hash":config_hash,"registrations":[],"environment_id":"windows:local","sampled_at_ms":now_ms(),"metrics_interval_ms":interval,"coverage":{"status":"limited"},"installations":[],"instances":[],"resources":[]});
                }
                *sample.lock().unwrap() = Sample {
                    value: result,
                    targets: targets.clone(),
                };
                for _ in 0..20 {
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        });
    }
}
fn discover(
    catalog: &Catalog,
    user: Option<&identity::Credentials>,
    scope: &Mutex<Scope>,
) -> (HashMap<String, Observed>, Value) {
    let mut result = HashMap::new();
    let (mut scanned, mut denied, mut omitted) = (0, 0, 0);
    let Ok(snapshot) = identity::owned(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) })
    else {
        return (
            result,
            json!({"status":"unknown","reason":"process_enumeration_failed"}),
        );
    };
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut ok = unsafe { Process32FirstW(snapshot.raw(), &mut entry) };
    let windows = crate::return_window::visible_windows();
    let mut parents = HashMap::new();
    while ok != 0 {
        if scanned >= 4096 {
            omitted += 1;
            break;
        }
        scanned += 1;
        parents.insert(entry.th32ProcessID, entry.th32ParentProcessID);
        let name = String::from_utf16_lossy(
            &entry.szExeFile[..entry.szExeFile.iter().position(|x| *x == 0).unwrap_or(260)],
        )
        .to_lowercase();
        if [
            "node.exe",
            "python.exe",
            "pythonw.exe",
            "codex.exe",
            "claude.exe",
            "hermes.exe",
            "hermes-agent.exe",
            "chatgpt.exe",
        ]
        .contains(&name.as_str())
            || catalog.candidate_name(&name)
        {
            if let Ok(process) = ProcessIdentity::open(entry.th32ProcessID) {
                if identity::credentials(process.pid).ok().as_ref() == user
                    && user.is_some()
                    && let Some((provider, kind)) = catalog.identify(&process).or_else(|| {
                        catalog
                            .custom_match(&process)
                            .then(|| (String::new(), "custom_exe"))
                    })
                    && (provider.is_empty()
                        || ["codex", "claude", "hermes", "pi"].contains(&provider.as_str()))
                {
                    if result.len() == 256 {
                        omitted += 1;
                    } else {
                        let candidates: Vec<_> = windows
                            .iter()
                            .filter(|(_, pid)| *pid == process.pid)
                            .collect();
                        if kind == "desktop" && candidates.is_empty() {
                            omitted += 1;
                        } else {
                            let window = if candidates.len() == 1 {
                                Some(candidates[0].0)
                            } else {
                                None
                            };
                            let needed = !scope.lock().unwrap().roots.is_empty();
                            let raw = if needed {
                                identity::cwd(&process).ok()
                            } else {
                                None
                            };
                            if needed && raw.is_none() {
                                denied += 1;
                            }
                            let scope = scope.lock().unwrap();
                            let directories = raw
                                .as_ref()
                                .map(|text| directory_matches(text, &scope.roots))
                                .unwrap_or_default();
                            result.insert(
                                process.key(),
                                Observed {
                                    registration_ids: catalog
                                        .registration_ids(&process, Some(&provider)),
                                    process,
                                    provider,
                                    kind: kind.into(),
                                    directories,
                                    scope_revision: scope.revision,
                                    window,
                                },
                            );
                        }
                    }
                }
            } else {
                denied += 1;
            }
        }
        ok = unsafe { Process32NextW(snapshot.raw(), &mut entry) };
    }
    // A verified native child replaces its thin node/console-script launcher, not vice versa.
    let wrappers: HashSet<_> = result
        .values()
        .filter_map(|child| {
            let parent_pid = parents.get(&child.process.pid)?;
            let parent = result.values().find(|p| {
                p.process.pid == *parent_pid
                    && p.provider == child.provider
                    && p.process.created <= child.process.created
            })?;
            let name = parent
                .process
                .image
                .file_name()?
                .to_string_lossy()
                .to_lowercase();
            let child_name = child
                .process
                .image
                .file_name()?
                .to_string_lossy()
                .to_lowercase();
            ((name == "node.exe" && child_name == "codex.exe")
                || (["hermes.exe", "hermes-agent.exe"].contains(&name.as_str())
                    && child_name == "python.exe"))
                .then(|| parent.process.key())
        })
        .collect();
    result.retain(|key, _| !wrappers.contains(key));
    let count = result.len();
    (
        result,
        json!({"status":if omitted>0||denied>0{"limited"}else{"complete"},"scanned":scanned,"recognized":count,"omitted":omitted,"denied":denied}),
    )
}
fn thread_counts() -> HashMap<u32, u32> {
    let mut rows = HashMap::new();
    let Ok(snapshot) = identity::owned(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) })
    else {
        return rows;
    };
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut ok = unsafe { Process32FirstW(snapshot.raw(), &mut entry) };
    let mut scanned = 0;
    while ok != 0 && scanned < 4096 {
        rows.insert(entry.th32ProcessID, entry.cntThreads);
        scanned += 1;
        ok = unsafe { Process32NextW(snapshot.raw(), &mut entry) };
    }
    rows
}
fn normalized(path: &str) -> String {
    let value = path.replace('/', "\\").to_lowercase();
    let value = if let Some(rest) = value.strip_prefix("\\\\?\\unc\\") {
        format!("\\\\{rest}")
    } else {
        value.trim_start_matches("\\\\?\\").to_string()
    };
    value.trim_end_matches('\\').to_string()
}
fn directory_matches(raw: &identity::Secret, roots: &[ScopeRoot]) -> Vec<String> {
    if roots.is_empty() {
        return vec![];
    }
    let Ok(mut text) = raw.text() else {
        return vec![];
    };
    let mut cwd = normalized(&text);
    unsafe {
        text.as_bytes_mut().fill(0);
    }
    let mut matches = Vec::new();
    let mut longest = 0;
    for root in roots {
        let mut path = normalized(&root.path);
        if cwd == path || cwd.strip_prefix(&path).is_some_and(|x| x.starts_with('\\')) {
            if path.len() > longest {
                matches.clear();
                longest = path.len();
            }
            if path.len() == longest {
                matches.push(root.directory_id.clone());
            }
        }
        unsafe {
            path.as_bytes_mut().fill(0);
        }
    }
    unsafe {
        cwd.as_bytes_mut().fill(0);
    }
    if matches.len() > 16 { vec![] } else { matches }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn project_matching_uses_longest_component_boundary_and_unc() {
        assert_eq!(
            normalized(r"\\?\UNC\Server\Share\Project\"),
            r"\\server\share\project"
        );
        let roots = vec![
            ScopeRoot {
                directory_id: "parent".into(),
                path: "C:/example/fixture".into(),
            },
            ScopeRoot {
                directory_id: "child".into(),
                path: "C:/example/fixture/child".into(),
            },
        ];
        let secret =
            |text: &str| identity::Secret(text.encode_utf16().flat_map(u16::to_le_bytes).collect());
        assert_eq!(
            directory_matches(&secret("C:/example/fixture/child/work"), &roots),
            vec!["child"]
        );
        assert!(directory_matches(&secret("C:/example/fixture-other"), &roots).is_empty());
    }
}
