//! Compiles the vendored E2B envd protos. protox (a protobuf compiler in Rust) produces the descriptor set, so the
//! build needs no `protoc` on PATH; connectrpc-build then generates buffa messages and the service traits.

use std::path::PathBuf;

use prost::Message;

fn main() {
    let out = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));
    let files = ["process/process.proto", "filesystem/filesystem.proto"];
    for f in files {
        println!("cargo:rerun-if-changed=proto/{f}");
    }
    let fds = protox::compile(files, ["proto"]).expect("compile protos");
    let set = out.join("envd_descriptor.bin");
    std::fs::write(&set, fds.encode_to_vec()).expect("write descriptor set");
    connectrpc_build::Config::new()
        .files(&files)
        .descriptor_set(&set)
        .include_file("_connectrpc.rs")
        .compile()
        .expect("generate connect code");
}
