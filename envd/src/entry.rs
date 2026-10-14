//! `EntryInfo` for a path, and io::Error -> Connect error mapping.

use std::collections::HashMap;
use std::fs::Metadata;
use std::io;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::Path;
use std::sync::Mutex;

use buffa::MessageField;
use buffa_types::google::protobuf::Timestamp;
use connectrpc::ConnectError;

use crate::proto::filesystem::{EntryInfo, FileType};

/// uid/gid -> name, read from /etc/passwd and /etc/group (musl reads them directly; there is no NSS).
#[derive(Default)]
pub struct Names {
    users: Mutex<HashMap<u32, String>>,
    groups: Mutex<HashMap<u32, String>>,
}

impl Names {
    fn user(&self, uid: u32) -> String {
        let mut m = crate::lock(&self.users);
        m.entry(uid)
            .or_insert_with(|| {
                nix::unistd::User::from_uid(uid.into())
                    .ok()
                    .flatten()
                    .map(|u| u.name)
                    .unwrap_or_else(|| uid.to_string())
            })
            .clone()
    }

    fn group(&self, gid: u32) -> String {
        let mut m = crate::lock(&self.groups);
        m.entry(gid)
            .or_insert_with(|| {
                nix::unistd::Group::from_gid(gid.into())
                    .ok()
                    .flatten()
                    .map(|g| g.name)
                    .unwrap_or_else(|| gid.to_string())
            })
            .clone()
    }
}

/// Go's `FileMode.String()` (what E2B envd reports as `permissions`), e.g. `drwxr-xr-x`, `-rw-r--r--`.
pub fn permissions(md: &Metadata) -> String {
    let ft = md.file_type();
    let mut s = String::with_capacity(11);
    let mode = md.permissions().mode();
    if ft.is_dir() {
        s.push('d');
    }
    if ft.is_symlink() {
        s.push('L');
    }
    if ft.is_block_device() {
        s.push('D');
    }
    if ft.is_fifo() {
        s.push('p');
    }
    if ft.is_socket() {
        s.push('S');
    }
    if mode & 0o4000 != 0 {
        s.push('u');
    }
    if mode & 0o2000 != 0 {
        s.push('g');
    }
    if ft.is_char_device() {
        s.push('c');
    }
    if mode & 0o1000 != 0 {
        s.push('t');
    }
    if s.is_empty() {
        s.push('-');
    }
    for (i, ch) in "rwxrwxrwx".chars().enumerate() {
        s.push(if mode & (1 << (8 - i)) != 0 { ch } else { '-' });
    }
    s
}

/// Describes `path` without following a final symlink; for a symlink, `type` describes its target and
/// `is_symlink` is set (E2B's convention).
pub fn entry_info(names: &Names, path: &Path) -> io::Result<EntryInfo> {
    let lmd = std::fs::symlink_metadata(path)?;
    let is_symlink = lmd.file_type().is_symlink();
    let (md, target) = if is_symlink {
        (std::fs::metadata(path).ok(), std::fs::read_link(path).ok())
    } else {
        (Some(lmd.clone()), None)
    };
    let ty = match &md {
        Some(m) if m.is_dir() => FileType::FILE_TYPE_DIRECTORY,
        Some(m) if m.is_file() => FileType::FILE_TYPE_FILE,
        None => FileType::FILE_TYPE_SYMLINK, // dangling link
        _ => FileType::FILE_TYPE_UNSPECIFIED,
    };
    let shown = md.as_ref().unwrap_or(&lmd);
    Ok(EntryInfo {
        name: path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "/".into()),
        r#type: ty.into(),
        path: path.to_string_lossy().into_owned(),
        size: shown.len() as i64,
        mode: shown.permissions().mode() & 0o777, // Go's FileMode.Perm(), as E2B reports it
        permissions: permissions(&lmd),
        owner: names.user(lmd.uid()),
        group: names.group(lmd.gid()),
        modified_time: MessageField::some(Timestamp {
            seconds: lmd.mtime(),
            nanos: lmd.mtime_nsec() as i32,
            ..Default::default()
        }),
        symlink_target: target.map(|t| t.to_string_lossy().into_owned()),
        is_symlink,
        ..Default::default()
    })
}

pub fn io_err(what: &str, path: &Path, e: io::Error) -> ConnectError {
    let msg = format!("{what} {}: {e}", path.display());
    match e.kind() {
        io::ErrorKind::NotFound => ConnectError::not_found(msg),
        io::ErrorKind::PermissionDenied => ConnectError::permission_denied(msg),
        io::ErrorKind::AlreadyExists => ConnectError::already_exists(msg),
        io::ErrorKind::InvalidInput | io::ErrorKind::NotADirectory | io::ErrorKind::IsADirectory => {
            ConnectError::invalid_argument(msg)
        }
        io::ErrorKind::StorageFull => ConnectError::resource_exhausted(msg),
        _ => ConnectError::internal(msg),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn describes_files_dirs_and_links() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("f.txt");
        std::fs::write(&f, b"hello").unwrap();
        std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o640)).unwrap();
        std::os::unix::fs::symlink(&f, d.path().join("l")).unwrap();
        std::os::unix::fs::symlink("/nonexistent", d.path().join("dangling")).unwrap();
        let n = Names::default();
        let e = entry_info(&n, &f).unwrap();
        assert_eq!((e.name.as_str(), e.size, e.mode), ("f.txt", 5, 0o640));
        assert_eq!(e.permissions, "-rw-r-----");
        assert_eq!(e.r#type.as_known(), Some(FileType::FILE_TYPE_FILE));
        let l = entry_info(&n, &d.path().join("l")).unwrap();
        assert!(l.is_symlink);
        assert_eq!(l.r#type.as_known(), Some(FileType::FILE_TYPE_FILE));
        assert_eq!(l.symlink_target.as_deref(), Some(f.to_str().unwrap()));
        let g = entry_info(&n, &d.path().join("dangling")).unwrap();
        assert_eq!(g.r#type.as_known(), Some(FileType::FILE_TYPE_SYMLINK));
        let dir = entry_info(&n, d.path()).unwrap();
        assert!(dir.permissions.starts_with('d'));
        assert!(entry_info(&n, &d.path().join("missing")).is_err());
    }
}
