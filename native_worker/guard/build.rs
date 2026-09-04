use std::env;

fn required_digest(name: &str) -> String {
    let digest =
        env::var(name).unwrap_or_else(|_| panic!("{name} must be set by native_worker/build.bat"));
    assert!(
        digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "{name} must be a 64-character hexadecimal digest"
    );
    digest
}

fn main() {
    println!("cargo:rerun-if-env-changed=PDF_WORKER_BACKEND_SHA256");
    println!("cargo:rerun-if-env-changed=PDF_WORKER_MUPDF_SHA256");
    let backend = required_digest("PDF_WORKER_BACKEND_SHA256");
    let mupdf = required_digest("PDF_WORKER_MUPDF_SHA256");
    println!("cargo:rustc-env=PDF_WORKER_BACKEND_SHA256={backend}");
    println!("cargo:rustc-env=PDF_WORKER_MUPDF_SHA256={mupdf}");
}
