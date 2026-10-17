//! Exact Windows identities and bounded, short-lived process metadata.

use sha2::{Digest, Sha256};
use std::{
    ffi::c_void,
    mem::{size_of, zeroed},
    path::{Path, PathBuf},
    sync::Arc,
};
use windows_sys::Win32::{
    Foundation::*,
    Security::*,
    Storage::FileSystem::*,
    System::{
        Diagnostics::Debug::ReadProcessMemory, RemoteDesktop::ProcessIdToSessionId, Threading::*,
    },
};

pub type Result<T> = std::result::Result<T, &'static str>;
pub const UTF16_CAP: usize = 32768;

#[derive(Debug)]
pub struct Handle(pub usize);
impl Handle {
    pub fn raw(&self) -> HANDLE {
        self.0 as HANDLE
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.raw());
        }
    }
}
pub fn owned(raw: HANDLE) -> Result<Handle> {
    if raw.is_null() || raw == INVALID_HANDLE_VALUE {
        Err("permission_denied")
    } else {
        Ok(Handle(raw as usize))
    }
}
pub fn wide(value: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    value.as_ref().encode_wide().chain(Some(0)).collect()
}
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn random_id() -> String {
    use windows_sys::Win32::Security::Cryptography::*;
    let mut data = [0u8; 16];
    // The system-preferred generator does not require an algorithm handle.
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            data.as_mut_ptr(),
            16,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    assert!(status >= 0, "system_random_failed");
    hex(&data)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileIdentity {
    pub volume: u64,
    pub id: [u8; 16],
}
pub fn file_identity(path: &Path, directory: bool) -> Result<FileIdentity> {
    let handle = owned(unsafe {
        CreateFileW(
            wide(path).as_ptr(),
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            if directory {
                FILE_FLAG_BACKUP_SEMANTICS
            } else {
                0
            },
            std::ptr::null_mut(),
        )
    })?;
    file_identity_handle(&handle)
}
fn file_identity_handle(handle: &Handle) -> Result<FileIdentity> {
    let mut info: FILE_ID_INFO = unsafe { zeroed() };
    if unsafe {
        GetFileInformationByHandleEx(
            handle.raw(),
            FileIdInfo,
            (&mut info as *mut FILE_ID_INFO).cast(),
            size_of::<FILE_ID_INFO>() as u32,
        )
    } == 0
    {
        return Err("file_identity_unavailable");
    }
    Ok(FileIdentity {
        volume: info.VolumeSerialNumber,
        id: info.FileId.Identifier,
    })
}
pub fn final_path(path: &Path) -> Result<PathBuf> {
    let handle = owned(unsafe {
        CreateFileW(
            wide(path).as_ptr(),
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            0,
            std::ptr::null_mut(),
        )
    })?;
    let mut buffer = vec![0u16; 32768];
    let length = unsafe {
        GetFinalPathNameByHandleW(handle.raw(), buffer.as_mut_ptr(), buffer.len() as u32, 0)
    } as usize;
    if length == 0 || length >= buffer.len() {
        return Err("image_unavailable");
    }
    Ok(PathBuf::from(
        String::from_utf16(&buffer[..length]).map_err(|_| "image_unavailable")?,
    ))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Credentials {
    pub sid: Vec<u8>,
    pub logon: u64,
    pub session: u32,
}
pub fn credentials(pid: u32) -> Result<Credentials> {
    let process = owned(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })?;
    let mut token = std::ptr::null_mut();
    if unsafe { OpenProcessToken(process.raw(), TOKEN_QUERY, &mut token) } == 0 {
        return Err("permission_denied");
    }
    let token = owned(token)?;
    let mut needed = 0;
    unsafe {
        GetTokenInformation(token.raw(), TokenUser, std::ptr::null_mut(), 0, &mut needed);
    }
    if needed == 0 || needed > 65536 {
        return Err("identity_unavailable");
    }
    let mut storage = vec![0usize; (needed as usize).div_ceil(size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.raw(),
            TokenUser,
            storage.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err("identity_unavailable");
    }
    let user = unsafe { &*storage.as_ptr().cast::<TOKEN_USER>() };
    let length = unsafe { GetLengthSid(user.User.Sid) } as usize;
    if length == 0 || length > 1024 {
        return Err("identity_unavailable");
    }
    let sid = unsafe { std::slice::from_raw_parts(user.User.Sid.cast::<u8>(), length).to_vec() };
    let mut statistics: TOKEN_STATISTICS = unsafe { zeroed() };
    if unsafe {
        GetTokenInformation(
            token.raw(),
            TokenStatistics,
            (&mut statistics as *mut TOKEN_STATISTICS).cast(),
            size_of::<TOKEN_STATISTICS>() as u32,
            &mut needed,
        )
    } == 0
    {
        return Err("identity_unavailable");
    }
    let mut session = 0;
    if unsafe { ProcessIdToSessionId(pid, &mut session) } == 0 {
        return Err("identity_unavailable");
    }
    let logon = (statistics.AuthenticationId.HighPart as u32 as u64) << 32
        | statistics.AuthenticationId.LowPart as u64;
    Ok(Credentials {
        sid,
        logon,
        session,
    })
}
pub fn profile_key(directory: &Path) -> Result<String> {
    let user = credentials(unsafe { GetCurrentProcessId() })?;
    let file = file_identity(directory, true)?;
    let mut hash = Sha256::new();
    hash.update(b"session-panel-observer/profile/v1\0");
    hash.update((user.sid.len() as u32).to_le_bytes());
    hash.update(&user.sid);
    hash.update(file.volume.to_le_bytes());
    hash.update(file.id);
    Ok(hex(&hash.finalize()))
}
pub fn pipe_name(key: &str) -> String {
    format!("\\\\.\\pipe\\session-panel-observer-v1-{key}")
}

#[derive(Clone, Debug)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub created: u64,
    pub image: PathBuf,
    pub file: FileIdentity,
    pub handle: Arc<Handle>,
}
impl ProcessIdentity {
    pub fn open(pid: u32) -> Result<Self> {
        let handle = Arc::new(owned(unsafe {
            OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid)
        })?);
        let mut times: [FILETIME; 4] = unsafe { zeroed() };
        if unsafe {
            GetProcessTimes(
                handle.raw(),
                &mut times[0],
                &mut times[1],
                &mut times[2],
                &mut times[3],
            )
        } == 0
        {
            return Err("identity_unavailable");
        }
        let created = (times[0].dwHighDateTime as u64) << 32 | times[0].dwLowDateTime as u64;
        let mut buffer = vec![0u16; 32768];
        let mut length = buffer.len() as u32;
        if unsafe { QueryFullProcessImageNameW(handle.raw(), 0, buffer.as_mut_ptr(), &mut length) }
            == 0
        {
            return Err("image_unavailable");
        }
        let image = PathBuf::from(
            String::from_utf16(&buffer[..length as usize]).map_err(|_| "image_unavailable")?,
        );
        let file = file_identity(&image, false)?;
        let result = Self {
            pid,
            created,
            image,
            file,
            handle,
        };
        result.verify()?;
        Ok(result)
    }
    pub fn verify(&self) -> Result<()> {
        if unsafe { WaitForSingleObject(self.handle.raw(), 0) } != WAIT_TIMEOUT {
            return Err("instance_changed");
        }
        let mut times: [FILETIME; 4] = unsafe { zeroed() };
        if unsafe {
            GetProcessTimes(
                self.handle.raw(),
                &mut times[0],
                &mut times[1],
                &mut times[2],
                &mut times[3],
            )
        } == 0
        {
            return Err("instance_changed");
        }
        if (times[0].dwHighDateTime as u64) << 32 | times[0].dwLowDateTime as u64 != self.created {
            return Err("instance_changed");
        }
        if file_identity(&self.image, false)? != self.file {
            return Err("instance_changed");
        }
        Ok(())
    }
    pub fn key(&self) -> String {
        let mut hash = Sha256::new();
        hash.update(self.pid.to_le_bytes());
        hash.update(self.created.to_le_bytes());
        hash.update(self.file.volume.to_le_bytes());
        hash.update(self.file.id);
        hex(&hash.finalize())
    }
}

#[repr(C)]
struct Unicode {
    length: u16,
    maximum: u16,
    buffer: usize,
}
#[repr(C)]
struct Basic {
    status: i32,
    peb: usize,
    affinity: usize,
    priority: i32,
    pid: usize,
    parent: usize,
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryInformationProcess(
        handle: HANDLE,
        class: u32,
        data: *mut c_void,
        size: u32,
        returned: *mut u32,
    ) -> i32;
}

fn query(handle: &Handle, class: u32, data: *mut c_void, size: usize) -> Result<usize> {
    let mut returned = 0;
    let status =
        unsafe { NtQueryInformationProcess(handle.raw(), class, data, size as u32, &mut returned) };
    if status < 0 {
        return Err("metadata_unavailable");
    }
    Ok(returned as usize)
}
pub fn command_line(process: &ProcessIdentity) -> Result<Secret> {
    let result = bounded_command_query(|buffer| {
        if buffer.is_empty() {
            let mut required = 0;
            let status = unsafe {
                NtQueryInformationProcess(
                    process.handle.raw(),
                    60,
                    std::ptr::null_mut(),
                    0,
                    &mut required,
                )
            };
            if status as u32 != 0xc0000004 {
                return Err("metadata_unavailable");
            }
            Ok(required as usize)
        } else {
            query(
                &process.handle,
                60,
                buffer.as_mut_ptr().cast(),
                buffer.len(),
            )
        }
    })?;
    process.verify()?;
    Ok(result)
}
fn bounded_command_query(mut call: impl FnMut(&mut [u8]) -> Result<usize>) -> Result<Secret> {
    let required = call(&mut [])?;
    if !(size_of::<Unicode>()..=UTF16_CAP + size_of::<Unicode>()).contains(&required) {
        return Err("metadata_too_large");
    }
    let mut buffer = Secret(vec![0; required]);
    let returned = call(&mut buffer.0)?;
    if returned > buffer.0.len() || returned < size_of::<Unicode>() {
        return Err("metadata_invalid");
    }
    let descriptor = unsafe { std::ptr::read_unaligned(buffer.0.as_ptr().cast::<Unicode>()) };
    let base = buffer.0.as_ptr() as usize;
    let length = checked_length(descriptor.length, descriptor.maximum)?;
    if descriptor.buffer < base + size_of::<Unicode>()
        || !descriptor.buffer.is_multiple_of(2)
        || descriptor
            .buffer
            .checked_add(descriptor.maximum as usize)
            .is_none_or(|end| end > base + returned)
    {
        return Err("metadata_invalid");
    }
    let offset = descriptor.buffer - base;
    let output = Secret(buffer.0[offset..offset + length].to_vec());
    Ok(output)
}
pub fn checked_length(length: u16, maximum: u16) -> Result<usize> {
    if !length.is_multiple_of(2)
        || !maximum.is_multiple_of(2)
        || length > maximum
        || maximum as usize > UTF16_CAP
    {
        Err("metadata_too_large")
    } else {
        Ok(length as usize)
    }
}
fn remote(handle: &Handle, address: usize, size: usize) -> Result<Secret> {
    if address == 0
        || !address.is_multiple_of(2)
        || address.checked_add(size).is_none()
        || size > UTF16_CAP
    {
        return Err("metadata_invalid");
    }
    let mut buffer = Secret(vec![0; size]);
    let mut count = 0;
    if unsafe {
        ReadProcessMemory(
            handle.raw(),
            address as *const c_void,
            buffer.0.as_mut_ptr().cast(),
            size,
            &mut count,
        )
    } == 0
        || count != size
    {
        return Err("permission_denied");
    }
    Ok(buffer)
}
pub fn cwd(process: &ProcessIdentity) -> Result<Secret> {
    let handle =
        owned(unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, process.pid) })?;
    let mut times: [FILETIME; 4] = unsafe { zeroed() };
    if unsafe {
        GetProcessTimes(
            handle.raw(),
            &mut times[0],
            &mut times[1],
            &mut times[2],
            &mut times[3],
        )
    } == 0
        || (times[0].dwHighDateTime as u64) << 32 | times[0].dwLowDateTime as u64 != process.created
    {
        return Err("instance_changed");
    }
    let mut machine = 0;
    let mut native = 0;
    if unsafe { IsWow64Process2(handle.raw(), &mut machine, &mut native) } == 0 || native != 0x8664
    {
        return Err("unsupported_bitness");
    }
    let (peb, width) = if machine == 0 {
        let mut basic: Basic = unsafe { zeroed() };
        query(
            &handle,
            0,
            (&mut basic as *mut Basic).cast(),
            size_of::<Basic>(),
        )?;
        (basic.peb, 8)
    } else if machine == 0x14c {
        let mut peb: usize = 0;
        query(
            &handle,
            26,
            (&mut peb as *mut usize).cast(),
            size_of::<usize>(),
        )?;
        if peb > u32::MAX as usize {
            return Err("metadata_invalid");
        }
        (peb, 4)
    } else {
        return Err("unsupported_bitness");
    };
    let value = read_cwd(peb, width, |address, length| {
        remote(&handle, address, length)
    })?;
    process.verify()?;
    Ok(value)
}
fn read_cwd(
    peb: usize,
    width: usize,
    mut read: impl FnMut(usize, usize) -> Result<Secret>,
) -> Result<Secret> {
    let (pointer_offset, cwd_offset) = match width {
        8 => (0x20, 0x38),
        4 => (0x10, 0x24),
        _ => return Err("unsupported_bitness"),
    };
    if peb == 0 || !peb.is_multiple_of(width) || width == 4 && peb > u32::MAX as usize {
        return Err("metadata_invalid");
    }
    let pointer = read(
        peb.checked_add(pointer_offset).ok_or("metadata_invalid")?,
        width,
    )?;
    if pointer.0.len() != width {
        return Err("metadata_invalid");
    }
    let params = if width == 8 {
        u64::from_le_bytes(pointer.0[..8].try_into().unwrap()) as usize
    } else {
        u32::from_le_bytes(pointer.0[..4].try_into().unwrap()) as usize
    };
    if params == 0 || !params.is_multiple_of(width) {
        return Err("metadata_invalid");
    }
    let address = params.checked_add(cwd_offset).ok_or("metadata_invalid")?;
    let descriptor = read(address, if width == 8 { 16 } else { 8 })?;
    if descriptor.0.len() != if width == 8 { 16 } else { 8 } {
        return Err("metadata_invalid");
    }
    let length = checked_length(
        u16::from_le_bytes(descriptor.0[..2].try_into().unwrap()),
        u16::from_le_bytes(descriptor.0[2..4].try_into().unwrap()),
    )?;
    let text_address = if width == 8 {
        u64::from_le_bytes(descriptor.0[8..16].try_into().unwrap()) as usize
    } else {
        u32::from_le_bytes(descriptor.0[4..8].try_into().unwrap()) as usize
    };
    if text_address == 0
        || !text_address.is_multiple_of(2)
        || text_address.checked_add(length).is_none()
        || width == 4
            && text_address
                .checked_add(length)
                .is_none_or(|end| end > u32::MAX as usize)
    {
        return Err("metadata_invalid");
    }
    let value = read(text_address, length)?;
    if value.0.len() != length {
        return Err("metadata_invalid");
    }
    if descriptor.0 != read(address, descriptor.0.len())?.0 {
        return Err("metadata_changed");
    }
    Ok(value)
}
pub struct Secret(pub Vec<u8>);
impl Secret {
    pub fn text(&self) -> Result<String> {
        let mut units: Vec<u16> = self
            .0
            .chunks_exact(2)
            .map(|x| u16::from_le_bytes([x[0], x[1]]))
            .collect();
        let decoded = String::from_utf16(&units);
        units.fill(0);
        let mut value = decoded.map_err(|_| "metadata_invalid")?;
        if value.contains('\0') {
            unsafe {
                value.as_bytes_mut().fill(0);
            }
            return Err("metadata_invalid");
        }
        Ok(value)
    }
}
impl Drop for Secret {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cwd_layouts_are_bounded_before_text_read() {
        for width in [4, 8] {
            let (pointer_offset, cwd_offset) = if width == 8 {
                (0x20, 0x38)
            } else {
                (0x10, 0x24)
            };
            let mut calls = Vec::new();
            let result = read_cwd(0x1000, width, |address, length| {
                calls.push((address, length));
                let data = match address {
                    a if a == 0x1000 + pointer_offset => 0x2000u64.to_le_bytes()[..width].to_vec(),
                    a if a == 0x2000 + cwd_offset => {
                        let mut v = vec![0; if width == 8 { 16 } else { 8 }];
                        v[..2].copy_from_slice(&4u16.to_le_bytes());
                        v[2..4].copy_from_slice(&4u16.to_le_bytes());
                        let start = if width == 8 { 8 } else { 4 };
                        v[start..].copy_from_slice(&0x3000u64.to_le_bytes()[..width]);
                        v
                    }
                    0x3000 => vec![65, 0, 66, 0],
                    _ => panic!("unexpected remote read"),
                };
                Ok(Secret(data))
            })
            .unwrap();
            assert_eq!(result.text().unwrap(), "AB");
            assert_eq!(calls.len(), 4);
            let mut calls = 0;
            assert!(
                read_cwd(0x1000, width, |_, _| {
                    calls += 1;
                    Ok(Secret(vec![]))
                })
                .is_err()
            );
            assert_eq!(calls, 1);
        }
    }
    #[test]
    fn utf16_budget_precedes_read() {
        assert_eq!(checked_length(32768, 32768).unwrap(), 32768);
        assert!(checked_length(32769, 32769).is_err());
        assert!(checked_length(2, 65534).is_err());
        assert!(checked_length(4, 2).is_err());
    }
    #[test]
    fn oversized_command_never_reads_content() {
        let mut calls = 0;
        assert!(
            bounded_command_query(|buffer| {
                calls += 1;
                assert!(buffer.is_empty());
                Ok(UTF16_CAP + size_of::<Unicode>() + 1)
            })
            .is_err()
        );
        assert_eq!(calls, 1);
    }
    #[test]
    fn command_growth_is_not_retried() {
        let mut calls = 0;
        assert!(
            bounded_command_query(|buffer| {
                calls += 1;
                Ok(if buffer.is_empty() { 32 } else { 33 })
            })
            .is_err()
        );
        assert_eq!(calls, 2);
    }
    #[test]
    fn invalid_unicode_never_escapes() {
        assert!(Secret(vec![0x00, 0xd8]).text().is_err());
        assert!(Secret(vec![0, 0]).text().is_err());
    }
    #[test]
    fn owned_process_identity_and_metadata() {
        let process = ProcessIdentity::open(std::process::id()).unwrap();
        assert_eq!(process.key().len(), 64);
        process.verify().unwrap();
        let command = command_line(&process).unwrap();
        assert!(!command.text().unwrap().is_empty());
        let directory = cwd(&process).unwrap();
        assert!(!directory.text().unwrap().is_empty());
        let credentials = credentials(process.pid).unwrap();
        assert!(!credentials.sid.is_empty());
    }
    #[test]
    fn profile_identity_shared_vector() {
        let mut hash = Sha256::new();
        hash.update(b"session-panel-observer/profile/v1\0");
        hash.update(12u32.to_le_bytes());
        hash.update([1, 1, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0]);
        hash.update(0x1122334455667788u64.to_le_bytes());
        hash.update((0..16).collect::<Vec<u8>>());
        assert_eq!(
            hex(&hash.finalize()),
            "74a358cb764c8e9afee9ca52562881972fc816ad20e2708f120f37c08f446aed"
        );
    }
}
