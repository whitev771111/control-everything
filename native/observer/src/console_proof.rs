//! A short-lived helper; console attachment never happens in the daemon.

use crate::identity::{self, ProcessIdentity, Result};
use std::{io::Write, os::windows::io::FromRawHandle};
use windows_sys::Win32::{
    Foundation::*,
    System::{Console::*, Threading::*},
    UI::WindowsAndMessaging::*,
};

pub fn run(pid: u32, created: u64) -> Result<()> {
    let process = ProcessIdentity::open(pid)?;
    if process.created != created {
        return Err("instance_changed");
    }
    // Preserve the inherited pipe independently before AttachConsole resets std handles.
    let mut output = std::ptr::null_mut();
    if unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetStdHandle(STD_OUTPUT_HANDLE),
            GetCurrentProcess(),
            &mut output,
            0,
            0,
            DUPLICATE_SAME_ACCESS,
        )
    } == 0
    {
        return Err("helper_output_failed");
    }
    let mut output = unsafe { std::fs::File::from_raw_handle(output) };
    unsafe {
        FreeConsole();
    }
    if unsafe { AttachConsole(pid) } == 0 {
        return Err("binding_unknown");
    }
    let proof = (|| -> Result<serde_json::Value> {
        let mut members = [0u32; 256];
        let count = unsafe { GetConsoleProcessList(members.as_mut_ptr(), 256) } as usize;
        if count == 0 || count > 256 || !members[..count].contains(&pid) {
            return Err("binding_unknown");
        }
        let hwnd = unsafe { GetConsoleWindow() };
        if hwnd.is_null() || unsafe { IsWindowVisible(hwnd) } == 0 {
            return Err("binding_unknown");
        }
        let mut owner = 0;
        unsafe {
            GetWindowThreadProcessId(hwnd, &mut owner);
        }
        let owner = ProcessIdentity::open(owner)?;
        process.verify()?;
        if identity::credentials(owner.pid)? != identity::credentials(pid)? {
            return Err("binding_unknown");
        }
        Ok(
            serde_json::json!({"hwnd":hwnd as usize,"owner_pid":owner.pid,"owner_created":owner.created}),
        )
    })();
    let detached = unsafe { FreeConsole() };
    if detached == 0 {
        return Err("helper_detach_failed");
    }
    let proof = proof?;
    serde_json::to_writer(&mut output, &proof).map_err(|_| "helper_output_failed")?;
    output.flush().map_err(|_| "helper_output_failed")
}
