#![cfg_attr(not(windows), allow(dead_code, unused_imports))]

#[cfg(not(windows))]
compile_error!("pdf-worker-guard supports Windows only");

use std::collections::{HashMap, HashSet};
use std::env;
use std::ffi::{OsStr, OsString, c_void};
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::thread;

type Handle = *mut c_void;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
const JOB_OBJECT_LIMIT_ACTIVE_PROCESS: u32 = 0x0000_0008;
const JOB_OBJECT_LIMIT_PROCESS_MEMORY: u32 = 0x0000_0100;
const JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: u32 = 0x0000_0400;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

#[repr(C)]
#[allow(non_snake_case)]
struct IoCounters {
    ReadOperationCount: u64,
    WriteOperationCount: u64,
    OtherOperationCount: u64,
    ReadTransferCount: u64,
    WriteTransferCount: u64,
    OtherTransferCount: u64,
}

#[repr(C)]
#[allow(non_snake_case)]
struct BasicLimitInformation {
    PerProcessUserTimeLimit: i64,
    PerJobUserTimeLimit: i64,
    LimitFlags: u32,
    MinimumWorkingSetSize: usize,
    MaximumWorkingSetSize: usize,
    ActiveProcessLimit: u32,
    Affinity: usize,
    PriorityClass: u32,
    SchedulingClass: u32,
}

#[repr(C)]
#[allow(non_snake_case)]
struct ExtendedLimitInformation {
    BasicLimitInformation: BasicLimitInformation,
    IoInfo: IoCounters,
    ProcessMemoryLimit: usize,
    JobMemoryLimit: usize,
    PeakProcessMemoryUsed: usize,
    PeakJobMemoryUsed: usize,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
    fn SetInformationJobObject(
        job: Handle,
        information_class: i32,
        information: *const c_void,
        information_length: u32,
    ) -> i32;
    fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
}

const PROTOCOL_VERSION: u32 = 3;
const SECURITY_GUARD_VERSION: u32 = 1;
const BACKEND_NAME: &str = "pdf_fast_worker_backend.exe";
const MUPDF_NAME: &str = "mupdfcpp64.dll";
const DEFAULT_MEMORY_MIB: usize = 1536;
const MIN_MEMORY_MIB: usize = 512;
const MAX_MEMORY_MIB: usize = 4096;
const MAX_PDF_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAX_MANIFEST_BYTES: u64 = 8 * 1024 * 1024;
const MAX_TASKS: usize = 4096;
const MAX_MERGE_SOURCES: usize = 100;
const MAX_MERGE_TOTAL_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const MAX_LINE_BYTES: usize = 1024;
const MAX_COMMAND_LINE_BYTES: usize = 32 * 1024;
const MAX_BACKEND_LINE_BYTES: usize = 64 * 1024;
const MAX_PIXELS_PER_TASK: f64 = 100_000_000.0;
const MAX_PIXELS_PER_BATCH: f64 = 2_000_000_000.0;
const EXPECTED_BACKEND_SHA256: &str = env!("PDF_WORKER_BACKEND_SHA256");
const EXPECTED_MUPDF_SHA256: &str = env!("PDF_WORKER_MUPDF_SHA256");

type GuardResult<T> = Result<T, String>;

#[derive(Debug)]
enum Mode {
    Version,
    Serve {
        input: PathBuf,
        workspace: PathBuf,
        threads: u32,
    },
    RenderBatch {
        input: PathBuf,
        workspace: PathBuf,
        manifest: PathBuf,
        output: PathBuf,
        threads: u32,
    },
    Merge {
        workspace: PathBuf,
        manifest: PathBuf,
        output: PathBuf,
    },
}

#[derive(Debug)]
struct Config {
    mode: Mode,
    backend: PathBuf,
    mupdf: PathBuf,
    memory_mib: usize,
}

struct JobObject {
    handle: Handle,
    memory_bytes: usize,
}

impl JobObject {
    fn create(memory_mib: usize) -> GuardResult<Self> {
        let memory_bytes = memory_mib
            .checked_mul(1024 * 1024)
            .ok_or_else(|| "Worker memory limit overflowed.".to_owned())?;
        // SAFETY: The null security attributes and name request a private job
        // object. The returned handle is checked and owned by this RAII type.
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(format!(
                "Unable to create the Windows worker job: {}",
                io::Error::last_os_error()
            ));
        }
        let job = Self {
            handle,
            memory_bytes,
        };
        job.apply_limits(true)?;
        // Assigning the guard before spawning closes the race where the C++
        // backend could parse an untrusted PDF before entering the job. Child
        // processes inherit the job and a third process exceeds the limit.
        // SAFETY: Both handles are live; GetCurrentProcess returns the valid
        // pseudo-handle for this process.
        if unsafe { AssignProcessToJobObject(job.handle, GetCurrentProcess()) } == 0 {
            return Err(format!(
                "Unable to enter the Windows worker job: {}",
                io::Error::last_os_error()
            ));
        }
        Ok(job)
    }

    fn apply_limits(&self, kill_on_close: bool) -> GuardResult<()> {
        // SAFETY: Zero is the documented initial state for this Win32 data
        // structure and every field enabled by LimitFlags is initialized.
        let mut limits: ExtendedLimitInformation = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | if kill_on_close {
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            } else {
                0
            };
        limits.BasicLimitInformation.ActiveProcessLimit = 2;
        limits.ProcessMemoryLimit = self.memory_bytes;
        // SAFETY: The buffer points to a fully initialized structure and the
        // byte length exactly matches the selected information class.
        let succeeded = unsafe {
            SetInformationJobObject(
                self.handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                &limits as *const _ as *const c_void,
                std::mem::size_of::<ExtendedLimitInformation>() as u32,
            )
        };
        if succeeded == 0 {
            return Err(format!(
                "Unable to set Windows worker limits: {}",
                io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn finish(mut self) -> GuardResult<()> {
        // On a normal exit the backend has already stopped, so disarm
        // KILL_ON_JOB_CLOSE before closing a job that also contains this guard.
        self.apply_limits(false)?;
        // SAFETY: handle is owned and closed exactly once here.
        unsafe { CloseHandle(self.handle) };
        self.handle = std::ptr::null_mut();
        Ok(())
    }
}

impl Drop for JobObject {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: handle is owned and has not been closed. On error or
            // panic, KILL_ON_JOB_CLOSE terminates the inherited backend.
            unsafe { CloseHandle(self.handle) };
        }
    }
}

fn json_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            value if value.is_control() => escaped.push_str(&format!("\\u{:04x}", value as u32)),
            value => escaped.push(value),
        }
    }
    escaped
}

fn emit_protocol_error(message: &str) {
    println!(
        "{{\"type\":\"result\",\"ok\":false,\"completed\":0,\"message\":\"{}\",\"security_guard\":{}}}",
        json_escape(message),
        SECURITY_GUARD_VERSION
    );
}

fn parse_options(
    arguments: &[OsString],
    allowed: &[&str],
) -> GuardResult<HashMap<String, OsString>> {
    if arguments.len() % 2 != 0 {
        return Err("Every worker option must have exactly one value.".to_owned());
    }
    let allowed: HashSet<&str> = allowed.iter().copied().collect();
    let mut options = HashMap::new();
    for pair in arguments.chunks_exact(2) {
        let name = pair[0]
            .to_str()
            .ok_or_else(|| "Worker option names must be UTF-8.".to_owned())?;
        if !allowed.contains(name) {
            return Err(format!("Unsupported worker option: {name}"));
        }
        if options.insert(name.to_owned(), pair[1].clone()).is_some() {
            return Err(format!("Duplicate worker option: {name}"));
        }
    }
    for required in allowed {
        if !options.contains_key(required) {
            return Err(format!("Missing required worker option: {required}"));
        }
    }
    Ok(options)
}

fn parse_threads(value: &OsStr) -> GuardResult<u32> {
    let threads = value
        .to_str()
        .ok_or_else(|| "Thread count must be UTF-8.".to_owned())?
        .parse::<u32>()
        .map_err(|_| "Thread count is not an integer.".to_owned())?;
    if !(1..=12).contains(&threads) {
        return Err("Thread count must be between 1 and 12.".to_owned());
    }
    Ok(threads)
}

fn canonical_pdf(path: &OsStr) -> GuardResult<PathBuf> {
    let path = PathBuf::from(path);
    let metadata =
        fs::metadata(&path).map_err(|error| format!("Unable to inspect input PDF: {error}"))?;
    if !metadata.is_file() {
        return Err("Input PDF is not a regular file.".to_owned());
    }
    if metadata.len() > MAX_PDF_BYTES {
        return Err("Input PDF exceeds the 4 GiB safety limit.".to_owned());
    }
    if path
        .extension()
        .and_then(OsStr::to_str)
        .is_none_or(|extension| !extension.eq_ignore_ascii_case("pdf"))
    {
        return Err("Input file must use the .pdf extension.".to_owned());
    }
    path.canonicalize()
        .map_err(|error| format!("Unable to resolve input PDF: {error}"))
}

fn canonical_directory(path: &OsStr, label: &str) -> GuardResult<PathBuf> {
    let path = PathBuf::from(path);
    let metadata =
        fs::metadata(&path).map_err(|error| format!("Unable to inspect {label}: {error}"))?;
    if !metadata.is_dir() {
        return Err(format!("{label} is not a directory."));
    }
    path.canonicalize()
        .map_err(|error| format!("Unable to resolve {label}: {error}"))
}

fn parse_config() -> GuardResult<Config> {
    let arguments: Vec<OsString> = env::args_os().skip(1).collect();
    let executable =
        env::current_exe().map_err(|error| format!("Unable to locate security guard: {error}"))?;
    let backend = executable
        .parent()
        .ok_or_else(|| "Security guard has no parent directory.".to_owned())?
        .join(BACKEND_NAME)
        .canonicalize()
        .map_err(|error| format!("Unable to locate native backend: {error}"))?;
    let mupdf = executable
        .parent()
        .ok_or_else(|| "Security guard has no parent directory.".to_owned())?
        .join(MUPDF_NAME)
        .canonicalize()
        .map_err(|error| format!("Unable to locate MuPDF runtime: {error}"))?;
    let memory_mib = env::var("PDF_SIZE_REDUCER_WORKER_MEMORY_MIB")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_MEMORY_MIB)
        .clamp(MIN_MEMORY_MIB, MAX_MEMORY_MIB);

    let command = arguments
        .first()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "A worker command is required.".to_owned())?;
    let mode = match command {
        "--version" if arguments.len() == 1 => Mode::Version,
        "serve" => {
            let options = parse_options(&arguments[1..], &["--input", "--threads", "--workspace"])?;
            Mode::Serve {
                input: canonical_pdf(&options["--input"])?,
                workspace: canonical_directory(&options["--workspace"], "worker workspace")?,
                threads: parse_threads(&options["--threads"])?,
            }
        }
        "render-batch" => {
            let options = parse_options(
                &arguments[1..],
                &[
                    "--input",
                    "--manifest",
                    "--output-dir",
                    "--threads",
                    "--workspace",
                ],
            )?;
            Mode::RenderBatch {
                input: canonical_pdf(&options["--input"])?,
                workspace: canonical_directory(&options["--workspace"], "worker workspace")?,
                manifest: PathBuf::from(&options["--manifest"]),
                output: PathBuf::from(&options["--output-dir"]),
                threads: parse_threads(&options["--threads"])?,
            }
        }
        "merge" => {
            let options =
                parse_options(&arguments[1..], &["--manifest", "--output", "--workspace"])?;
            Mode::Merge {
                workspace: canonical_directory(&options["--workspace"], "worker workspace")?,
                manifest: PathBuf::from(&options["--manifest"]),
                output: PathBuf::from(&options["--output"]),
            }
        }
        _ => return Err("Unsupported or malformed worker command.".to_owned()),
    };
    Ok(Config {
        mode,
        backend,
        mupdf,
        memory_mib,
    })
}

fn sha256(input: &[u8]) -> [u8; 32] {
    const INITIAL: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    const ROUND: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    let bit_length = (input.len() as u64).wrapping_mul(8);
    let mut padded = Vec::with_capacity(input.len() + 72);
    padded.extend_from_slice(input);
    padded.push(0x80);
    while padded.len() % 64 != 56 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_length.to_be_bytes());

    let mut state = INITIAL;
    for block in padded.chunks_exact(64) {
        let mut words = [0_u32; 64];
        for (index, bytes) in block.chunks_exact(4).enumerate() {
            words[index] = u32::from_be_bytes(bytes.try_into().expect("four-byte chunk"));
        }
        for index in 16..64 {
            let s0 = words[index - 15].rotate_right(7)
                ^ words[index - 15].rotate_right(18)
                ^ (words[index - 15] >> 3);
            let s1 = words[index - 2].rotate_right(17)
                ^ words[index - 2].rotate_right(19)
                ^ (words[index - 2] >> 10);
            words[index] = words[index - 16]
                .wrapping_add(s0)
                .wrapping_add(words[index - 7])
                .wrapping_add(s1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for index in 0..64 {
            let sum1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choice = (e & f) ^ ((!e) & g);
            let first = h
                .wrapping_add(sum1)
                .wrapping_add(choice)
                .wrapping_add(ROUND[index])
                .wrapping_add(words[index]);
            let sum0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let second = sum0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(first);
            d = c;
            c = b;
            b = a;
            a = first.wrapping_add(second);
        }
        for (slot, value) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *slot = slot.wrapping_add(value);
        }
    }

    let mut output = [0_u8; 32];
    for (chunk, value) in output.chunks_exact_mut(4).zip(state) {
        chunk.copy_from_slice(&value.to_be_bytes());
    }
    output
}

fn hash_file(path: &Path) -> GuardResult<String> {
    let bytes =
        fs::read(path).map_err(|error| format!("Unable to read native component: {error}"))?;
    Ok(sha256(&bytes)
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect())
}

fn verify_native_component(path: &Path, expected: &str, label: &str) -> GuardResult<()> {
    let metadata =
        fs::metadata(path).map_err(|error| format!("Unable to inspect {label}: {error}"))?;
    if !metadata.is_file() {
        return Err(format!("{label} is not a regular file."));
    }
    if metadata.len() > 64 * 1024 * 1024 {
        return Err(format!("{label} exceeds the 64 MiB integrity-check limit."));
    }
    let actual = hash_file(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        return Err(format!("{label} integrity verification failed."));
    }
    Ok(())
}

fn safe_filename(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.ends_with(".jpg")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        && value != "."
        && value != ".."
}

fn safe_pdf_filename(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.to_ascii_lowercase().ends_with(".pdf")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        && value != "."
        && value != ".."
}

fn parse_bounded_u32(value: &str, label: &str, maximum: u32) -> GuardResult<u32> {
    let parsed = value
        .parse::<u32>()
        .map_err(|_| format!("{label} is not a non-negative integer."))?;
    if parsed > maximum {
        return Err(format!("{label} exceeds its safety limit."));
    }
    Ok(parsed)
}

fn parse_coordinate(value: &str) -> GuardResult<f64> {
    let parsed = value
        .parse::<f64>()
        .map_err(|_| "Figure coordinate is not numeric.".to_owned())?;
    if !parsed.is_finite() || parsed.abs() > 1_000_000.0 {
        return Err("Figure coordinate is outside the safety range.".to_owned());
    }
    Ok(parsed)
}

fn validate_manifest(
    manifest: &Path,
    output: &Path,
    workspace: &Path,
    ladder: bool,
) -> GuardResult<(PathBuf, PathBuf)> {
    let manifest = manifest
        .canonicalize()
        .map_err(|error| format!("Unable to resolve render manifest: {error}"))?;
    let output = output
        .canonicalize()
        .map_err(|error| format!("Unable to resolve render output directory: {error}"))?;
    if !manifest.starts_with(workspace) || !output.starts_with(workspace) {
        return Err("Render paths must stay inside the private worker workspace.".to_owned());
    }
    if manifest.parent() != Some(output.as_path()) {
        return Err("Render manifest must be stored directly in its output directory.".to_owned());
    }
    let metadata = fs::metadata(&manifest)
        .map_err(|error| format!("Unable to inspect render manifest: {error}"))?;
    if !metadata.is_file() || metadata.len() > MAX_MANIFEST_BYTES {
        return Err("Render manifest is not a regular file or exceeds 8 MiB.".to_owned());
    }
    let contents = fs::read_to_string(&manifest)
        .map_err(|error| format!("Render manifest is not valid UTF-8: {error}"))?;
    let mut ids = HashSet::new();
    let mut filenames = HashSet::new();
    let mut groups: HashMap<u32, (u32, [u64; 4])> = HashMap::new();
    let mut count = 0_usize;
    let mut total_pixels = 0.0_f64;
    for (line_number, line) in contents.lines().enumerate() {
        count += 1;
        if count > MAX_TASKS {
            return Err(format!("Render manifest exceeds {MAX_TASKS} tasks."));
        }
        if line.is_empty() || line.len() > MAX_LINE_BYTES {
            return Err(format!("Invalid render manifest line {}.", line_number + 1));
        }
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.len() != 10 {
            return Err(format!(
                "Malformed render manifest line {}.",
                line_number + 1
            ));
        }
        let id = parse_bounded_u32(fields[0], "Task id", 1_000_000)?;
        let page = parse_bounded_u32(fields[1], "Page number", 1_000_000)?;
        let coordinates = [
            parse_coordinate(fields[2])?,
            parse_coordinate(fields[3])?,
            parse_coordinate(fields[4])?,
            parse_coordinate(fields[5])?,
        ];
        if coordinates[2] <= coordinates[0] || coordinates[3] <= coordinates[1] {
            return Err("Figure rectangle must have positive area.".to_owned());
        }
        let dpi = parse_bounded_u32(fields[6], "DPI", 1200)?;
        if dpi < 24 {
            return Err("DPI must be between 24 and 1200.".to_owned());
        }
        let quality = parse_bounded_u32(fields[7], "JPEG quality", 100)?;
        if quality < 35 {
            return Err("JPEG quality must be between 35 and 100.".to_owned());
        }
        if !safe_filename(fields[8]) || !filenames.insert(fields[8].to_owned()) {
            return Err("Render filenames must be unique safe JPEG basenames.".to_owned());
        }
        if !ids.insert(id) {
            return Err("Render task ids must be unique.".to_owned());
        }
        let group = parse_bounded_u32(fields[9], "Render group", 1_000_000)?;
        let width = (coordinates[2] - coordinates[0]) * f64::from(dpi) / 72.0;
        let height = (coordinates[3] - coordinates[1]) * f64::from(dpi) / 72.0;
        let task_pixels = width * height;
        if task_pixels > MAX_PIXELS_PER_TASK {
            return Err("A render task exceeds the 100-megapixel safety limit.".to_owned());
        }
        total_pixels += task_pixels;
        if total_pixels > MAX_PIXELS_PER_BATCH {
            return Err("A render batch exceeds the 2-gigapixel safety limit.".to_owned());
        }
        if ladder {
            let region = (page, coordinates.map(f64::to_bits));
            if groups
                .insert(group, region)
                .is_some_and(|previous| previous != region)
            {
                return Err("A ladder group contains different Figure regions.".to_owned());
            }
        }
        let output_file = output.join(fields[8]);
        if fs::symlink_metadata(&output_file).is_ok() {
            return Err("A render output file already exists.".to_owned());
        }
    }
    if count == 0 {
        return Err("Render manifest is empty.".to_owned());
    }
    Ok((manifest, output))
}

fn validate_merge_request(
    manifest: &Path,
    output: &Path,
    workspace: &Path,
) -> GuardResult<(PathBuf, PathBuf)> {
    let manifest = manifest
        .canonicalize()
        .map_err(|error| format!("Unable to resolve merge manifest: {error}"))?;
    if manifest.parent() != Some(workspace) {
        return Err("Merge manifest must stay directly inside the private workspace.".to_owned());
    }
    let metadata = fs::metadata(&manifest)
        .map_err(|error| format!("Unable to inspect merge manifest: {error}"))?;
    if !metadata.is_file() || metadata.len() > MAX_MANIFEST_BYTES {
        return Err("Merge manifest is not a regular file or exceeds 8 MiB.".to_owned());
    }
    let output_name = output
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or_else(|| "Merge output filename must be UTF-8.".to_owned())?;
    if !safe_pdf_filename(output_name) {
        return Err("Merge output must use a safe PDF basename.".to_owned());
    }
    let output_parent = output
        .parent()
        .ok_or_else(|| "Merge output has no parent directory.".to_owned())?
        .canonicalize()
        .map_err(|error| format!("Unable to resolve merge output directory: {error}"))?;
    if output_parent != workspace {
        return Err("Merge output must stay directly inside the private workspace.".to_owned());
    }
    let output = output_parent.join(output_name);
    if fs::symlink_metadata(&output).is_ok() {
        return Err("Merge output already exists.".to_owned());
    }

    let contents = fs::read_to_string(&manifest)
        .map_err(|error| format!("Merge manifest is not valid UTF-8: {error}"))?;
    let mut source_count = 0_usize;
    let mut total_bytes = 0_u64;
    for (line_number, line) in contents.lines().enumerate() {
        source_count += 1;
        if source_count > MAX_MERGE_SOURCES {
            return Err(format!(
                "Merge manifest exceeds {MAX_MERGE_SOURCES} source files."
            ));
        }
        if line.is_empty() || line.len() > MAX_COMMAND_LINE_BYTES || line.contains('\0') {
            return Err(format!("Invalid merge manifest line {}.", line_number + 1));
        }
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.len() != 2 {
            return Err(format!(
                "Malformed merge manifest line {}.",
                line_number + 1
            ));
        }
        let id = parse_bounded_u32(fields[0], "Merge source id", (MAX_MERGE_SOURCES - 1) as u32)?;
        if id as usize != line_number {
            return Err("Merge source ids must be consecutive.".to_owned());
        }
        let source = canonical_pdf(Path::new(fields[1]).as_os_str())?;
        let source_bytes = fs::metadata(&source)
            .map_err(|error| format!("Unable to inspect merge input: {error}"))?
            .len();
        total_bytes = total_bytes
            .checked_add(source_bytes)
            .ok_or_else(|| "Merge input size overflowed.".to_owned())?;
        if total_bytes > MAX_MERGE_TOTAL_BYTES {
            return Err("Merge inputs exceed the 16 GiB aggregate safety limit.".to_owned());
        }
    }
    if source_count < 2 {
        return Err("Native merge requires at least two PDFs.".to_owned());
    }
    Ok((manifest, output))
}

fn validate_server_command(line: &str, workspace: &Path) -> GuardResult<Option<String>> {
    let line = line.trim_end_matches(['\r', '\n']);
    if line == "QUIT" {
        return Ok(Some("QUIT\n".to_owned()));
    }
    if line.len() > MAX_COMMAND_LINE_BYTES || line.contains('\0') {
        return Err("Worker command exceeds the safety limit.".to_owned());
    }
    let fields: Vec<&str> = line.split('\t').collect();
    if fields.len() != 3 || !matches!(fields[0], "BATCH" | "LADDER") {
        return Err("Invalid worker server command.".to_owned());
    }
    let (manifest, output) = validate_manifest(
        Path::new(fields[1]),
        Path::new(fields[2]),
        workspace,
        fields[0] == "LADDER",
    )?;
    Ok(Some(format!(
        "{}\t{}\t{}\n",
        fields[0],
        manifest.display(),
        output.display()
    )))
}

enum BoundedLine {
    End,
    Line(Vec<u8>),
    TooLong,
}

fn read_bounded_line<R: BufRead>(reader: &mut R, maximum: usize) -> io::Result<BoundedLine> {
    let mut line = Vec::with_capacity(maximum.min(4096));
    let mut too_long = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if too_long {
                Ok(BoundedLine::TooLong)
            } else if line.is_empty() {
                Ok(BoundedLine::End)
            } else {
                Ok(BoundedLine::Line(line))
            };
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let consumed = newline.map_or(available.len(), |position| position + 1);
        if !too_long {
            if line.len().saturating_add(consumed) > maximum {
                too_long = true;
                line.clear();
            } else {
                line.extend_from_slice(&available[..consumed]);
            }
        }
        reader.consume(consumed);
        if newline.is_some() {
            return if too_long {
                Ok(BoundedLine::TooLong)
            } else {
                Ok(BoundedLine::Line(line))
            };
        }
    }
}

fn relay_stream<R: Read + Send + 'static>(reader: R, to_stdout: bool) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = BufReader::new(reader);
        loop {
            match read_bounded_line(&mut reader, MAX_BACKEND_LINE_BYTES) {
                Ok(BoundedLine::End) | Err(_) => break,
                Ok(BoundedLine::TooLong) if to_stdout => {
                    emit_protocol_error("Native backend output exceeded 64 KiB.");
                }
                Ok(BoundedLine::TooLong) => {
                    eprintln!("pdf-worker-guard: native backend error output exceeded 64 KiB.");
                }
                Ok(BoundedLine::Line(line)) => {
                    let line = String::from_utf8_lossy(&line);
                    if to_stdout {
                        let mut output = io::stdout().lock();
                        let _ = write!(output, "{line}");
                        if !line.ends_with('\n') {
                            let _ = writeln!(output);
                        }
                        let _ = output.flush();
                    } else {
                        let mut output = io::stderr().lock();
                        let _ = write!(output, "{line}");
                        if !line.ends_with('\n') {
                            let _ = writeln!(output);
                        }
                        let _ = output.flush();
                    }
                }
            }
        }
    })
}

fn wait_for_backend(mut child: Child, job: JobObject) -> GuardResult<i32> {
    let output_thread = relay_stream(
        child
            .stdout
            .take()
            .ok_or_else(|| "Backend stdout is unavailable.".to_owned())?,
        true,
    );
    let error_thread = relay_stream(
        child
            .stderr
            .take()
            .ok_or_else(|| "Backend stderr is unavailable.".to_owned())?,
        false,
    );
    let status = child
        .wait()
        .map_err(|error| format!("Unable to wait for native backend: {error}"))?;
    let _ = output_thread.join();
    let _ = error_thread.join();
    job.finish()?;
    Ok(status.code().unwrap_or(70))
}

fn run_serve(
    backend: &Path,
    input: &Path,
    workspace: PathBuf,
    threads: u32,
    memory_mib: usize,
) -> GuardResult<i32> {
    let job = JobObject::create(memory_mib)?;
    // The C++ CLI expects the command before its options.
    let mut arguments = vec![
        OsString::from("serve"),
        OsString::from("--input"),
        input.as_os_str().to_owned(),
        OsString::from("--threads"),
        OsString::from(threads.to_string()),
    ];
    let mut command = Command::new(backend);
    command
        .args(arguments.drain(..))
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| format!("Unable to start native backend: {error}"))?;
    let mut backend_input = child
        .stdin
        .take()
        .ok_or_else(|| "Backend stdin is unavailable.".to_owned())?;
    thread::spawn(move || {
        let mut input = BufReader::new(io::stdin().lock());
        loop {
            match read_bounded_line(&mut input, MAX_COMMAND_LINE_BYTES) {
                Ok(BoundedLine::End) | Err(_) => break,
                Ok(BoundedLine::TooLong) => {
                    emit_protocol_error("Worker command exceeds the 32 KiB safety limit.");
                }
                Ok(BoundedLine::Line(line)) => match String::from_utf8(line) {
                    Ok(line) => match validate_server_command(&line, &workspace) {
                        Ok(Some(command)) => {
                            if backend_input.write_all(command.as_bytes()).is_err()
                                || backend_input.flush().is_err()
                            {
                                break;
                            }
                            if command == "QUIT\n" {
                                break;
                            }
                        }
                        Ok(None) => {}
                        Err(message) => emit_protocol_error(&message),
                    },
                    Err(_) => emit_protocol_error("Worker commands must be valid UTF-8."),
                },
            }
        }
    });
    wait_for_backend(child, job)
}

fn run_batch(
    backend: &Path,
    input: &Path,
    workspace: &Path,
    manifest: &Path,
    output: &Path,
    threads: u32,
    memory_mib: usize,
) -> GuardResult<i32> {
    let (manifest, output) = validate_manifest(manifest, output, workspace, false)?;
    let job = JobObject::create(memory_mib)?;
    let mut command = Command::new(backend);
    command
        .arg("render-batch")
        .arg("--input")
        .arg(input)
        .arg("--manifest")
        .arg(manifest)
        .arg("--output-dir")
        .arg(output)
        .arg("--threads")
        .arg(threads.to_string())
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command
        .spawn()
        .map_err(|error| format!("Unable to start native backend: {error}"))?;
    wait_for_backend(child, job)
}

fn run_merge(
    backend: &Path,
    workspace: &Path,
    manifest: &Path,
    output: &Path,
    memory_mib: usize,
) -> GuardResult<i32> {
    let (manifest, output) = validate_merge_request(manifest, output, workspace)?;
    let job = JobObject::create(memory_mib)?;
    let mut command = Command::new(backend);
    command
        .arg("merge")
        .arg("--manifest")
        .arg(manifest)
        .arg("--output")
        .arg(&output)
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command
        .spawn()
        .map_err(|error| format!("Unable to start native merge backend: {error}"))?;
    let exit_code = wait_for_backend(child, job)?;
    if exit_code == 0 {
        let metadata = fs::symlink_metadata(&output)
            .map_err(|error| format!("Unable to inspect native merge output: {error}"))?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err("Native merge output is not a regular file.".to_owned());
        }
        if metadata.len() < 5 || metadata.len() > MAX_MERGE_TOTAL_BYTES {
            return Err("Native merge output size is outside the safety range.".to_owned());
        }
    }
    Ok(exit_code)
}

fn run() -> GuardResult<i32> {
    let config = parse_config()?;
    verify_native_component(&config.backend, EXPECTED_BACKEND_SHA256, "Native backend")?;
    verify_native_component(&config.mupdf, EXPECTED_MUPDF_SHA256, "MuPDF runtime")?;
    match config.mode {
        Mode::Version => {
            let output = Command::new(&config.backend)
                .arg("--version")
                .creation_flags(CREATE_NO_WINDOW)
                .output()
                .map_err(|error| format!("Unable to query native backend: {error}"))?;
            let response = String::from_utf8_lossy(&output.stdout);
            if !output.status.success() || !response.contains("\"protocol\":3") {
                return Err("Native backend protocol verification failed.".to_owned());
            }
            println!(
                "{{\"name\":\"pdf_worker_guard\",\"protocol\":{},\"security_guard\":{},\"capabilities\":[\"render\",\"ladder\",\"merge\"],\"backend_sha256\":\"{}\",\"mupdf_sha256\":\"{}\",\"memory_limit_mib\":{},\"active_process_limit\":2}}",
                PROTOCOL_VERSION,
                SECURITY_GUARD_VERSION,
                EXPECTED_BACKEND_SHA256,
                EXPECTED_MUPDF_SHA256,
                config.memory_mib
            );
            Ok(0)
        }
        Mode::Serve {
            input,
            workspace,
            threads,
        } => run_serve(
            &config.backend,
            &input,
            workspace,
            threads,
            config.memory_mib,
        ),
        Mode::RenderBatch {
            input,
            workspace,
            manifest,
            output,
            threads,
        } => run_batch(
            &config.backend,
            &input,
            &workspace,
            &manifest,
            &output,
            threads,
            config.memory_mib,
        ),
        Mode::Merge {
            workspace,
            manifest,
            output,
        } => run_merge(
            &config.backend,
            &workspace,
            &manifest,
            &output,
            config.memory_mib,
        ),
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code.clamp(0, 255) as u8),
        Err(message) => {
            eprintln!("pdf-worker-guard: {message}");
            ExitCode::from(64)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn filenames_are_restricted_to_safe_jpeg_basenames() {
        assert!(safe_filename("figure-0042.jpg"));
        assert!(!safe_filename("../escape.jpg"));
        assert!(!safe_filename("figure.png"));
        assert!(!safe_filename("subdir\\figure.jpg"));
    }

    #[test]
    fn merge_outputs_are_restricted_to_safe_pdf_basenames() {
        assert!(safe_pdf_filename("native-merged-pages.pdf"));
        assert!(safe_pdf_filename("MERGED.PDF"));
        assert!(!safe_pdf_filename("../escape.pdf"));
        assert!(!safe_pdf_filename("merge.jpg"));
        assert!(!safe_pdf_filename("nested\\merge.pdf"));
    }

    #[test]
    fn json_errors_are_escaped() {
        assert_eq!(json_escape("bad\n\"path\""), "bad\\n\\\"path\\\"");
    }

    #[test]
    fn sha256_matches_known_vectors() {
        let encoded = |value: &[u8]| {
            sha256(value)
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        };
        assert_eq!(
            encoded(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            encoded(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn bounded_line_reader_discards_oversized_lines() {
        let mut input = Cursor::new(b"12345\nok\n");
        assert!(matches!(
            read_bounded_line(&mut input, 4).unwrap(),
            BoundedLine::TooLong
        ));
        match read_bounded_line(&mut input, 4).unwrap() {
            BoundedLine::Line(line) => assert_eq!(line, b"ok\n"),
            _ => panic!("expected the line after the oversized record"),
        }
    }
}
