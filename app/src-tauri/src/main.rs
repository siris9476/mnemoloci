// Native window for Mnemoloci. The engine (Python) serves the UI and the API on
// 127.0.0.1 with a per-launch token; this shell starts it, opens a WebView2 window
// on the URL it prints, and stops it when the window closes. No IPC is exposed to
// the page: it is a plain web page talking to its own origin.
//
// - Python: MNEMOLOCI_PYTHON, else the one recorded in ~/.mnemoloci/python.txt
//   (written by `mnemoloci init` / `doctor --fix`), else `py -3` then `python` on Windows,
//   `python3` then `python` on Linux.
// - The engine gets --parent-pid: it exits by itself when this process is gone, even
//   after a crash, so no server is left running with its token.
// - When the engine cannot start, the window shows why and how to fix it instead of
//   the app vanishing silently.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Engine(Mutex<Option<Child>>);

fn candidates() -> Vec<(String, Vec<String>)> {
    let mut out = Vec::new();
    if let Ok(python) = std::env::var("MNEMOLOCI_PYTHON") {
        out.push((python, vec![]));
    }
    if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
        let file: PathBuf = [PathBuf::from(home), PathBuf::from(".mnemoloci"), PathBuf::from("python.txt")].iter().collect();
        if let Ok(text) = std::fs::read_to_string(file) {
            let path = text.trim_start_matches('\u{feff}').trim().to_string();
            if !path.is_empty() && std::path::Path::new(&path).is_file() {
                out.push((path, vec![]));
            }
        }
    }
    if cfg!(windows) {
        out.push(("py".into(), vec!["-3".into()]));
        out.push(("python".into(), vec![]));
    } else {
        out.push(("python3".into(), vec![]));
        out.push(("python".into(), vec![]));
    }
    out
}

fn start_with(program: &str, pre: &[String]) -> Result<(Child, String), String> {
    let mut cmd = Command::new(program);
    let pid = std::process::id().to_string();
    cmd.args(pre)
        .args(["-P", "-m", "mnemoloci", "app", "--no-window", "--parent-pid", &pid])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn().map_err(|e| format!("{program}: {e}"))?;
    // stderr is collected on the side: shown only if the engine fails to start
    let mut stderr = child.stderr.take().ok_or("no stderr from engine")?;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stderr.read_to_string(&mut text);
        let _ = tx.send(text);
    });
    let stdout = child.stdout.take().ok_or("no stdout from engine")?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    let _ = reader.read_line(&mut line);
    if let Some(url) = line
        .strip_prefix("MNEMOLOCI_APP_URL ")
        .map(|u| u.trim().to_string())
        .filter(|u| u.starts_with("http://127.0.0.1:"))
    {
        // Keep draining stdout so a later write can never block or break the engine.
        std::thread::spawn(move || for _ in reader.lines() {});
        return Ok((child, url));
    }
    // Not the URL: the engine is reporting an error (a JSON object over several lines) and exiting.
    let (out_tx, out_rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut rest = String::new();
        let _ = reader.read_to_string(&mut rest);
        let _ = out_tx.send(rest);
    });
    let rest = out_rx.recv_timeout(Duration::from_secs(5)).unwrap_or_default();
    let _ = child.kill();
    let _ = child.wait();
    let errors = rx.recv_timeout(Duration::from_secs(5)).unwrap_or_default();
    let lines: Vec<&str> = errors.lines().collect();
    let tail = lines[lines.len().saturating_sub(12)..].join("\n");
    Err(format!("{program}:\n{}{}\n{tail}", line.trim_end(), rest.trim_end()))
}

fn start_engine() -> Result<(Child, String), String> {
    let mut failures = Vec::new();
    for (program, pre) in candidates() {
        match start_with(&program, &pre) {
            Ok(found) => return Ok(found),
            Err(message) => failures.push(message),
        }
    }
    Err(failures.join("\n\n"))
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn error_page(details: &str) -> String {
    let html = format!(
        "<!doctype html><meta charset=utf-8><title>Mnemoloci</title>\
         <body style=\"font-family:Segoe UI,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222\">\
         <h1>The Mnemoloci engine does not start</h1>\
         <p>The app needs a Python with Mnemoloci installed. Open a terminal and run:</p>\
         <pre style=\"background:#f3f3f3;padding:12px\">{py} -P -m mnemoloci doctor --fix</pre>\
         <p>with the Python where you installed Mnemoloci (the one used for <code>pip install -e</code>). \
         The command checks the installation, repairs what it can and tells you what is missing; \
         then reopen the app.</p>\
         <p>If Python is not installed: install Python 3.11 or later from python.org, then \
         <code>{py} -m pip install -e &lt;mnemoloci folder&gt;[mcp,app]</code>.</p>\
         <details open><summary>Technical details</summary><pre style=\"white-space:pre-wrap;font-size:12px\">{}</pre></details>",
        escape(details),
        py = if cfg!(windows) { "python" } else { "python3" }
    );
    let mut encoded = String::from("data:text/html;charset=utf-8,");
    for byte in html.bytes() {
        if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn main() {
    let (child, url) = match start_engine() {
        Ok((child, url)) => (Some(child), url),
        Err(details) => (None, error_page(&details)),
    };
    let app = tauri::Builder::default()
        .manage(Engine(Mutex::new(child)))
        .setup(move |app| {
            let parsed = url.parse().expect("engine URL");
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
                .title("Mnemoloci")
                .inner_size(1280.0, 860.0)
                .min_inner_size(720.0, 480.0)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Mnemoloci");
    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Engine>().0.lock().unwrap().take() {
                let _ = child.kill(); // --parent-pid covers the cases where this never runs
            }
        }
    });
}
