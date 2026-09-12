use fst_reader::{FstHierarchyEntry, FstReader};
fn main() {
    let path = std::env::args().nth(1).unwrap();
    let file = std::fs::File::open(&path).unwrap();
    let mut reader = FstReader::open_and_read_time_table(std::io::BufReader::new(file)).unwrap();
    let header = reader.get_header();
    println!("header: {:?}", header);
    let mut scope = Vec::new();
    reader
        .read_hierarchy(|e| match e {
            FstHierarchyEntry::Scope { name, tpe, .. } => {
                println!("scope {:?} {}", tpe, name);
                scope.push(name);
            }
            FstHierarchyEntry::UpScope => {
                scope.pop();
            }
            FstHierarchyEntry::Var {
                name,
                handle,
                length,
                tpe,
                is_alias,
                ..
            } => {
                let full = if scope.is_empty() {
                    name.clone()
                } else {
                    format!("{}.{}", scope.join("."), name)
                };
                println!(
                    "var {:?} handle_idx={} len={} tpe={:?} is_alias={}",
                    full,
                    handle.get_index(),
                    length,
                    tpe,
                    is_alias
                );
            }
            other => println!("other {:?}", other),
        })
        .unwrap();
}
