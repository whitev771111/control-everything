//! Public installation entry points, never execution or account discovery.

use crate::identity::{self, ProcessIdentity};
use crate::registrations::Observations;
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use windows_sys::Win32::Foundation::LocalFree;

#[derive(Clone)]
pub struct Installation {
    pub provider: String,
    pub command: String,
    pub entry: PathBuf,
    pub images: Vec<PathBuf>,
    pub version: Option<String>,
}
#[derive(Default)]
pub struct Catalog {
    pub entries: Vec<Installation>,
    environment: String,
    refreshed: Option<Instant>,
    desktop_installed: bool,
    registration_hash: String,
    pub registrations: Vec<VerifiedRegistration>,
    candidate_rows: Vec<Value>,
}
#[derive(Clone)]
pub struct VerifiedRegistration {
    pub id: String,
    pub install: Option<Installation>,
    pub images: Vec<PathBuf>,
    pub proof: Vec<(PathBuf, identity::FileIdentity)>,
    pub status: &'static str,
    pub reason: Option<&'static str>,
    pub duplicate_of: Option<String>,
}
impl VerifiedRegistration {
    pub fn valid(&self) -> bool {
        self.status == "available"
            && self
                .proof
                .iter()
                .all(|(p, id)| identity::file_identity(p, false).is_ok_and(|v| v == *id))
    }
    pub fn public(&self) -> Value {
        json!({"registration_id":self.id,"status":self.status,"reason":self.reason,"source":"registration_verification","sampled_at_ms":crate::sampler::now_ms(),"duplicate_of_id":self.duplicate_of})
    }
}

pub fn path_commands(name: &str) -> Vec<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    let extensions = std::env::var("PATHEXT").unwrap_or_else(|_| ".EXE;.CMD;.BAT".into());
    let suffixes: Vec<_> = extensions
        .split(';')
        .filter(|x| x.starts_with('.') && x.len() <= 8)
        .collect();
    let mut result = Vec::new();
    for directory in std::env::split_paths(&path).take(256) {
        if !directory.is_absolute() {
            continue;
        }
        for suffix in &suffixes {
            let candidate = directory.join(format!("{name}{suffix}"));
            if candidate.is_file() {
                result.push(candidate);
                if result.len() == 64 {
                    return result;
                }
            }
        }
    }
    result
}
fn read_public(path: &Path) -> Option<String> {
    use std::io::Read;
    let file = std::fs::File::open(path).ok()?;
    if file.metadata().ok()?.len() > 256 * 1024 {
        return None;
    }
    let mut text = String::new();
    file.take(256 * 1024 + 1).read_to_string(&mut text).ok()?;
    (text.len() <= 256 * 1024).then_some(text)
}
fn public_version(value: &str) -> Option<String> {
    (!value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._+-".contains(&b)))
    .then(|| value.to_owned())
}
fn registered_desktop_images() -> Vec<PathBuf> {
    use windows_sys::Win32::Storage::Packaging::Appx::*;
    let family = identity::wide("OpenAI.Codex_2p2nqsd0c76g0");
    let (mut count, mut length) = (0, 0);
    unsafe {
        GetPackagesByPackageFamily(
            family.as_ptr(),
            &mut count,
            std::ptr::null_mut(),
            &mut length,
            std::ptr::null_mut(),
        );
    }
    if count == 0 || count > 32 || length > 16384 {
        return vec![];
    }
    let mut names = vec![std::ptr::null_mut(); count as usize];
    let mut buffer = vec![0u16; length as usize];
    if unsafe {
        GetPackagesByPackageFamily(
            family.as_ptr(),
            &mut count,
            names.as_mut_ptr(),
            &mut length,
            buffer.as_mut_ptr(),
        )
    } != 0
    {
        return vec![];
    }
    names
        .into_iter()
        .take(count as usize)
        .filter_map(|name| {
            let mut path = vec![0u16; 32768];
            let mut length = path.len() as u32;
            if unsafe { GetPackagePathByFullName(name, &mut length, path.as_mut_ptr()) } != 0 {
                return None;
            }
            let root = PathBuf::from(String::from_utf16_lossy(
                &path[..length.saturating_sub(1) as usize],
            ));
            (read_public(&root.join("AppxManifest.xml")).is_some_and(|manifest| {
                manifest.contains("CN=50BDFD77-8903-4850-9FFE-6E8522F64D5B")
            }) && pe_x64(&root.join("app/ChatGPT.exe")))
            .then(|| root.join("app/ChatGPT.exe"))
        })
        .collect()
}
fn registered_codex() -> bool {
    !registered_desktop_images().is_empty()
}
fn public_locator(path: &Path) -> Option<String> {
    let text = path.to_str()?;
    let plain = text.strip_prefix(r"\\?\").unwrap_or(text);
    (crate::registrations::local_locator(plain)
        && plain.chars().count() <= 2048
        && !plain.chars().any(char::is_control))
    .then(|| plain.to_owned())
}
fn registered_desktop_path(path: &Path) -> bool {
    identity::file_identity(path, false).is_ok_and(|file| {
        registered_desktop_images()
            .iter()
            .any(|p| identity::file_identity(p, false).is_ok_and(|v| v == file))
    })
}
fn pe_image(path: &Path, x64_only: bool) -> bool {
    use std::io::{Read, Seek, SeekFrom};
    let Ok(mut file) = std::fs::File::open(path) else {
        return false;
    };
    let mut header = [0u8; 64];
    if file.read_exact(&mut header).is_err() || &header[..2] != b"MZ" {
        return false;
    }
    let offset = u32::from_le_bytes(header[60..64].try_into().unwrap());
    if !(64..=1024 * 1024).contains(&offset) || file.seek(SeekFrom::Start(offset as u64)).is_err() {
        return false;
    }
    let mut signature = [0u8; 6];
    file.read_exact(&mut signature).is_ok()
        && &signature[..4] == b"PE\0\0"
        && (signature[4..6] == [0x64, 0x86] || (!x64_only && signature[4..6] == [0x4c, 0x01]))
}
fn pe_x64(path: &Path) -> bool {
    pe_image(path, true)
}
fn npm(command: &Path, provider: &str, package: &str, bin: &str) -> Option<Installation> {
    let directory = command.parent()?;
    let root = directory.join("node_modules").join(package);
    let manifest: Value = serde_json::from_str(&read_public(&root.join("package.json"))?).ok()?;
    if manifest.get("name")?.as_str()? != package {
        return None;
    }
    let target = manifest
        .get("bin")?
        .get(bin)
        .and_then(Value::as_str)
        .or_else(|| manifest.get("bin")?.as_str())?;
    let allowed = match provider {
        "codex" => target == "bin/codex.js",
        "claude" => ["bin/claude.exe", "cli.js"].contains(&target),
        "pi" => target == "dist/bundle/cli.js",
        _ => false,
    };
    if !allowed {
        return None;
    }
    let entry = identity::final_path(&root.join(target)).ok()?;
    let mut images = Vec::new();
    if target.ends_with(".exe") {
        images.push(entry.clone());
    }
    if provider == "codex" {
        // The verified public package locates the platform binary; never glob user directories.
        for base in [
            root.clone(),
            directory.join("node_modules/@openai/codex-win32-x64"),
        ] {
            if base != root
                && !read_public(&base.join("package.json"))
                    .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                    .is_some_and(|manifest| manifest["name"] == "@openai/codex-win32-x64")
            {
                continue;
            }
            let native = base.join("vendor/x86_64-pc-windows-msvc/codex/codex.exe");
            if native.is_file() {
                images.push(identity::final_path(&native).ok()?);
            }
        }
    }
    Some(Installation {
        provider: provider.into(),
        command: bin.into(),
        entry,
        images,
        version: manifest
            .get("version")
            .and_then(Value::as_str)
            .and_then(public_version),
    })
}
fn hermes(command: &Path) -> Option<Installation> {
    let entry = identity::final_path(command).ok()?;
    let site = command.parent()?.parent()?.join("Lib/site-packages");
    for row in std::fs::read_dir(site).ok()?.take(64).flatten() {
        let name = row.file_name().to_string_lossy().to_lowercase();
        if !name.starts_with("hermes_agent-") || !name.ends_with(".dist-info") {
            continue;
        }
        let metadata = read_public(&row.path().join("METADATA"))?;
        if !metadata.lines().any(|x| x == "Name: hermes-agent") {
            continue;
        }
        let points = read_public(&row.path().join("entry_points.txt"))?;
        if points.lines().any(|x| {
            [
                "hermes = hermes_cli.main:main",
                "hermes-agent = agent.legacy_cli:main",
            ]
            .contains(&x.trim())
        }) {
            return Some(Installation {
                provider: "hermes".into(),
                command: "hermes".into(),
                entry: entry.clone(),
                images: vec![entry],
                version: metadata
                    .lines()
                    .find_map(|x| x.strip_prefix("Version: "))
                    .and_then(public_version),
            });
        }
    }
    None
}
impl Catalog {
    pub fn refresh(&mut self, observations: &Observations) {
        let registration_hash = observations.hash();
        let environment = format!(
            "{:?}|{:?}",
            std::env::var_os("PATH"),
            std::env::var_os("PATHEXT")
        );
        if self
            .refreshed
            .is_some_and(|t| t.elapsed() < Duration::from_secs(60))
            && environment == self.environment
            && registration_hash == self.registration_hash
            && self
                .registrations
                .iter()
                .all(|r| r.status != "available" || r.valid())
        {
            return;
        }
        self.entries.clear();
        self.registrations.clear();
        self.candidate_rows.clear();
        self.registration_hash = registration_hash;
        self.environment = environment;
        self.refreshed = Some(Instant::now());
        self.desktop_installed = registered_codex();
        for image in registered_desktop_images() {
            if let Some(path) = public_locator(&image) {
                self.candidate_rows.push(json!({"provider_id":"codex","locator_kind":"codex_desktop","path":path,"version":null}));
            }
        }
        let mut seen = HashSet::new();
        for (provider, command, package) in [
            ("codex", "codex", "@openai/codex"),
            ("claude", "claude", "@anthropic-ai/claude-code"),
            ("pi", "pi", "@earendil-works/pi-coding-agent"),
            ("hermes", "hermes", ""),
            ("grok", "grok", ""),
            ("dsh", "dsh", ""),
            ("dsh", "dsh-tui", ""),
            ("dsh", "dst", ""),
            ("opencode", "opencode", ""),
        ] {
            for candidate in path_commands(command) {
                if self.entries.len() >= 64 {
                    break;
                }
                let found = if !package.is_empty() {
                    npm(&candidate, provider, package, command)
                } else if provider == "hermes" {
                    hermes(&candidate)
                } else {
                    identity::final_path(&candidate)
                        .ok()
                        .map(|entry| Installation {
                            provider: provider.into(),
                            command: command.into(),
                            entry,
                            images: vec![],
                            version: None,
                        })
                };
                if let Some(install) = found
                    && seen.insert((install.provider.clone(), install.entry.clone()))
                {
                    if ["codex", "claude", "pi", "hermes"].contains(&provider) {
                        self.candidate_rows.push(json!({"provider_id":provider,"locator_kind":if provider=="hermes"{"hermes_launcher"}else{"npm_command"},"path":public_locator(&candidate),"version":install.version}));
                    }
                    self.entries.push(install);
                }
            }
        }
        self.apply_registrations(observations, &mut seen);
    }
    fn apply_registrations(
        &mut self,
        observations: &Observations,
        seen: &mut HashSet<(String, PathBuf)>,
    ) {
        self.registrations.clear();
        for row in &observations.registrations {
            let mut verified = VerifiedRegistration {
                id: row.id.clone(),
                install: None,
                images: vec![],
                proof: vec![],
                status: "not_configured",
                reason: Some(if row.enabled {
                    "rebind_required"
                } else {
                    "registration_disabled"
                }),
                duplicate_of: None,
            };
            if row.active() {
                verified.status = "not_found";
                verified.reason = Some("unsupported_installation");
                let selected = Path::new(&row.path);
                let install = match row.locator_kind.as_str() {
                    "npm_command" => {
                        let provider = row.provider_id.as_deref().unwrap_or("");
                        let package = match provider {
                            "codex" => "@openai/codex",
                            "claude" => "@anthropic-ai/claude-code",
                            "pi" => "@earendil-works/pi-coding-agent",
                            _ => "",
                        };
                        npm(
                            selected,
                            provider,
                            package,
                            if provider == "claude" {
                                "claude"
                            } else {
                                provider
                            },
                        )
                    }
                    "hermes_launcher" => hermes(selected),
                    "codex_desktop" => registered_desktop_path(selected).then(|| Installation {
                        provider: "codex".into(),
                        command: "codex-desktop".into(),
                        entry: selected.into(),
                        images: vec![selected.into()],
                        version: None,
                    }),
                    _ => None,
                };
                let custom = row.category == "custom_exe";
                let known_host = selected.file_name().is_some_and(|s| {
                    [
                        "node.exe",
                        "python.exe",
                        "pythonw.exe",
                        "cmd.exe",
                        "powershell.exe",
                        "pwsh.exe",
                    ]
                    .iter()
                    .any(|n| s.eq_ignore_ascii_case(n))
                });
                if custom && known_host {
                    verified.status = "limited";
                    verified.reason = Some("entry_unsupported");
                } else if (custom && pe_image(selected, false)) || install.is_some() {
                    let mut paths = vec![selected.to_path_buf()];
                    if let Some(v) = &install {
                        paths.push(v.entry.clone());
                        paths.extend(v.images.clone());
                    }
                    let proof: Option<Vec<_>> = paths
                        .iter()
                        .map(|p| {
                            identity::file_identity(p, false)
                                .ok()
                                .map(|id| (p.clone(), id))
                        })
                        .collect();
                    if let Some(proof) = proof {
                        verified.proof = proof;
                        verified.images = if custom {
                            identity::final_path(selected).ok().into_iter().collect()
                        } else {
                            install.as_ref().unwrap().images.clone()
                        };
                        let identity_path =
                            install.as_ref().map_or(selected, |i| i.entry.as_path());
                        let identity = identity::file_identity(identity_path, false).ok();
                        if let Some(other) = self.registrations.iter().find(|other| {
                            other.status == "available"
                                && other
                                    .proof
                                    .iter()
                                    .any(|(_, id)| Some(id) == identity.as_ref())
                        }) {
                            verified.status = "limited";
                            verified.reason = Some("duplicate_installation");
                            verified.duplicate_of = Some(other.id.clone());
                        } else {
                            verified.status = "available";
                            verified.reason = None;
                            if let Some(v) = &install
                                && seen.insert((v.provider.clone(), v.entry.clone()))
                            {
                                self.entries.push(v.clone());
                            }
                        }
                        verified.install = install;
                    }
                }
            }
            self.registrations.push(verified);
        }
    }
    pub fn candidates(&self) -> Vec<Value> {
        self.candidate_rows
            .iter()
            .filter(|r| r["path"].as_str().is_some())
            .cloned()
            .collect()
    }
    pub fn candidate_name(&self, name: &str) -> bool {
        self.registrations
            .iter()
            .filter(|r| r.status == "available")
            .flat_map(|r| &r.images)
            .any(|p| p.file_name().is_some_and(|s| s.eq_ignore_ascii_case(name)))
    }
    pub fn registration_ids(
        &self,
        process: &ProcessIdentity,
        provider: Option<&str>,
    ) -> Vec<String> {
        self.registrations
            .iter()
            .filter(|r| {
                r.valid()
                    && (r.images.iter().any(|p| {
                        identity::file_identity(p, false).is_ok_and(|v| v == process.file)
                    }) || r.install.as_ref().is_some_and(|i| {
                        Some(i.provider.as_str()) == provider
                            && installation_script_matches(i, process)
                    }))
            })
            .map(|r| r.id.clone())
            .collect()
    }
    pub fn custom_match(&self, process: &ProcessIdentity) -> bool {
        self.registrations.iter().any(|r| {
            r.install.is_none()
                && r.valid()
                && r.images
                    .iter()
                    .any(|p| identity::file_identity(p, false).is_ok_and(|v| v == process.file))
        })
    }
    pub fn public(&self) -> Vec<Value> {
        ["codex","claude","hermes","pi","grok","dsh","opencode"].iter().map(|provider| {
            let matches: Vec<_> = self.entries.iter().filter(|v| v.provider==*provider).collect();
            let installed=!matches.is_empty()||(*provider=="codex"&&self.desktop_installed);
            let mut commands:Vec<_>=matches.iter().map(|x|x.command.clone()).collect();commands.sort();commands.dedup();
            json!({"provider_id":provider,"status":if installed{"available"}else{"not_found"},
                "reason":if installed{None}else{Some("supported_entry_not_found")},"source":"public_installation",
                "commands":commands,
                "version":matches.first().and_then(|x|x.version.clone())})
        }).collect()
    }
    pub fn identify(&self, process: &ProcessIdentity) -> Option<(String, &'static str)> {
        for install in &self.entries {
            if install
                .images
                .iter()
                .any(|image| identity::file_identity(image, false).is_ok_and(|v| v == process.file))
            {
                return Some((
                    install.provider.clone(),
                    if install.command == "codex-desktop" {
                        "desktop"
                    } else {
                        "cli"
                    },
                ));
            }
        }
        let basename = process.image.file_name()?.to_string_lossy().to_lowercase();
        if !["node.exe", "python.exe", "pythonw.exe"].contains(&basename.as_str()) {
            return desktop_codex(process).then(|| ("codex".into(), "desktop"));
        }
        let raw = identity::command_line(process).ok()?;
        let mut text = raw.text().ok()?;
        // Only the actual script entry, never its arguments, can establish ownership.
        let tokens = windows_arguments(&text);
        let found = script_installation(&basename, &tokens, &self.entries, |script, entry| {
            identity::file_identity(script, false).is_ok_and(|id| {
                identity::file_identity(entry, false).is_ok_and(|expected| expected == id)
            })
        })
        .map(|install| (install.provider.clone(), "cli"));
        // Clear owned raw copies before this candidate scope ends.
        for mut token in tokens {
            unsafe {
                token.as_bytes_mut().fill(0);
            }
        }
        unsafe {
            text.as_bytes_mut().fill(0);
        }
        found
    }
}
fn installation_script_matches(install: &Installation, process: &ProcessIdentity) -> bool {
    let runtime = process
        .image
        .file_name()
        .map(|s| s.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if !["node.exe", "python.exe", "pythonw.exe"].contains(&runtime.as_str()) {
        return false;
    }
    let Ok(raw) = identity::command_line(process) else {
        return false;
    };
    let Ok(mut text) = raw.text() else {
        return false;
    };
    let mut tokens = windows_arguments(&text);
    let found = script_installation(
        &runtime,
        &tokens,
        std::slice::from_ref(install),
        |p, entry| {
            identity::file_identity(p, false)
                .is_ok_and(|v| identity::file_identity(entry, false).is_ok_and(|e| v == e))
        },
    )
    .is_some();
    for token in &mut tokens {
        unsafe {
            token.as_bytes_mut().fill(0);
        }
    }
    unsafe {
        text.as_bytes_mut().fill(0);
    }
    found
}
fn script_installation<'a>(
    runtime: &str,
    tokens: &[String],
    installations: &'a [Installation],
    mut same_file: impl FnMut(&Path, &Path) -> bool,
) -> Option<&'a Installation> {
    // Supported invocation forms are deliberately small; flags/eval/module stay unknown.
    let script = match (runtime, tokens.get(1)?.as_str()) {
        ("node.exe", "--") => tokens.get(2)?,
        ("node.exe" | "python.exe" | "pythonw.exe", value) if !value.starts_with('-') => &tokens[1],
        _ => return None,
    };
    let script = Path::new(script);
    // Relative entries need the target's resolution context, not the daemon's cwd.
    if !script.is_absolute() {
        return None;
    }
    installations.iter().find(|install| {
        let compatible = match runtime {
            "node.exe" => {
                ["codex", "claude", "pi"].contains(&install.provider.as_str())
                    && install
                        .entry
                        .extension()
                        .is_some_and(|extension| extension.eq_ignore_ascii_case("js"))
            }
            "python.exe" | "pythonw.exe" => install.provider == "hermes",
            _ => false,
        };
        compatible && same_file(script, &install.entry)
    })
}
pub fn windows_arguments(command: &str) -> Vec<String> {
    // CommandLineToArgvW has no process side effects; input is already byte-bounded.
    #[link(name = "shell32")]
    unsafe extern "system" {
        fn CommandLineToArgvW(command: *const u16, count: *mut i32) -> *mut *mut u16;
    }
    let mut input = identity::wide(command);
    let mut count = 0;
    let pointer = unsafe { CommandLineToArgvW(input.as_ptr(), &mut count) };
    input.fill(0);
    if pointer.is_null() {
        return vec![];
    }
    let mut result = Vec::new();
    for index in 0..count.max(0) as usize {
        let start = unsafe { *pointer.add(index) };
        let mut length = 0;
        while length < 16384 && unsafe { *start.add(length) } != 0 {
            length += 1;
        }
        let slice = unsafe { std::slice::from_raw_parts_mut(start, length) };
        if let Ok(text) = String::from_utf16(slice) {
            result.push(text);
        }
        slice.fill(0);
    }
    unsafe {
        LocalFree(pointer.cast());
    }
    result
}
fn desktop_codex(process: &ProcessIdentity) -> bool {
    use windows_sys::Win32::Storage::Packaging::Appx::*;
    let mut buffer = vec![0u16; 512];
    let mut length = buffer.len() as u32;
    if unsafe { GetPackageFamilyName(process.handle.raw(), &mut length, buffer.as_mut_ptr()) } != 0
    {
        return false;
    }
    let family = String::from_utf16_lossy(&buffer[..length.saturating_sub(1) as usize]);
    if family != "OpenAI.Codex_2p2nqsd0c76g0" {
        return false;
    }
    let mut full = vec![0u16; 512];
    let mut length = full.len() as u32;
    if unsafe { GetPackageFullName(process.handle.raw(), &mut length, full.as_mut_ptr()) } != 0 {
        return false;
    }
    let mut path = vec![0u16; 32768];
    let mut length = path.len() as u32;
    if unsafe { GetPackagePathByFullName(full.as_ptr(), &mut length, path.as_mut_ptr()) } != 0 {
        return false;
    }
    let root = PathBuf::from(String::from_utf16_lossy(
        &path[..length.saturating_sub(1) as usize],
    ));
    let Some(manifest) = read_public(&root.join("AppxManifest.xml")) else {
        return false;
    };
    manifest.contains("CN=50BDFD77-8903-4850-9FFE-6E8522F64D5B")
        && identity::file_identity(&root.join("app/ChatGPT.exe"), false)
            .is_ok_and(|v| v == process.file)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn registration(
        id: &str,
        path: &Path,
        category: &str,
        provider: Option<&str>,
        kind: &str,
    ) -> crate::registrations::Registration {
        crate::registrations::Registration {
            id: id.into(),
            category: category.into(),
            provider_id: provider.map(str::to_owned),
            environment_id: "windows:local".into(),
            label: "Example tool".into(),
            locator_kind: kind.into(),
            path: path.to_str().unwrap().into(),
            enabled: true,
            binding_state: "confirmed".into(),
        }
    }
    #[test]
    fn custom_registration_uses_exact_identity_and_duplicate_has_no_capability() {
        let process = ProcessIdentity::open(std::process::id()).unwrap();
        let first = registration(
            "00000000000000000000000000000001",
            &process.image,
            "custom_exe",
            None,
            "executable",
        );
        let mut second = first.clone();
        second.id = "00000000000000000000000000000002".into();
        let mut catalog = Catalog::default();
        catalog.apply_registrations(
            &Observations {
                revision: 1,
                registrations: vec![first.clone(), second],
            },
            &mut HashSet::new(),
        );
        assert!(catalog.custom_match(&process));
        assert_eq!(
            catalog.registration_ids(&process, None),
            vec![first.id.clone()]
        );
        assert_eq!(
            catalog.registrations[1].reason,
            Some("duplicate_installation")
        );
        assert!(!catalog.registrations[1].valid());
        let mut inactive = first;
        inactive.binding_state = "rebind_required".into();
        catalog.apply_registrations(
            &Observations {
                revision: 2,
                registrations: vec![inactive],
            },
            &mut HashSet::new(),
        );
        assert!(!catalog.custom_match(&process));
        assert!(catalog.registration_ids(&process, None).is_empty());
    }
    #[test]
    fn registered_non_path_package_uses_public_entry_and_replacement_invalidates_proof() {
        let root =
            std::env::temp_dir().join(format!("observer-registration-{}", identity::random_id()));
        let package = root.join("node_modules/@openai/codex");
        std::fs::create_dir_all(package.join("bin")).unwrap();
        let command = root.join("codex.cmd");
        std::fs::write(&command, "synthetic launcher, never executed").unwrap();
        std::fs::write(
            package.join("bin/codex.js"),
            "synthetic entry, never executed",
        )
        .unwrap();
        std::fs::write(
            package.join("package.json"),
            r#"{"name":"@openai/codex","version":"1.0.0","bin":{"codex":"bin/codex.js"}}"#,
        )
        .unwrap();
        let row = registration(
            "00000000000000000000000000000001",
            &command,
            "builtin",
            Some("codex"),
            "npm_command",
        );
        let mut catalog = Catalog::default();
        catalog.apply_registrations(
            &Observations {
                revision: 1,
                registrations: vec![row],
            },
            &mut HashSet::new(),
        );
        assert_eq!(catalog.entries.len(), 1);
        assert!(catalog.registrations[0].valid());
        let entry = package.join("bin/codex.js");
        std::fs::rename(&entry, package.join("bin/old.js")).unwrap();
        std::fs::write(&entry, "new identity").unwrap();
        assert!(!catalog.registrations[0].valid());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn candidate_paths_are_explicit_local_locators_not_internal_device_paths() {
        assert_eq!(
            public_locator(Path::new(r"\\?\C:\example\tool.exe")),
            Some(r"C:\example\tool.exe".into())
        );
        assert!(public_locator(Path::new(r"\\?\UNC\example.invalid\share\tool.exe")).is_none());
        assert!(public_locator(Path::new(r"\\example.invalid\share\tool.exe")).is_none());
        assert!(public_locator(Path::new("relative.exe")).is_none());
    }
    fn entry(provider: &str, path: &str) -> Installation {
        Installation {
            provider: provider.into(),
            command: provider.into(),
            entry: PathBuf::from(path),
            images: vec![],
            version: None,
        }
    }
    fn recognized(
        runtime: &str,
        arguments: &[&str],
        installations: &[Installation],
    ) -> Option<String> {
        let tokens: Vec<_> = std::iter::once(runtime)
            .chain(arguments.iter().copied())
            .map(str::to_owned)
            .collect();
        script_installation(runtime, &tokens, installations, |script, entry| {
            script == entry
        })
        .map(|install| install.provider.clone())
    }
    #[test]
    fn supported_interpreters_recognize_only_direct_program_entries() {
        for (runtime, provider, path) in [
            ("node.exe", "codex", "C:/example/codex/bin/codex.js"),
            ("node.exe", "claude", "C:/example/claude/cli.js"),
            ("node.exe", "pi", "C:/example/pi/dist/bundle/cli.js"),
            ("python.exe", "hermes", "C:/example/venv/Scripts/hermes.exe"),
            (
                "pythonw.exe",
                "hermes",
                "C:/example/venv/Scripts/hermes.exe",
            ),
        ] {
            let installs = [entry(provider, path)];
            assert_eq!(
                recognized(runtime, &[path, "--flag", "argument"], &installs),
                Some(provider.into())
            );
            if runtime == "node.exe" {
                assert_eq!(
                    recognized(runtime, &["--", path, "--flag"], &installs),
                    Some(provider.into())
                );
            }
        }
    }
    #[test]
    fn ordinary_script_arguments_never_claim_a_provider() {
        for (runtime, provider, path) in [
            ("node.exe", "codex", "C:/example/codex/bin/codex.js"),
            ("python.exe", "hermes", "C:/example/venv/Scripts/hermes.exe"),
        ] {
            let installs = [entry(provider, path)];
            for arguments in [
                vec!["C:/example/ordinary.js", path],
                vec!["C:/example/ordinary.js", "--", path],
                vec!["ordinary.js", path],
            ] {
                assert_eq!(recognized(runtime, &arguments, &installs), None);
            }
            if runtime == "node.exe" {
                assert_eq!(
                    recognized(runtime, &["--", "C:/example/ordinary.js", path], &installs),
                    None
                );
            }
        }
    }
    #[test]
    fn unsupported_interpreter_modes_do_not_search_later_arguments() {
        let installs = [
            entry("codex", "C:/example/codex/bin/codex.js"),
            entry("hermes", "C:/example/venv/Scripts/hermes.exe"),
        ];
        for runtime in ["node.exe", "python.exe", "pythonw.exe"] {
            for mode in [
                "-e",
                "--eval",
                "--eval=code",
                "-c",
                "-m",
                "--input-type=module",
                "--import",
                "--require",
                "--inspect",
                "--check",
                "-u",
                "-",
            ] {
                for install in &installs {
                    assert_eq!(
                        recognized(runtime, &[mode, install.entry.to_str().unwrap()], &installs),
                        None
                    );
                }
            }
            assert_eq!(recognized(runtime, &[], &installs), None);
            assert_eq!(recognized(runtime, &["--"], &installs), None);
            assert_eq!(recognized(runtime, &[""], &installs), None);
        }
    }
    #[test]
    fn incompatible_runtimes_and_relative_entries_never_check_file_identity() {
        for (runtime, provider, path) in [
            ("python.exe", "codex", "C:/example/codex/bin/codex.js"),
            ("pythonw.exe", "pi", "C:/example/pi/dist/bundle/cli.js"),
            ("node.exe", "hermes", "C:/example/venv/Scripts/hermes.exe"),
            ("node.exe", "claude", "C:/example/claude/bin/claude.exe"),
            ("node.exe", "grok", "C:/example/grok.js"),
            ("other.exe", "codex", "C:/example/codex/bin/codex.js"),
            ("node.exe", "codex", "codex.js"),
        ] {
            let installs = [entry(provider, path)];
            let tokens = vec![runtime.into(), path.into()];
            assert!(
                script_installation(runtime, &tokens, &installs, |_, _| panic!(
                    "incompatible entry must not perform file I/O"
                ))
                .is_none()
            );
        }
    }
    #[test]
    fn public_versions_and_command_tokens_stay_bounded() {
        assert_eq!(
            public_version("0.0.0-fixture+test"),
            Some("0.0.0-fixture+test".into())
        );
        for value in ["", "unsafe\nversion", &"x".repeat(65)] {
            assert!(public_version(value).is_none());
        }
        assert_eq!(
            windows_arguments(r#"node.exe "C:/example/fixture path/codex.js" --flag"#),
            vec!["node.exe", "C:/example/fixture path/codex.js", "--flag"]
        );
    }
    #[test]
    fn synthetic_public_package_requires_exact_manifest_entry() {
        let root = std::env::temp_dir().join(format!("observer-public-{}", identity::random_id()));
        let package = root.join("node_modules/@openai/codex");
        std::fs::create_dir_all(package.join("bin")).unwrap();
        std::fs::write(package.join("bin/codex.js"), b"// synthetic entry").unwrap();
        let manifest = package.join("package.json");
        std::fs::write(
            &manifest,
            r#"{"name":"@openai/codex","version":"0.0.0-fixture","bin":{"codex":"bin/codex.js"}}"#,
        )
        .unwrap();
        let command = root.join("codex.cmd");
        assert!(npm(&command, "codex", "@openai/codex", "codex").is_some());
        std::fs::write(
            &manifest,
            r#"{"name":"other","bin":{"codex":"bin/codex.js"}}"#,
        )
        .unwrap();
        assert!(npm(&command, "codex", "@openai/codex", "codex").is_none());
        let pe = root.join("wezterm-gui.exe");
        let mut bytes = vec![0u8; 70];
        bytes[..2].copy_from_slice(b"MZ");
        bytes[60..64].copy_from_slice(&64u32.to_le_bytes());
        bytes[64..70].copy_from_slice(b"PE\0\0\x64\x86");
        std::fs::write(&pe, &bytes).unwrap();
        assert!(pe_x64(&pe));
        bytes[68] = 0x4c;
        std::fs::write(&pe, &bytes).unwrap();
        assert!(!pe_x64(&pe));
        std::fs::remove_dir_all(root).unwrap();
    }
}
