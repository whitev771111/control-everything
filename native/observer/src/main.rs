mod console_proof;
mod discovery;
mod identity;
mod pipe;
mod protocol;
mod registrations;
mod return_window;
mod sampler;

use std::{
    os::windows::{io::AsRawHandle, process::CommandExt},
    path::PathBuf,
    time::{Duration, Instant},
};

fn command_client(profile: &str, method: &str) -> identity::Result<serde_json::Value> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| "runtime_failed")?;
    runtime.block_on(async{
        use tokio::io::{AsyncReadExt,AsyncWriteExt};
        let mut client=tokio::net::windows::named_pipe::ClientOptions::new().open(identity::pipe_name(profile)).map_err(|e|if e.raw_os_error()==Some(2){"pipe_missing"}else{"pipe_unavailable"})?;
        let mut pid=0;
        if unsafe{windows_sys::Win32::System::Pipes::GetNamedPipeServerProcessId(client.as_raw_handle(),&mut pid)}==0{return Err("identity_mismatch");}
        let peer=identity::ProcessIdentity::open(pid)?;
        if peer.file!=identity::file_identity(&std::env::current_exe().map_err(|_|"native_missing")?,false)?||identity::credentials(pid)?!=identity::credentials(std::process::id())?{return Err("identity_mismatch");}
        let hello=serde_json::json!({"method":"hello","protocol":protocol::VERSION,"profile_key":profile,"role":"cli","build":protocol::BUILD});
        let mut result=serde_json::Value::Null;
        for request in [hello,serde_json::json!({"method":method})]{
            result=tokio::time::timeout(Duration::from_secs(2),async{
                let bytes=serde_json::to_vec(&request).map_err(|_|"invalid_message")?;
                client.write_u32_le(bytes.len() as u32).await.map_err(|_|"pipe_closed")?;client.write_all(&bytes).await.map_err(|_|"pipe_closed")?;
                let size=client.read_u32_le().await.map_err(|_|"pipe_closed")? as usize;if size>protocol::RESPONSE_MAX{return Err("frame_size");}
                let mut bytes=vec![0;size];client.read_exact(&mut bytes).await.map_err(|_|"pipe_closed")?;serde_json::from_slice(&bytes).map_err(|_|"invalid_message")
            }).await.map_err(|_|"pipe_timeout")??;
            if result["status"]!="available"{return Err("protocol_mismatch");}
        }Ok(result)
    })
}
fn run() -> identity::Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.first().is_some_and(|x| x == "console-proof") {
        if args.len() != 3 {
            return Err("invalid_arguments");
        }
        return console_proof::run(
            args[1]
                .to_str()
                .and_then(|v| v.parse().ok())
                .ok_or("invalid_arguments")?,
            args[2]
                .to_str()
                .and_then(|v| v.parse().ok())
                .ok_or("invalid_arguments")?,
        );
    }
    if args.len() != 3 || args[1] != "--data-dir" {
        return Err("invalid_arguments");
    }
    let command = args[0].to_str().ok_or("invalid_arguments")?;
    let directory = PathBuf::from(&args[2]);
    let profile = identity::profile_key(&directory)?;
    match command {
        "daemon" => tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| "runtime_failed")?
            .block_on(pipe::serve(profile)),
        "status" | "stop" => {
            let result =
                command_client(&profile, if command == "stop" { "stop" } else { "status" })?;
            println!("{result}");
            Ok(())
        }
        "start" => {
            match command_client(&profile, "status") {
                Ok(_) => return Ok(()),
                Err("pipe_missing") => (),
                Err(reason) => return Err(reason),
            }
            let executable = std::env::current_exe().map_err(|_| "native_missing")?;
            std::process::Command::new(executable)
                .arg("daemon")
                .arg("--data-dir")
                .arg(directory)
                .creation_flags(0x00000008 | 0x00000200)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .map_err(|_| "start_failed")?;
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline {
                if command_client(&profile, "status").is_ok() {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err("start_timeout")
        }
        _ => Err("invalid_arguments"),
    }
}
fn main() {
    if let Err(reason) = run() {
        eprintln!("{reason}");
        std::process::exit(1);
    }
}
